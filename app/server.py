from __future__ import annotations

import asyncio
import base64
import io
import json
import secrets
import shutil
import threading
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import qrcode
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from bili.academic import AcademicConfig, AcademicCrawler, parse_academic_seeds
from bili.autodl import SESSION as AUTODL_SESSION, connect_autodl, put_via_session, session_alive
from bili.client import BiliClient
from bili.corpus import Corpus
from bili.crawler import Crawler, JobConfig
from bili.gpu_remote import (
    gpu_worker_ready,
    needs_remote_models,
    normalize_worker_url,
    resolve_compute,
    worker_headers,
)
from bili.ingest import ingest_library
from bili.paths import DATA_DIR, LIBRARY_DIR, ensure_dirs
from bili.settings import AppSettings, load_settings, save_settings
from bili.store import Store
from bili.media import which_ffmpeg
from bili.notify import normalize_bark_key, progress_messages, send_bark
from bili.ocr import ocr_available
from bili.runtime import prepare_process, probe_compute
from bili.storage import KEEP_POLICIES, rclone_drive_ready, rclone_remote_names, reclaim_library, which_rclone
from bili.transcribe import whisper_available
from bili.util import parse_uids

prepare_process()

STATIC_DIR = Path(__file__).resolve().parent / "static"
ensure_dirs()
store = Store()
corpus = Corpus()
BOOT_INTERRUPTED_JOBS = store.mark_interrupted_jobs()

TRANSCRIBE_MODES = {
    "none",
    "url_only",
    "official",
    "whisper",
    "official_then_whisper",
    "cloud_gpu",
    "official_then_cloud",
}
LOCAL_WHISPER = {"whisper", "official_then_whisper"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = load_settings()
    if BOOT_INTERRUPTED_JOBS and settings.bark_enabled and settings.bark_key:
        n = len(BOOT_INTERRUPTED_JOBS)
        progress = BOOT_INTERRUPTED_JOBS[0].get("progress") or {}
        progress["error"] = f"{n} 个任务"
        await send_bark(
            key=settings.bark_key,
            body=progress_messages("interrupted", progress),
            title=settings.bark_title or "b站爬虫",
            sound=settings.bark_sound or "bell",
            server=settings.bark_server or "https://api.day.app",
            level="timeSensitive",
        )
    yield
    AUTODL_SESSION.close()
    settings = load_settings()
    running = [job for job in JOBS.values() if job.status in {"queued", "running", "cancelling"}]
    if running and settings.bark_enabled and settings.bark_key:
        await send_bark(
            key=settings.bark_key,
            body=progress_messages("interrupted", running[0].progress or {}),
            title=settings.bark_title or "b站爬虫",
            sound=settings.bark_sound or "bell",
            server=settings.bark_server or "https://api.day.app",
            level="timeSensitive",
        )


app = FastAPI(title="BiliArchiver", version="1.0.0", lifespan=lifespan)
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
    bark_enabled: bool | None = None
    bark_key: str | None = None
    bark_title: str | None = None
    bark_sound: str | None = None
    bark_server: str | None = None
    gpu_worker_url: str | None = None
    gpu_worker_token: str | None = None
    compute_backend: str | None = None
    autodl_ssh_command: str | None = None
    autodl_ssh_password: str | None = None


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
    media_keep: str = "upload_then_delete"
    ocr_enabled: bool = True
    resume: bool = True
    compute_backend: str = "local"
    kind: str = "archive"


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
    data["corpus_path"] = str(corpus.path)
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
    payload = body.model_dump(exclude_none=True)
    raw_key = payload.get("bark_key")
    if raw_key is not None:
        cleaned = normalize_bark_key(str(raw_key))
        if cleaned:
            payload["bark_key"] = cleaned
        else:
            payload.pop("bark_key", None)
    raw_gpu_token = payload.get("gpu_worker_token")
    if raw_gpu_token is not None:
        cleaned_token = str(raw_gpu_token).strip()
        if cleaned_token:
            payload["gpu_worker_token"] = cleaned_token
        else:
            payload.pop("gpu_worker_token", None)
    if payload.get("gpu_worker_url") is not None:
        payload["gpu_worker_url"] = normalize_worker_url(str(payload.get("gpu_worker_url") or ""))
    raw_autodl_password = payload.get("autodl_ssh_password")
    if raw_autodl_password is not None:
        cleaned_password = str(raw_autodl_password).strip()
        if cleaned_password:
            payload["autodl_ssh_password"] = cleaned_password
        else:
            payload.pop("autodl_ssh_password", None)
    for key, value in payload.items():
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
    if current.compute_backend not in {"local", "cloud"}:
        raise HTTPException(400, "计算后端无效")
    if current.bark_sound == "":
        current.bark_sound = "bell"
    if current.bark_title == "":
        current.bark_title = "b站爬虫"
    save_settings(current)
    return public_settings(current)


@app.get("/api/capabilities")
async def capabilities() -> dict[str, Any]:
    settings = load_settings()
    usage = shutil.disk_usage(DATA_DIR)
    gpu = probe_compute()
    drive_ok, drive_note = rclone_drive_ready(settings.rclone_remote)
    worker_ok, worker_note = gpu_worker_ready(
        settings.gpu_worker_url, settings.gpu_worker_token, timeout=2.5
    )
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
            "ready": drive_ok,
            "purpose": drive_note,
            "remotes": rclone_remote_names(),
            "binary": which_rclone() or "",
            "remote": settings.rclone_remote,
            "root": settings.rclone_root,
        },
        "gpu_worker": {
            "ready": worker_ok,
            "purpose": worker_note,
            "url": settings.gpu_worker_url or "",
        },
        "autodl": AUTODL_SESSION.status(),
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


