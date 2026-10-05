from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


def normalize_worker_url(raw: str) -> str:
    text = (raw or "").strip().rstrip("/")
    if not text:
        return ""
    if not text.startswith("http://") and not text.startswith("https://"):
        text = "http://" + text
    parsed = urlparse(text)
    if not parsed.netloc:
        return ""
    return text.rstrip("/")


def worker_headers(token: str) -> dict[str, str]:
    token = (token or "").strip()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


CLOUD_WHISPER_MODEL = "large-v3"


def whisper_model_for_backend(local_model: str, compute_backend: str | None = None) -> str:
    """Cloud GPU jobs use large-v3; do not inherit the Mac's small default."""
    name = (local_model or "").strip()
    if (compute_backend or "local") != "cloud":
        return name or "small"
    if name.lower().startswith("large"):
        return name
    return CLOUD_WHISPER_MODEL


def resolve_compute(transcribe_mode: str, compute_backend: str | None = None) -> tuple[str, str]:
    """Legacy cloud transcribe modes collapse into (mode, backend)."""
    mode = transcribe_mode or "official"
    backend = (compute_backend or "local").strip() or "local"
    if mode == "official_then_cloud":
        return "official_then_whisper", "cloud"
    if mode == "cloud_gpu":
        return "whisper", "cloud"
    if backend not in {"local", "cloud"}:
        backend = "local"
    return mode, backend


def needs_remote_models(transcribe_mode: str, compute_backend: str, ocr_enabled: bool) -> bool:
    mode, backend = resolve_compute(transcribe_mode, compute_backend)
    if backend != "cloud":
        return False
    return ocr_enabled or mode in {"whisper", "official_then_whisper"}


def gpu_worker_ready(url: str, token: str = "", timeout: float = 6.0) -> tuple[bool, str]:
    base = normalize_worker_url(url)
    if not base:
        return False, "尚未填写云端 GPU 工作机地址。租卡后在那台机器启动 gpu_worker，再把公网或局域网 URL 填到设置里。"
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(f"{base}/health", headers=worker_headers(token))
    except Exception as exc:
        return False, f"无法连接 GPU 工作机：{exc}"
    if response.status_code in {401, 403}:
        return False, "GPU 工作机拒绝访问，请核对 Token。"
    if response.status_code >= 400:
        return False, f"GPU 工作机健康检查失败 HTTP {response.status_code}"
    try:
        data = response.json()
    except Exception:
        data = {}
    if not data.get("ok", True):
        return False, str(data.get("error") or "GPU 工作机未就绪")
    whisper = data.get("whisper")
    if whisper is False:
        return False, "工作机没有 faster-whisper，请在 GPU 机器执行 pip install -r requirements-ai.txt"
    if data.get("av_ok") is False:
        return False, (
            "工作机 PyAV 与 faster-whisper 不兼容"
            + (f"（{data.get('av')}）" if data.get("av") else "")
            + "。请重新接入采集转写通道，会自动把 av 钉到 <19。"
        )
    device = data.get("device") or "unknown"
    ocr = "OCR 可用" if data.get("ocr") else "OCR 未装"
    av = data.get("av") or ""
    av_note = f" · av {av}" if av and data.get("av_ok") else ""
    return True, f"已连接 {base} · 设备 {device} · {ocr}{av_note}"


def local_app_url() -> str:
    return (os.environ.get("BILI_APP_URL") or "http://127.0.0.1:8765").rstrip("/")


def _put_via_local_app(local_path: Path) -> str:
    """Ask the already-running web server to SFTP over its AutoDL SSH session."""
    timeout = httpx.Timeout(30.0, read=300.0, write=30.0, pool=30.0)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.post(
            f"{local_app_url()}/api/gpu/autodl/put",
            json={"path": str(local_path)},
        )
    if response.status_code >= 400:
        detail = (response.text or "")[:240]
        raise RuntimeError(f"本机代传失败 HTTP {response.status_code} {detail}")
    remote = str((response.json() or {}).get("remote") or "")
    if not remote:
        raise RuntimeError("本机代传没有返回远程路径")
    return remote


