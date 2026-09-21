from __future__ import annotations

import asyncio
import base64
import io
import shutil
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import qrcode
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from bili.client import BiliClient
from bili.crawler import Crawler, JobConfig
from bili.paths import DATA_DIR, LIBRARY_DIR, ensure_dirs
from bili.settings import AppSettings, load_settings, save_settings
from bili.store import Store
from bili.media import which_ffmpeg
from bili.ocr import ocr_available
from bili.runtime import prepare_process, probe_compute
from bili.storage import KEEP_POLICIES, reclaim_library, which_rclone
from bili.transcribe import whisper_available
from bili.util import parse_uids

prepare_process()

STATIC_DIR = Path(__file__).resolve().parent / "static"
ensure_dirs()
store = Store()
store.mark_interrupted_jobs()
app = FastAPI(title="BiliArchiver", version="1.0.0")
JOB_LOCK = asyncio.Lock()


class SettingsIn(BaseModel):
    cookie: str | None = None
    min_interval: float | None = None
    max_interval: float | None = None
    max_retries: int | None = None
    comment_max_pages: int | None = None
    include_sub_replies: bool | None = None
    danmaku_mode: str | None = None
    media_mode: str | None = None
    transcribe_mode: str | None = None
    media_keep: str | None = None
    ocr_enabled: bool | None = None
    whisper_model: str | None = None
    whisper_language: str | None = None
    whisper_device: str | None = None
    whisper_compute_type: str | None = None
    ocr_min_confidence: float | None = None
    video_quality: int | None = None
    ffmpeg_path: str | None = None
    rclone_remote: str | None = None
    rclone_root: str | None = None


class JobIn(BaseModel):
    uids_text: str
    time_range: str = "1y"
    crawl_profile: bool = True
    crawl_videos: bool = True
    crawl_dynamics: bool = True
    crawl_comments: bool = True
    crawl_danmaku: bool = True
    media_mode: str = "link"
    transcribe_mode: str = "official"
    media_keep: str = "delete_after_text"
    ocr_enabled: bool = True
    resume: bool = True


class JobRuntime:
    def __init__(self, job_id: str, config: dict[str, Any]):
        self.id = job_id
        self.config = config
        self.status = "queued"
        self.progress: dict[str, Any] = {}
        self.logs: deque[dict[str, str]] = deque(maxlen=800)
        self.subscribers: list[asyncio.Queue] = []
        self.cancel = False
        self.task: asyncio.Task | None = None
        self.error = ""

    def emit(self, event: str, payload: Any) -> None:
        message = {"event": event, "data": payload}
        for q in list(self.subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass


JOBS: dict[str, JobRuntime] = {}
QR_SESSIONS: dict[str, BiliClient] = {}


def public_settings(s: AppSettings) -> dict[str, Any]:
    data = s.to_public_dict()
    data["whisper_installed"] = whisper_available()
    data["ocr_installed"] = ocr_available()
    data["ffmpeg_installed"] = bool(which_ffmpeg(s.ffmpeg_path))
    data["gpu"] = probe_compute()
    data["library_dir"] = str(LIBRARY_DIR)
    return data


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "name": "BiliArchiver"}


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return public_settings(load_settings())


@app.put("/api/settings")
async def put_settings(body: SettingsIn) -> dict[str, Any]:
    current = load_settings()
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(current, key, value)
    if current.min_interval < 0.2 or current.max_interval < current.min_interval:
        raise HTTPException(400, "请求间隔必须 ≥ 0.2 秒，且最大间隔不能小于最小间隔")
    current.max_retries = min(10, max(1, int(current.max_retries)))
    current.comment_max_pages = max(0, int(current.comment_max_pages))
    current.ocr_min_confidence = min(0.95, max(0.1, float(current.ocr_min_confidence)))
    if current.whisper_device not in {"auto", "cpu", "cuda"}:
        raise HTTPException(400, "Whisper 设备无效")
    if current.whisper_compute_type not in {"auto", "float16", "int8_float16", "int8"}:
        raise HTTPException(400, "Whisper 计算类型无效")
    save_settings(current)
    return public_settings(current)


@app.get("/api/capabilities")
async def capabilities() -> dict[str, Any]:
    settings = load_settings()
    usage = shutil.disk_usage(DATA_DIR)
    gpu = probe_compute()
    pip_command = (
        "pip install -r requirements-gpu-windows.txt"
        if gpu["platform"].startswith("win")
        else "pip install faster-whisper rapidocr onnxruntime"
    )
    return {
        "ffmpeg": {
            "ready": bool(which_ffmpeg(settings.ffmpeg_path)),
            "purpose": "封装 m4a、合并 DASH 音视频为 MP4",
        },
        "whisper": {
            "ready": whisper_available(),
            "purpose": "没有官方字幕时，在本机把音频转为带时间戳文字",
        },
        "ocr": {
            "ready": ocr_available(),
            "purpose": "识别动态图片中的文字，并保留置信度与坐标",
        },
        "gpu": {
            "ready": gpu["cuda_devices"] > 0,
            "purpose": gpu["note"],
            "name": gpu.get("gpu_name") or "",
            "device": gpu["device"],
        },
        "rclone": {
            "ready": bool(which_rclone()),
            "purpose": "把媒体上传到 Google Drive 后再删除本地大文件",
        },
        "disk": {
            "free_bytes": usage.free,
            "total_bytes": usage.total,
        },
        "pip_command": pip_command,
    }