class BarkTestIn(BaseModel):
    key: str | None = None


@app.post("/api/notify/test")
async def bark_test(body: BarkTestIn = BarkTestIn()) -> dict[str, Any]:
    settings = load_settings()
    key = normalize_bark_key(body.key or settings.bark_key)
    if not key:
        raise HTTPException(400, "请先填写 Bark Key")
    result = await send_bark(
        key=key,
        body="测试推送：BiliArchiver 已连接 Bark，后续采集进度会发到这里。",
        title=settings.bark_title or "b站爬虫",
        sound=settings.bark_sound or "bell",
        server=settings.bark_server or "https://api.day.app",
    )
    if not result.get("ok"):
        raise HTTPException(400, str(result.get("error") or result.get("data") or "Bark 推送失败"))
    if not settings.bark_key:
        settings.bark_key = key
        settings.bark_enabled = True
        save_settings(settings)
    return {"ok": True, "settings": public_settings(load_settings())}


class GpuTestIn(BaseModel):
    url: str | None = None
    token: str | None = None


@app.post("/api/gpu/test")
async def gpu_test(body: GpuTestIn = GpuTestIn()) -> dict[str, Any]:
    settings = load_settings()
    url = normalize_worker_url(body.url or settings.gpu_worker_url)
    token = (body.token or settings.gpu_worker_token or "").strip()
    if not url:
        raise HTTPException(400, "请先填写 GPU 工作机地址")
    ok, message = gpu_worker_ready(url, token)
    if not ok:
        raise HTTPException(400, message)
    if body.url:
        settings.gpu_worker_url = url
    if body.token:
        settings.gpu_worker_token = token
    if body.url or body.token:
        save_settings(settings)
    return {"ok": True, "message": message, "settings": public_settings(load_settings())}


class AutodlConnectIn(BaseModel):
    ssh_command: str | None = None
    password: str | None = None


class AutodlPutIn(BaseModel):
    path: str