def _sftp_put_audio(local_path: Path, on_log=None) -> str:
    """Copy audio on the existing AutoDL SSH. Never open a second login."""
    from bili.autodl import put_via_session, session_alive

    def emit(message: str, level: str = "info") -> None:
        if on_log:
            on_log(level, message)

    if session_alive():
        return put_via_session(local_path, log=emit)
    return _put_via_local_app(local_path)


async def transcribe_remote_from_bili(
    *,
    bvid: str,
    urls: list[str],
    cookie: str,
    token: str,
    model: str,
    language: str,
    on_log,
) -> dict[str, Any]:
    """Let AutoDL download audio from Bilibili and Whisper locally. Archive stays on the Mac."""
    from bili.autodl import fetch_and_transcribe_via_session

    def emit(message: str, level: str = "info") -> None:
        on_log(level, message)

    data = await asyncio.to_thread(
        fetch_and_transcribe_via_session,
        bvid=bvid,
        urls=urls,
        referer=f"https://www.bilibili.com/video/{bvid}",
        cookie=cookie or "",
        token=token,
        model=model,
        language=language,
        log=emit,
    )
    data["status"] = "done"
    return data


async def transcribe_remote(
    *,
    url: str,
    token: str,
    audio_path: str,
    model: str,
    language: str,
    on_log,
) -> dict[str, Any]:
    """SFTP fallback: copy audio over the live SSH, then curl the worker on GPU localhost.

    Prefer ``transcribe_remote_from_bili`` (GPU pulls from Bilibili). This path is
    only used when the direct pull failed and a local audio file already exists.
    HTTP to the Mac's tunnel URL is last-resort: that port often goes stale.
    """
    path = Path(audio_path)
    if not path.is_file():
        raise RuntimeError("没有可上传的本地音频")

    def emit(message: str, level: str = "info") -> None:
        on_log(level, message)

    from bili.autodl import SESSION, session_alive, ssh_transcribe_audio

    if session_alive():
        emit(f"SFTP 备选：经已有 SSH 传 {path.name}，在 GPU 本机转写（不走本机隧道）")
        data = await asyncio.to_thread(
            ssh_transcribe_audio,
            SESSION.client,
            path,
            token,
            model,
            language,
            emit,
        )
        data["status"] = "done"
        return data

    base = normalize_worker_url(url)
    if not base:
        raise RuntimeError("未配置 GPU 工作机地址，且 AutoDL SSH 未接入")
    emit(f"SSH 未接入，经本机隧道传音频：{path.name} ({path.stat().st_size} 字节)")
    remote_path = await asyncio.to_thread(_sftp_put_audio, path, on_log)
    emit(f"已传到工作机，开始转写 {path.name}")
    timeout = httpx.Timeout(30.0, read=2400.0, write=60.0, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.post(
            f"{base}/v1/transcribe_path",
            headers=worker_headers(token),
            data={"path": remote_path, "model": model, "language": language},
        )
    if response.status_code in {401, 403}:
        raise RuntimeError("GPU 工作机 Token 不正确")
    if response.status_code >= 400:
        detail = (response.text or "")[:240]
        raise RuntimeError(f"云端转写失败 HTTP {response.status_code} {detail}")
    data = response.json()
    if not data.get("markdown"):
        raise RuntimeError("云端转写没有返回文字")
    data["status"] = "done"
    return data


async def ocr_remote(
    *,
    url: str,
    token: str,
    image_path: str,
    min_confidence: float,
) -> dict[str, Any]:
    base = normalize_worker_url(url)
    if not base:
        raise RuntimeError("未配置 GPU 工作机地址")
    path = Path(image_path)
    if not path.is_file():
        raise RuntimeError("没有可上传的图片")
    timeout = httpx.Timeout(20.0, read=180.0, write=60.0, pool=20.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        with path.open("rb") as fh:
            response = await client.post(
                f"{base}/v1/ocr",
                headers=worker_headers(token),
                data={"min_confidence": str(min_confidence)},
                files={"file": (path.name, fh, "application/octet-stream")},
            )
    if response.status_code in {401, 403}:
        raise RuntimeError("GPU 工作机 Token 不正确")
    if response.status_code >= 400:
        raise RuntimeError(f"云端 OCR 失败 HTTP {response.status_code} {(response.text or '')[:240]}")
    return response.json()