@app.post("/api/login/qr/start")
async def qr_start() -> dict[str, Any]:
    settings = load_settings()
    client = BiliClient(settings)
    await client._ensure_buvid()
    data = await client.qr_generate()
    QR_SESSIONS[data["qrcode_key"]] = client
    img = qrcode.make(data["url"])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return {
        "qrcode_key": data["qrcode_key"],
        "url": data["url"],
        "image": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
    }


@app.get("/api/login/qr/poll")
async def qr_poll(key: str) -> dict[str, Any]:
    client = QR_SESSIONS.get(key)
    if not client:
        raise HTTPException(404, "二维码已过期，请重新生成")
    payload = await client.qr_poll(key)
    data = payload.get("data") or {}
    code = data.get("code")
    if code == 0:
        settings = load_settings()
        settings.cookie = client.cookie_header()
        save_settings(settings)
        await client.close()
        QR_SESSIONS.pop(key, None)
        nav = {"is_login": True}
        return {"code": 0, "message": "登录成功", "settings": public_settings(settings), "nav": nav}
    if code == 86038:
        await client.close()
        QR_SESSIONS.pop(key, None)
    return {"code": code, "message": data.get("message") or ""}


@app.get("/api/nav")
async def nav_status() -> dict[str, Any]:
    settings = load_settings()
    client = BiliClient(settings)
    try:
        await client._ensure_buvid()
        nav = await client.get_nav()
        return {
            "is_login": bool((nav.get("data") or {}).get("isLogin")),
            "uname": (nav.get("data") or {}).get("uname") or "",
            "mid": (nav.get("data") or {}).get("mid"),
            "settings": public_settings(settings),
        }
    except Exception as exc:
        return {"is_login": False, "error": str(exc), "settings": public_settings(settings)}
    finally:
        await client.close()


@app.post("/api/jobs")
async def create_job(body: JobIn) -> dict[str, Any]:
    uids = parse_uids(body.uids_text)
    if not uids:
        raise HTTPException(400, "请填写至少一个 UID 或空间链接")
    if body.time_range not in {"all", "3y", "2y", "1y", "9m", "6m", "3m"}:
        raise HTTPException(400, "时间范围无效")
    if body.media_mode not in {"none", "link", "audio", "video"}:
        raise HTTPException(400, "媒体策略无效")
    if body.transcribe_mode not in {"none", "url_only", "official", "whisper", "official_then_whisper"}:
        raise HTTPException(400, "转写策略无效")
    if body.transcribe_mode in {"whisper", "official_then_whisper"} and body.media_mode not in {"audio", "video"}:
        raise HTTPException(400, "本地 Whisper 需要音频：请把媒体策略改为“下载音频”或“下载视频”")
    if body.media_keep not in KEEP_POLICIES:
        raise HTTPException(400, "空间策略无效")
    if body.media_keep == "upload_then_delete" and not which_rclone():
        raise HTTPException(400, "上传 Google Drive 需要先安装 rclone 并完成 rclone config")
    job_id = uuid.uuid4().hex[:12]
    config = body.model_dump()
    config["uids"] = uids
    runtime = JobRuntime(job_id, config)
    JOBS[job_id] = runtime
    store.save_job(job_id, config, "queued", {})
    runtime.task = asyncio.create_task(_run_job(runtime))
    return {"id": job_id, "uids": uids, "status": "queued"}


@app.get("/api/jobs")
async def list_jobs() -> dict[str, Any]:
    live = []
    for job in JOBS.values():
        live.append(
            {
                "id": job.id,
                "status": job.status,
                "progress": job.progress,
                "config": job.config,
                "error": job.error,
                "logs": list(job.logs)[-30:],
            }
        )
    return {"live": live, "history": store.list_jobs()}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return {
        "id": job.id,
        "status": job.status,
        "progress": job.progress,
        "config": job.config,
        "error": job.error,
        "logs": list(job.logs),
    }


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, str]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    job.cancel = True
    job.status = "cancelling"
    job.emit("status", {"status": "cancelling"})
    return {"id": job_id, "status": "cancelling"}


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    job.subscribers.append(queue)

    async def gen():
        try:
            queue.put_nowait({"event": "snapshot", "data": {"status": job.status, "progress": job.progress, "logs": list(job.logs)[-40:]}})
            if job.status in {"done", "error", "cancelled"}:
                yield f"event: done\ndata: {_json({'status': job.status, 'progress': job.progress, 'error': job.error})}\n\n"
                return
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: {message['event']}\ndata: { _json(message['data']) }\n\n"
                if message["event"] == "done":
                    break
        finally:
            if queue in job.subscribers:
                job.subscribers.remove(queue)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/results")