def _require_library_file(raw: str) -> Path:
    local = Path(raw).expanduser()
    try:
        resolved = local.resolve()
        resolved.relative_to(LIBRARY_DIR.resolve())
    except (OSError, ValueError) as exc:
        raise HTTPException(400, "只能上传资料库里的音频") from exc
    if not resolved.is_file():
        raise HTTPException(400, "本地文件不存在")
    return resolved


@app.get("/api/gpu/autodl/status")
async def autodl_status() -> dict[str, Any]:
    return AUTODL_SESSION.status()


@app.post("/api/gpu/autodl/put")
async def autodl_put(body: AutodlPutIn) -> dict[str, Any]:
    if not session_alive():
        raise HTTPException(400, "AutoDL 未接入")
    local = _require_library_file(body.path)
    try:
        remote = await asyncio.to_thread(put_via_session, local)
    except Exception as exc:
        raise HTTPException(500, str(exc).strip() or type(exc).__name__) from exc
    return {"ok": True, "remote": remote, "bytes": local.stat().st_size}


@app.post("/api/gpu/autodl/warmup")
async def autodl_warmup() -> dict[str, Any]:
    if not session_alive():
        raise HTTPException(400, "AutoDL 未接入")
    settings = load_settings()
    url = normalize_worker_url(settings.gpu_worker_url)
    token = (settings.gpu_worker_token or "").strip()
    if not url:
        raise HTTPException(400, "没有 GPU 工作机地址")
    import httpx

    timeout = httpx.Timeout(30.0, read=240.0, write=30.0, pool=30.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.post(
                f"{url}/v1/warmup",
                headers=worker_headers(token),
                data={"model": "large-v3"},
            )
    except Exception as exc:
        raise HTTPException(502, f"预热失败：{exc}") from exc
    if response.status_code in {401, 403}:
        raise HTTPException(400, "GPU 工作机 Token 不正确")
    if response.status_code >= 400:
        raise HTTPException(502, f"预热失败 HTTP {response.status_code} {(response.text or '')[:240]}")
    try:
        data = response.json()
    except Exception:
        data = {}
    if not data.get("ok", True):
        raise HTTPException(502, str(data.get("error") or "预热失败"))
    return data


@app.post("/api/gpu/autodl/disconnect")
async def autodl_disconnect() -> dict[str, Any]:
    AUTODL_SESSION.close()
    return {"ok": True, **AUTODL_SESSION.status()}


@app.post("/api/gpu/autodl/connect")
async def autodl_connect(body: AutodlConnectIn = AutodlConnectIn()) -> StreamingResponse:
    settings = load_settings()
    ssh_command = (body.ssh_command or settings.autodl_ssh_command or "").strip()
    password = (body.password or settings.autodl_ssh_password or "").strip()
    if not ssh_command:
        raise HTTPException(400, "请粘贴 AutoDL 的 SSH 登录指令")
    if not password:
        raise HTTPException(400, "请填写 AutoDL SSH 密码")
    token = (settings.gpu_worker_token or "").strip() or secrets.token_hex(16)
    # 本机 Intel Mac 默认 small；租 GPU 就是为了跑 large-v3，不要沿用本机模型档。
    model = "large-v3"
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def emit(message: str, level: str = "info") -> None:
        loop.call_soon_threadsafe(queue.put_nowait, {"line": message, "level": level})

    def worker() -> None:
        try:
            result = connect_autodl(
                ssh_command=ssh_command,
                password=password,
                token=token,
                model=model,
                log=emit,
            )
            current = load_settings()
            current.autodl_ssh_command = ssh_command
            current.autodl_ssh_password = password
            current.gpu_worker_token = token
            current.gpu_worker_url = str(result["url"])
            current.compute_backend = "cloud"
            save_settings(current)
            emit("接入完成。按 UP 主采集选文字研究或完整归档，步骤 6 选云端 GPU。", "ok")
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {
                    "done": True,
                    "ok": True,
                    "message": "AutoDL 已接入",
                    "url": result["url"],
                    "settings": public_settings(load_settings()),
                },
            )
        except Exception as exc:
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"done": True, "ok": False, "error": str(exc), "level": "error"},
            )

    threading.Thread(target=worker, daemon=True, name="autodl-connect").start()

    async def stream():
        while True:
            item = await queue.get()
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            if item.get("done"):
                break

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/jobs")
async def create_job(body: JobIn) -> dict[str, Any]:
    uids = parse_uids(body.uids_text)
    if not uids:
        raise HTTPException(400, "请填写至少一个 UID 或空间链接")
    if body.time_range not in {"all", "3y", "2y", "1y", "9m", "6m", "3m"}:
        raise HTTPException(400, "时间范围无效")
    if body.media_mode not in {"none", "link", "audio", "video"}:
        raise HTTPException(400, "媒体策略无效")
    if body.transcribe_mode not in TRANSCRIBE_MODES:
        raise HTTPException(400, "转写策略无效")
    transcribe_mode, compute_backend = resolve_compute(body.transcribe_mode, body.compute_backend)
    if transcribe_mode in LOCAL_WHISPER and body.media_mode not in {"audio", "video"}:
        raise HTTPException(400, "语音识别需要音频：请把媒体策略改为“下载音频”或“下载视频”")
    if needs_remote_models(transcribe_mode, compute_backend, body.ocr_enabled):
        settings_now = load_settings()
        ok, message = gpu_worker_ready(settings_now.gpu_worker_url, settings_now.gpu_worker_token)
        if not ok:
            raise HTTPException(400, message)
    if body.media_keep not in KEEP_POLICIES:
        raise HTTPException(400, "空间策略无效")
    if body.media_keep == "upload_then_delete":
        settings_now = load_settings()
        ok, message = rclone_drive_ready(settings_now.rclone_remote)
        if not ok:
            raise HTTPException(400, message)
    job_id = uuid.uuid4().hex[:12]
    config = body.model_dump()
    settings = load_settings()
    config["uids"] = uids
    config["transcribe_mode"] = transcribe_mode
    config["compute_backend"] = compute_backend
    config["rclone_remote"] = settings.rclone_remote
    config["rclone_root"] = settings.rclone_root
    config["kind"] = "archive"
    runtime = JobRuntime(job_id, config)
    JOBS[job_id] = runtime
    store.save_job(job_id, config, "queued", {})
    runtime.task = asyncio.create_task(_run_job(runtime))
    return {"id": job_id, "uids": uids, "status": "queued"}


