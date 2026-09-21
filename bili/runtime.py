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


def register_cuda_dlls() -> list[str]:
    """Add pip-installed NVIDIA CUDA/cuDNN bins to the DLL search path.

    faster-whisper on Windows often fails with missing cudnn_ops64_9.dll unless
    these directories are registered. Safe to call repeatedly.
    """
    added: list[str] = []
    seen: set[str] = set()

    def _add(path: Path) -> None:
        resolved = str(path)
        if resolved in seen or not path.is_dir():
            return
        seen.add(resolved)
        os.environ["PATH"] = resolved + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(resolved)
            except OSError:
                pass
        added.append(resolved)

    for env_key in ("CUDA_PATH", "CUDNN_PATH"):
        root = os.environ.get(env_key)
        if root:
            _add(Path(root) / "bin")
            for child in (Path(root) / "bin").glob("*"):
                if child.is_dir():
                    _add(child)

    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    cudnn_root = program_files / "NVIDIA" / "CUDNN"
    if cudnn_root.exists():
        for bin_dir in cudnn_root.glob("v*/bin"):
            _add(bin_dir)
            for child in bin_dir.glob("*"):
                if child.is_dir():
                    _add(child)
    cuda_root = program_files / "NVIDIA GPU Computing Toolkit" / "CUDA"
    if cuda_root.exists():
        for bin_dir in cuda_root.glob("v*/bin"):
            _add(bin_dir)

    try:
        import nvidia  # type: ignore
    except Exception:
        nvidia = None
    if nvidia is not None:
        nvidia_root = Path(nvidia.__file__).resolve().parent
        for bin_dir in nvidia_root.glob("*/bin"):
            _add(bin_dir)
        for lib_dir in nvidia_root.glob("*/lib"):
            _add(lib_dir)

    return added


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