async def results() -> dict[str, Any]:
    return {"accounts": store.list_accounts(), "library_dir": str(LIBRARY_DIR)}


@app.get("/api/results/{mid}")
async def result_detail(mid: str) -> dict[str, Any]:
    detail = store.account_detail(mid)
    if not detail.get("account"):
        raise HTTPException(404, "尚未采集该账号")
    return detail


@app.get("/api/file")
async def read_file(path: str) -> dict[str, str]:
    target = Path(path).resolve()
    allowed = (LIBRARY_DIR.resolve(), DATA_DIR.resolve())
    if not any(target.is_relative_to(base) for base in allowed):
        raise HTTPException(403, "路径不允许")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "文件不存在")
    if target.stat().st_size > 2_000_000:
        return {"path": str(target), "content": "文件过大，请在资源管理器中打开", "truncated": True}
    return {"path": str(target), "content": target.read_text(encoding="utf-8", errors="replace")}


def _json(data: Any) -> str:
    import json

    return json.dumps(data, ensure_ascii=False)


async def _run_job(job: JobRuntime) -> None:
    async with JOB_LOCK:
        if job.cancel:
            job.status = "cancelled"
            store.save_job(job.id, job.config, job.status, {}, "")
            job.emit("done", {"status": job.status, "progress": {}, "error": ""})
            return
        await _execute_job(job)


async def _execute_job(job: JobRuntime) -> None:
    settings = load_settings()
    crawler = Crawler(
        settings=settings,
        store=store,
        library=LIBRARY_DIR,
        should_cancel=lambda: job.cancel,
    )

    def on_log(level: str, message: str) -> None:
        item = {"level": level, "message": message}
        job.logs.append(item)
        job.emit("log", item)

    crawler.on_log = on_log
    cfg = JobConfig(
        uids=job.config["uids"],
        time_range=job.config.get("time_range") or "1y",
        crawl_profile=job.config.get("crawl_profile", True),
        crawl_videos=job.config.get("crawl_videos", True),
        crawl_dynamics=job.config.get("crawl_dynamics", True),
        crawl_comments=job.config.get("crawl_comments", True),
        crawl_danmaku=job.config.get("crawl_danmaku", True),
        media_mode=job.config.get("media_mode") or settings.media_mode,
        transcribe_mode=job.config.get("transcribe_mode") or settings.transcribe_mode,
        media_keep=job.config.get("media_keep") or settings.media_keep,
        ocr_enabled=bool(job.config.get("ocr_enabled", True)),
        resume=bool(job.config.get("resume", True)),
        job_id=job.id,
    )

    async def on_progress(payload: dict[str, Any]) -> None:
        if job.cancel:
            crawler.cancelled = True
        job.progress = payload
        job.emit("progress", payload)
        store.save_job(job.id, job.config, job.status, payload, job.error)

    job.status = "running"
    on_log("info", f"任务启动 · {len(cfg.uids)} 个账号 · 范围 {cfg.time_range}")
    try:
        await crawler.run(cfg, on_progress=on_progress)
        job.status = "cancelled" if job.cancel else "done"
        on_log("ok", "任务结束" if not job.cancel else "已取消")
    except asyncio.CancelledError:
        job.status = "cancelled"
        on_log("warn", "任务已取消；下载断点和已完成步骤已保留")
    except Exception as exc:
        if job.cancel:
            job.status = "cancelled"
            on_log("warn", "任务已取消；已完成步骤已保留")
        else:
            job.status = "error"
            job.error = str(exc)
            on_log("error", f"任务失败：{exc}")
    store.save_job(job.id, job.config, job.status, job.progress, job.error)
    job.emit("done", {"status": job.status, "progress": job.progress, "error": job.error})


class ReclaimIn(BaseModel):
    policy: str = "delete_after_text"
    dry_run: bool = False


@app.post("/api/library/reclaim")
async def reclaim_existing(body: ReclaimIn) -> dict[str, Any]:
    if body.policy not in KEEP_POLICIES:
        raise HTTPException(400, "空间策略无效")
    if body.policy == "upload_then_delete" and not which_rclone():
        raise HTTPException(400, "上传 Google Drive 需要先安装 rclone 并完成 rclone config")
    settings = load_settings()
    summary = await asyncio.to_thread(
        reclaim_library,
        LIBRARY_DIR,
        policy=body.policy,
        ffmpeg_path=settings.ffmpeg_path,
        rclone_remote=settings.rclone_remote,
        rclone_root=settings.rclone_root,
        dry_run=body.dry_run,
    )
    return summary


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