class AcademicJobIn(BaseModel):
    seeds_text: str
    max_depth: int = 2
    max_nodes: int = 80
    related_limit: int = 15
    min_views: int = 10000
    min_replies: int = 150
    min_engagement: float = 0.003
    category_allow: str = ""
    category_deny: str = "游戏,动画,番剧,国创,音乐,舞蹈,影视,娱乐,鬼畜,运动,汽车,时尚,美食"
    tag_terms: str = "就业,学历,失业,薪资,找工作,文凭,体制内,内卷,大厂,职场"
    keyword: str = ""
    time_range: str = "1y"
    seeds_per_uid: int = 8
    crawl_comments: bool = True
    crawl_danmaku: bool = False
    transcribe_mode: str = "official_then_whisper"
    media_mode: str = "audio"
    media_keep: str = "delete_after_text"
    resume: bool = True
    compute_backend: str = "local"


@app.post("/api/jobs/academic")
async def create_academic_job(body: AcademicJobIn) -> dict[str, Any]:
    bvids, uids = parse_academic_seeds(body.seeds_text)
    if not bvids and not uids:
        raise HTTPException(400, "请填写种子 BV 号，或 UP 主 UID / 空间链接")
    if body.time_range not in {"all", "3y", "2y", "1y", "9m", "6m", "3m"}:
        raise HTTPException(400, "时间范围无效")
    if body.max_depth < 0 or body.max_depth > 4:
        raise HTTPException(400, "深度请放在 0–4")
    if body.max_nodes < 1 or body.max_nodes > 500:
        raise HTTPException(400, "节点上限请放在 1–500")
    if body.media_mode not in {"none", "link", "audio", "video"}:
        raise HTTPException(400, "媒体策略无效")
    if body.transcribe_mode not in TRANSCRIBE_MODES:
        raise HTTPException(400, "转写策略无效")
    transcribe_mode, compute_backend = resolve_compute(body.transcribe_mode, body.compute_backend)
    if transcribe_mode in LOCAL_WHISPER and body.media_mode not in {"audio", "video"}:
        raise HTTPException(400, "语音识别需要音频：请把媒体策略改为“下载音频”或“下载视频”")
    if needs_remote_models(transcribe_mode, compute_backend, False):
        settings_now = load_settings()
        ok, message = gpu_worker_ready(settings_now.gpu_worker_url, settings_now.gpu_worker_token)
        if not ok:
            raise HTTPException(400, message)
    if body.media_keep not in KEEP_POLICIES:
        raise HTTPException(400, "空间策略无效")
    if body.media_keep == "upload_then_delete":
        settings_now = load_settings()
        ok, message = rclone_drive_ready(settings_now.rclone_remote)
        if not ok:
            raise HTTPException(400, message)
    job_id = uuid.uuid4().hex[:12]
    config = body.model_dump()
    config["kind"] = "academic"
    config["seed_bvids"] = bvids
    config["seed_uids"] = uids
    config["transcribe_mode"] = transcribe_mode
    config["compute_backend"] = compute_backend
    settings = load_settings()
    config["rclone_remote"] = settings.rclone_remote
    config["rclone_root"] = settings.rclone_root
    runtime = JobRuntime(job_id, config)
    JOBS[job_id] = runtime
    store.save_job(job_id, config, "queued", {})
    runtime.task = asyncio.create_task(_run_job(runtime))
    return {"id": job_id, "bvids": bvids, "uids": uids, "status": "queued"}


