from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any


def prepare_process() -> None:
    """Make Windows consoles and NVIDIA DLLs usable before importing CUDA libs."""
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
        os.environ.setdefault("PYTHONUTF8", "1")
    register_cuda_dlls()


def _nvidia_package_roots() -> list[Path]:
    """``nvidia`` is often a namespace package: ``__file__`` is None, ``__path__`` is set."""
    roots: list[Path] = []
    try:
        import nvidia  # type: ignore
    except Exception:
        return roots
    nvidia_file = getattr(nvidia, "__file__", None)
    if isinstance(nvidia_file, (str, os.PathLike)):
        roots.append(Path(nvidia_file).resolve().parent)
    for item in getattr(nvidia, "__path__", []) or []:
        try:
            roots.append(Path(item))
        except TypeError:
            continue
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def cuda_library_dirs() -> list[str]:
    """Directories that may contain libcublas / cuDNN. Used on PATH and LD_LIBRARY_PATH."""
    dirs: list[Path] = []
    for env_key in ("CUDA_PATH", "CUDNN_PATH", "CUDA_HOME"):
        root = os.environ.get(env_key)
        if root:
            dirs.append(Path(root) / "bin")
            dirs.append(Path(root) / "lib")
            dirs.append(Path(root) / "lib64")
            bin_dir = Path(root) / "bin"
            if bin_dir.is_dir():
                dirs.extend(child for child in bin_dir.glob("*") if child.is_dir())
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    cudnn_root = program_files / "NVIDIA" / "CUDNN"
    if cudnn_root.exists():
        for bin_dir in cudnn_root.glob("v*/bin"):
            dirs.append(bin_dir)
            dirs.extend(child for child in bin_dir.glob("*") if child.is_dir())
    cuda_root = program_files / "NVIDIA GPU Computing Toolkit" / "CUDA"
    if cuda_root.exists():
        for bin_dir in cuda_root.glob("v*/bin"):
            dirs.append(bin_dir)
    for system_cuda in (
        Path("/usr/local/cuda"),
        Path("/usr/local/cuda-12"),
        Path("/usr/local/cuda-12.4"),
        Path("/usr/local/cuda-12.1"),
        Path("/usr/local/cuda-13"),
        Path("/usr/local/cuda-13.0"),
    ):
        dirs.append(system_cuda / "lib64")
        dirs.append(system_cuda / "lib")
    for nvidia_root in _nvidia_package_roots():
        dirs.extend(nvidia_root.glob("*/bin"))
        dirs.extend(nvidia_root.glob("*/lib"))
        dirs.extend(nvidia_root.glob("*/lib64"))
    ordered: list[str] = []
    seen: set[str] = set()
    for path in dirs:
        if not path.is_dir():
            continue
        resolved = str(path.resolve()) if path.exists() else str(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(resolved)
    return ordered


def register_cuda_dlls() -> list[str]:
    """Put CUDA/cuDNN dirs on PATH (Windows) and LD_LIBRARY_PATH (Linux).

    faster-whisper on Windows often fails with missing cudnn_ops64_9.dll unless
    these directories are registered. On AutoDL, pip ctranslate2 wants
    libcublas.so.12 even when the image only shipped CUDA 13.
    """
    added = cuda_library_dirs()
    if not added:
        return added

    def _prepend(env_key: str) -> None:
        current = [part for part in os.environ.get(env_key, "").split(os.pathsep) if part]
        extra = [folder for folder in added if folder not in current]
        if extra:
            os.environ[env_key] = os.pathsep.join(extra + current)

    _prepend("PATH")
    if os.name != "nt":
        _prepend("LD_LIBRARY_PATH")
    if hasattr(os, "add_dll_directory"):
        for folder in added:
            try:
                os.add_dll_directory(folder)
            except OSError:
                pass
    if os.name != "nt":
        _preload_cuda_libs(added)
    return added


def _preload_cuda_libs(dirs: list[str]) -> None:
    """dlopen CUDA 12 libs now; changing LD_LIBRARY_PATH later is too late for ld.so."""
    try:
        import ctypes
    except Exception:
        return
    names = (
        "libcublas.so.12",
        "libcublasLt.so.12",
        "libcudart.so.12",
        "libcudnn.so.9",
        "libnvrtc.so.12",
    )
    for folder in dirs:
        root = Path(folder)
        for name in names:
            candidate = root / name
            if not candidate.exists():
                continue
            try:
                ctypes.CDLL(str(candidate), mode=getattr(ctypes, "RTLD_GLOBAL", 256))
            except OSError:
                continue


def ffmpeg_candidates(explicit: str = "ffmpeg") -> list[Path]:
    names = []
    if explicit:
        names.append(explicit)
        if os.name == "nt" and not explicit.lower().endswith(".exe"):
            names.append(explicit + ".exe")
    names.extend(["ffmpeg.exe", "ffmpeg"] if os.name == "nt" else ["ffmpeg"])
    root = Path(__file__).resolve().parent.parent
    candidates: list[Path] = []
    for name in names:
        expanded = Path(name).expanduser()
        candidates.append(expanded)
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for extra in (
        root / "tools" / "bin" / "ffmpeg.exe",
        root / "tools" / "bin" / "ffmpeg",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ffmpeg" / "bin" / "ffmpeg.exe",
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
    ):
        candidates.append(extra)
    ordered: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


def run_quiet(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {"capture_output": True, "text": True}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


def nvidia_smi_gpus() -> list[dict[str, str]]:
    smi = shutil.which("nvidia-smi")
    if not smi:
        return []
    proc = run_quiet(
        [smi, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"]
    )
    if proc.returncode != 0:
        return []
    gpus = []
    for line in (proc.stdout or "").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 2:
            gpus.append(
                {
                    "name": parts[0],
                    "memory_mb": parts[1],
                    "driver": parts[2] if len(parts) > 2 else "",
                }
            )
    return gpus


@lru_cache(maxsize=1)
def probe_compute() -> dict[str, Any]:
    register_cuda_dlls()
    info: dict[str, Any] = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "whisper_installed": False,
        "cuda_devices": 0,
        "device": "cpu",
        "compute_type": "int8",
        "gpu_name": "",
        "gpus": nvidia_smi_gpus(),
        "dll_dirs": [],
        "note": "将使用 CPU",
    }
    try:
        import faster_whisper  # noqa: F401

        info["whisper_installed"] = True
    except Exception:
        info["note"] = "未安装 faster-whisper"
        return info
    try:
        import ctranslate2

        info["cuda_devices"] = int(ctranslate2.get_cuda_device_count() or 0)
    except Exception as exc:
        info["note"] = f"ctranslate2 无法探测 CUDA：{exc}"
        return info
    if info["gpus"]:
        info["gpu_name"] = info["gpus"][0]["name"]
    if info["cuda_devices"] > 0:
        info["device"] = "cuda"
        info["compute_type"] = "float16"
        label = info["gpu_name"] or "NVIDIA GPU"
        info["note"] = f"将使用 GPU：{label}（float16）"
        memory = 0
        try:
            memory = int(float((info["gpus"][0] or {}).get("memory_mb") or 0))
        except (TypeError, ValueError):
            memory = 0
        if memory and memory < 6000:
            info["compute_type"] = "int8_float16"
            info["note"] = f"显存约 {memory} MB，建议 int8_float16"
    else:
        if info["gpus"]:
            info["note"] = (
                "检测到 NVIDIA 驱动，但 Python 看不到 CUDA。"
                "请把 python.exe 设为高性能 NVIDIA 处理器，并安装 requirements-gpu-windows.txt"
            )
        else:
            info["note"] = "未检测到 NVIDIA GPU，使用 CPU"
    return info


def resolve_whisper_backend(requested_device: str, requested_compute: str) -> tuple[str, str, str]:
    probe = probe_compute()
    device = (requested_device or "auto").lower()
    compute = (requested_compute or "auto").lower()
    if device == "cpu":
        return "cpu", "int8" if compute in {"auto", ""} else compute, "按设置使用 CPU"
    if probe["cuda_devices"] <= 0:
        if device == "cuda":
            return "cpu", "int8", "请求 CUDA 失败，已回退 CPU"
        return "cpu", "int8", probe["note"]
    use_compute = probe["compute_type"] if compute in {"auto", ""} else compute
    return "cuda", use_compute, probe["note"]
