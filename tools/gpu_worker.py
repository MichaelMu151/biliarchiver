#!/usr/bin/env python3
"""HTTP Whisper worker for a rented GPU box or a local NVIDIA laptop."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from bili.ocr import _run_ocr, ocr_available
from bili.runtime import probe_compute
from bili.transcribe import transcribe_local, warmup_whisper, whisper_available, whisper_loaded_key

app = FastAPI(title="BiliArchiver GPU Worker", version="1.0.0")
WORKER_TOKEN = ""
DEFAULT_MODEL = "small"
DEFAULT_DEVICE = "auto"
DEFAULT_COMPUTE = "auto"


def _check_token(authorization: str | None) -> None:
    if not WORKER_TOKEN:
        return
    expected = f"Bearer {WORKER_TOKEN}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "Token 无效")


@app.get("/health")
async def health(authorization: str | None = Header(default=None)) -> dict:
    _check_token(authorization)
    gpu = probe_compute()
    return {
        "ok": True,
        "whisper": whisper_available(),
        "ocr": ocr_available(),
        "device": gpu.get("device") or "cpu",
        "gpu_name": gpu.get("gpu_name") or "",
        "cuda_devices": gpu.get("cuda_devices") or 0,
        "model": DEFAULT_MODEL,
        "loaded": whisper_loaded_key(),
    }


def _nvidia_smi_line() -> str:
    try:
        text = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=8,
        )
        return (text or "").strip().splitlines()[0].strip()
    except Exception:
        return ""


@app.post("/v1/warmup")
async def warmup(
    model: str = Form(""),
    authorization: str | None = Header(default=None),
):
    _check_token(authorization)
    if not whisper_available():
        raise HTTPException(500, "工作机未安装 faster-whisper")
    name = (model.strip() if model else "") or DEFAULT_MODEL
    try:
        result = await asyncio.to_thread(warmup_whisper, name, DEFAULT_DEVICE, DEFAULT_COMPUTE)
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    result["nvidia_smi"] = _nvidia_smi_line()
    gpu = probe_compute()
    result["gpu_name"] = gpu.get("gpu_name") or ""
    return JSONResponse(result)


@app.post("/v1/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    model: str = Form(""),
    language: str = Form("auto"),
    authorization: str | None = Header(default=None),
):
    _check_token(authorization)
    if not whisper_available():
        raise HTTPException(500, "工作机未安装 faster-whisper")
    suffix = Path(file.filename or "audio.m4a").suffix or ".m4a"
    payload = await file.read()
    if not payload:
        raise HTTPException(400, "空音频")
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(payload)
            tmp_path = tmp.name
        result = await asyncio.to_thread(
            transcribe_local,
            tmp_path,
            (model.strip() if model else "") or DEFAULT_MODEL,
            language or "auto",
            None,
            DEFAULT_DEVICE,
            DEFAULT_COMPUTE,
        )
        result["status"] = "done"
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


@app.post("/v1/transcribe_path")
async def transcribe_path(
    path: str = Form(...),
    model: str = Form(""),
    language: str = Form("auto"),
    authorization: str | None = Header(default=None),
):
    """Transcribe a file already on this GPU box (SFTP first, then call this)."""
    _check_token(authorization)
    if not whisper_available():
        raise HTTPException(500, "工作机未安装 faster-whisper")
    audio = Path(path)
    allowed = str(audio.resolve()).startswith(("/tmp/", "/root/autodl-tmp/"))
    if not allowed or not audio.is_file():
        raise HTTPException(400, "音频路径无效")
    try:
        result = await asyncio.to_thread(
            transcribe_local,
            str(audio),
            (model.strip() if model else "") or DEFAULT_MODEL,
            language or "auto",
            None,
            DEFAULT_DEVICE,
            DEFAULT_COMPUTE,
        )
        result["status"] = "done"
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    finally:
        audio.unlink(missing_ok=True)


@app.post("/v1/ocr")
async def ocr_image(
    file: UploadFile = File(...),
    min_confidence: float = Form(0.55),
    authorization: str | None = Header(default=None),
):
    _check_token(authorization)
    if not ocr_available():
        raise HTTPException(500, "工作机未安装 RapidOCR")
    suffix = Path(file.filename or "image.jpg").suffix or ".jpg"
    payload = await file.read()
    if not payload:
        raise HTTPException(400, "空图片")
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(payload)
            tmp_path = tmp.name
        return JSONResponse(_run_ocr(Path(tmp_path), float(min_confidence)))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


def main() -> int:
    global WORKER_TOKEN, DEFAULT_MODEL, DEFAULT_DEVICE, DEFAULT_COMPUTE
    parser = argparse.ArgumentParser(description="在租用的 GPU 或本地 NVIDIA 电脑上启动转写工作机")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--token", required=True, help="和本机设置里填写的 Token 一致")
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--compute", default="auto")
    args = parser.parse_args()
    if not whisper_available():
        print("缺少 faster-whisper：pip install -r requirements-ai.txt", file=sys.stderr)
        return 2
    WORKER_TOKEN = args.token.strip()
    DEFAULT_MODEL = args.model
    DEFAULT_DEVICE = args.device
    DEFAULT_COMPUTE = args.compute
    import uvicorn

    print(f"BiliArchiver GPU worker on http://{args.host}:{args.port}  model={args.model}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