class TranscribeJobIn(BaseModel):
    bvids_text: str
    transcribe_mode: str = "official_then_whisper"
    media_mode: str = "video"
    media_keep: str = "upload_then_delete"
    resume: bool = True
    compute_backend: str = "cloud"
    crawl_comments: bool = False
    attach_run_id: str = ""


@app.get("/api/jobs/transcribe/missing")
async def missing_transcripts(run_id: str = "5ba1f9a96259") -> dict[str, Any]:
    from bili.transcribe import folder_transcript_has_text

    bvids = corpus.missing_transcript_bvids(run_id)
    folders: dict[str, Path] = {}
    for path in LIBRARY_DIR.rglob("*"):
        if not path.is_dir() or "/videos/" not in str(path) or path.name in {"media", "parts"}:
            continue
        for part in path.name.split("_"):
            if part.startswith("BV") and len(part) >= 12:
                folders[part] = path
                break
    pending: list[str] = []
    skipped_done = 0
    for bvid in bvids:
        folder = folders.get(bvid)
        if folder and folder_transcript_has_text(folder):
            skipped_done += 1
            continue
        pending.append(bvid)
    return {
        "run_id": run_id,
        "pending": pending,
        "count": len(pending),
        "already_on_disk": skipped_done,
        "text": "\n".join(pending),
    }


@app.post("/api/jobs/transcribe")
async def create_transcribe_job(body: TranscribeJobIn) -> dict[str, Any]:
    from bili.util import parse_bvids

    bvids = parse_bvids(body.bvids_text)
    if not bvids:
        raise HTTPException(400, "请填写至少一个 BV 号或视频链接")
    if body.media_mode not in {"none", "link", "audio", "video"}:
        raise HTTPException(400, "媒体策略无效")
    if body.transcribe_mode not in TRANSCRIBE_MODES:
        raise HTTPException(400, "转写策略无效")
    transcribe_mode, compute_backend = resolve_compute(body.transcribe_mode, body.compute_backend)
    if transcribe_mode in LOCAL_WHISPER and body.media_mode not in {"audio", "video"}:
        raise HTTPException(400, "语音识别需要音频：请把媒体策略改为“下载音频”或“下载视频”")
    if needs_remote_models(transcribe_mode, compute_backend, True):
        settings_now = load_settings()
        ok, message = gpu_worker_ready(settings_now.gpu_worker_url, settings_now.gpu_worker_token)
        if not ok:
            raise HTTPException(400, "请先到设置里接入 AutoDL GPU。\n" + message)
    if body.media_keep not in KEEP_POLICIES:
        raise HTTPException(400, "空间策略无效")
    if body.media_keep == "upload_then_delete":
        settings_now = load_settings()
        ok, message = rclone_drive_ready(settings_now.rclone_remote)
        if not ok:
            raise HTTPException(400, message)
    job_id = uuid.uuid4().hex[:12]
    settings = load_settings()
    config = {
        "kind": "transcribe",
        "skip_gate": True,
        "seeds_text": body.bvids_text,
        "seed_bvids": bvids,
        "seed_uids": [],
        "max_depth": 0,
        "max_nodes": len(bvids),
        "related_limit": 0,
        "min_views": 0,
        "min_replies": 0,
        "min_engagement": 0,
        "category_allow": "",
        "category_deny": "",
        "tag_terms": "",
        "keyword": "",
        "time_range": "all",
        "seeds_per_uid": 0,
        "crawl_comments": bool(body.crawl_comments),
        "crawl_danmaku": False,
        "transcribe_mode": transcribe_mode,
        "media_mode": body.media_mode,
        "media_keep": body.media_keep,
        "resume": bool(body.resume),
        "compute_backend": compute_backend,
        "ocr_enabled": True,
        "attach_run_id": (body.attach_run_id or "").strip(),
        "rclone_remote": settings.rclone_remote,
        "rclone_root": settings.rclone_root,
    }
    runtime = JobRuntime(job_id, config)
    JOBS[job_id] = runtime
    store.save_job(job_id, config, "queued", {})
    runtime.task = asyncio.create_task(_run_job(runtime))
    return {"id": job_id, "bvids": bvids, "status": "queued"}
    sql: str


@app.get("/api/corpus")
async def corpus_summary() -> dict[str, Any]:
    runs = corpus.runs(20)
    funnel: list[dict[str, Any]] = []
    for run in runs:
        rows = corpus.gate_funnel(run["run_id"])
        if rows:
            funnel = [{"run_id": run["run_id"], **row} for row in rows]
            break
    return {
        "path": str(corpus.path),
        "counts": corpus.table_counts(),
        "runs": runs,
        "funnel": funnel,
    }


@app.post("/api/corpus/query")
async def corpus_query(body: CorpusQueryIn) -> dict[str, Any]:
    try:
        rows = corpus.query(body.sql)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, f"查询失败：{exc}") from exc
    return {"rows": rows[:500], "count": len(rows)}


@app.post("/api/corpus/export")
async def corpus_export() -> dict[str, Any]:
    return await asyncio.to_thread(corpus.export_bundle)


@app.post("/api/corpus/ingest")
async def corpus_ingest() -> dict[str, Any]:
    run_id = "backfill-" + uuid.uuid4().hex[:8]
    counts = await asyncio.to_thread(ingest_library, LIBRARY_DIR, corpus, run_id)
    return {"ok": True, "run_id": run_id, "counts": counts}


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


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str) -> dict[str, Any]:
    busy = [item for item in JOBS.values() if item.status in {"queued", "running", "cancelling"}]
    if busy:
        raise HTTPException(400, "已有任务在跑。请先取消或等它结束，再续跑。")
    live = JOBS.get(job_id)
    recorded = store.get_job(job_id)
    # Prefer on-disk config so manual limit bumps (e.g. max_nodes) take effect on resume.
    config = ((recorded or {}).get("config") if recorded else None) or (live.config if live else None)
    if not config:
        raise HTTPException(404, "任务不存在，无法续跑")
    if (config.get("kind") or "archive") not in {"academic", "transcribe", "archive"}:
        raise HTTPException(400, "这种任务不能续跑")
    config = dict(config)
    config["resume"] = True
    runtime = JobRuntime(job_id, config)
    JOBS[job_id] = runtime
    store.save_job(job_id, config, "queued", (recorded or {}).get("progress") or {})
    runtime.task = asyncio.create_task(_run_job(runtime))
    return {"id": job_id, "status": "queued", "kind": config.get("kind")}


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
        corpus=corpus,
    )

    def on_log(level: str, message: str) -> None:
        item = {"level": level, "message": message}
        job.logs.append(item)
        job.emit("log", item)

    crawler.on_log = on_log
    cfg = JobConfig(
        uids=job.config.get("uids") or [],
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
        rclone_remote=job.config.get("rclone_remote") or settings.rclone_remote,
        rclone_root=job.config.get("rclone_root") or settings.rclone_root,
        compute_backend=job.config.get("compute_backend") or settings.compute_backend,
    )

    async def bark(event: str, payload: dict[str, Any] | None = None) -> None:
        current = load_settings()
        if not current.bark_enabled or not current.bark_key:
            if event in {"start", "done", "error", "cancelled"}:
                on_log("warn", "未发送 Bark：请在设置里保存 Key 并保持启用")
            return
        level = "timeSensitive" if event in {"done", "error", "cancelled", "interrupted"} else "active"
        result = await send_bark(
            key=current.bark_key,
            body=progress_messages(event, payload),
            title=current.bark_title or "b站爬虫",
            sound=current.bark_sound or "bell",
            server=current.bark_server or "https://api.day.app",
            level=level,
        )
        if event in {"start", "done", "error", "cancelled"}:
            if result.get("ok"):
                on_log("info", f"已发送 Bark（{event}）")
            else:
                on_log("warn", f"Bark 推送失败：{result.get('error') or result.get('data') or result.get('status')}")
        elif not result.get("ok") and not result.get("skipped"):
            on_log("warn", f"Bark 推送失败：{result.get('error') or result.get('data') or result.get('status')}")

    async def on_progress(payload: dict[str, Any]) -> None:
        nonlocal last_videos, last_uids
        if job.cancel:
            crawler.cancelled = True
        job.progress = payload
        job.emit("progress", payload)
        store.save_job(job.id, job.config, job.status, payload, job.error)
        videos = int(payload.get("videos_done") or 0)
        uids_done = int(payload.get("uids_done") or 0)
        if videos > last_videos:
            last_videos = videos
            await bark("video", payload)
        if uids_done > last_uids:
            last_uids = uids_done
            await bark("uid", payload)

    last_videos = 0
    last_uids = 0
    job.status = "running"
    if job.config.get("kind") in {"academic", "transcribe"}:
        transcribe_job = job.config.get("kind") == "transcribe" or bool(job.config.get("skip_gate"))
        on_log(
            "info",
            (
                f"按视频号采集启动 · {len(job.config.get('seed_bvids') or [])} 条 · "
                f"Whisper + 无声画面 OCR"
                if transcribe_job
                else f"学术滚雪球启动 · 种子视频 {len(job.config.get('seed_bvids') or [])} · "
                f"账号 {len(job.config.get('seed_uids') or [])} · 深度 {job.config.get('max_depth')}"
            ),
        )
        await bark("start", {"uids_total": job.config.get("max_nodes") or 0})
        academic = AcademicCrawler(
            settings=settings,
            store=store,
            corpus=corpus,
            crawler=crawler,
            on_log=on_log,
            should_cancel=lambda: job.cancel,
        )
        cfg_ac = AcademicConfig(
            job_id=job.id,
            seeds_text=job.config.get("seeds_text") or "",
            seed_bvids=job.config.get("seed_bvids") or [],
            seed_uids=job.config.get("seed_uids") or [],
            max_depth=0 if transcribe_job else int(job.config.get("max_depth") or 2),
            max_nodes=int(job.config.get("max_nodes") or 80),
            related_limit=0 if transcribe_job else int(job.config.get("related_limit") or 15),
            min_views=int(job.config.get("min_views") or 0),
            min_replies=int(job.config.get("min_replies") or 0),
            min_engagement=float(job.config.get("min_engagement") or 0),
            category_allow=job.config.get("category_allow") or "",
            category_deny=job.config.get("category_deny") or "",
            tag_terms=job.config.get("tag_terms") or "",
            keyword=job.config.get("keyword") or "",
            time_range=job.config.get("time_range") or "1y",
            seeds_per_uid=int(job.config.get("seeds_per_uid") or 8),
            crawl_comments=bool(job.config.get("crawl_comments", True)),
            crawl_danmaku=bool(job.config.get("crawl_danmaku", False)),
            transcribe_mode=job.config.get("transcribe_mode") or "official_then_whisper",
            media_mode=job.config.get("media_mode") or "audio",
            media_keep=job.config.get("media_keep") or "delete_after_text",
            ocr_enabled=bool(job.config.get("ocr_enabled", transcribe_job)),
            resume=bool(job.config.get("resume", True)),
            compute_backend=job.config.get("compute_backend") or "local",
            rclone_remote=job.config.get("rclone_remote") or settings.rclone_remote,
            rclone_root=job.config.get("rclone_root") or settings.rclone_root,
            skip_gate=transcribe_job,
        )
        try:
            await academic.run(cfg_ac, on_progress=on_progress)
            job.status = "cancelled" if job.cancel or academic.cancelled else "done"
            done_msg = "按视频号采集结束" if transcribe_job else "学术滚雪球结束"
            on_log("ok", done_msg if job.status == "done" else "已取消")
        except asyncio.CancelledError:
            job.status = "cancelled"
            on_log("warn", "任务已取消；队列和已采集节点已保留")
        except Exception as exc:
            if job.cancel:
                job.status = "cancelled"
                on_log("warn", "任务已取消；已完成步骤已保留")
            else:
                job.status = "error"
                job.error = str(exc)
                on_log("error", f"任务失败：{exc}")
        store.save_job(job.id, job.config, job.status, job.progress, job.error)
        finish = dict(job.progress or {})
        finish["error"] = job.error
        await bark("cancelled" if job.status == "cancelled" else ("error" if job.status == "error" else "done"), finish)
        job.emit("done", {"status": job.status, "progress": job.progress, "error": job.error})
        return

    on_log("info", f"任务启动 · {len(cfg.uids)} 个账号 · 范围 {cfg.time_range} · 算力 {cfg.compute_backend}")
    await bark("start", {"uids_total": len(cfg.uids)})
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
    finish = dict(job.progress or {})
    finish["error"] = job.error
    await bark("cancelled" if job.status == "cancelled" else ("error" if job.status == "error" else "done"), finish)
    job.emit("done", {"status": job.status, "progress": job.progress, "error": job.error})


class ReclaimIn(BaseModel):
    policy: str = "delete_after_text"
    dry_run: bool = False


@app.post("/api/library/reclaim")
async def reclaim_existing(body: ReclaimIn) -> dict[str, Any]:
    if body.policy not in KEEP_POLICIES:
        raise HTTPException(400, "空间策略无效")
    if body.policy == "upload_then_delete":
        settings = load_settings()
        ok, message = rclone_drive_ready(settings.rclone_remote)
        if not ok:
            raise HTTPException(400, message)
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


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
