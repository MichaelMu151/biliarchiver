from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Iterable

from bili.media import which_ffmpeg
from bili.runtime import run_quiet
from bili.util import now_iso, write_json

KEEP_POLICIES = ("keep", "delete_after_text", "compress", "upload_then_delete")
KEEP_LABELS = {
    "keep": "保留本地文件",
    "delete_after_text": "提取文字后删除，只留链接",
    "compress": "压缩后保留",
    "upload_then_delete": "上传 Google Drive 后删除本地媒体",
}
MEDIA_SUFFIXES = {".mp4", ".m4a", ".m4s", ".flv", ".webm", ".mkv", ".part"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
TEXT_KEEP = {
    "transcript.md",
    "transcript.json",
    "transcript.srt",
    "transcript.vtt",
    "video.md",
    "dynamic.md",
    "ocr.md",
    "ocr.json",
    "meta.json",
    "comments.jsonl",
    "danmaku.jsonl",
    "cloud_job.json",
    "storage.json",
    ".pipeline.json",
}

LogFn = Callable[[str, str], None]


def which_rclone(explicit: str = "rclone") -> str | None:
    names = [explicit or "rclone"]
    if os.name == "nt" and not names[0].lower().endswith(".exe"):
        names.append(names[0] + ".exe")
    candidates: list[Path] = []
    for name in names:
        if name:
            candidates.append(Path(name).expanduser())
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    root = Path(__file__).resolve().parent.parent
    extra = [
        root / "tools" / "bin" / "rclone.exe",
        root / "tools" / "bin" / "rclone",
        Path("/usr/local/bin/rclone"),
        Path("/opt/homebrew/bin/rclone"),
        Path("/usr/local/opt/rclone/bin/rclone"),
    ]
    cellar = Path("/usr/local/Cellar/rclone")
    if cellar.exists():
        extra.extend(sorted(cellar.glob("*/bin/rclone"), reverse=True))
    cellar = Path("/opt/homebrew/Cellar/rclone")
    if cellar.exists():
        extra.extend(sorted(cellar.glob("*/bin/rclone"), reverse=True))
    candidates.extend(extra)
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def rclone_drive_ready(remote: str) -> tuple[bool, str]:
    """Confirm the named remote can actually talk to Google Drive."""
    binary = which_rclone()
    if not binary:
        return False, "未找到 rclone"
    remotes = rclone_remote_names()
    if remote not in remotes:
        shown = "、".join(remotes) or "无"
        return False, f"没有名为 {remote} 的远程。当前：{shown}"
    proc = run_quiet([binary, "lsd", f"{remote}:", "--max-depth", "1"])
    if proc.returncode == 0:
        return True, "已连接 Google Drive"
    text = proc.stderr or proc.stdout or ""
    if "SERVICE_DISABLED" in text or "accessNotConfigured" in text:
        return False, (
            "Google Drive API 尚未启用。请打开 "
            "https://console.cloud.google.com/apis/library/drive.googleapis.com "
            "点“启用”，等一两分钟后再试"
        )
    return False, (text.strip() or "rclone 无法访问该远程")[-400:]


def rclone_remote_names() -> list[str]:
    binary = which_rclone()
    if not binary:
        return []
    proc = run_quiet([binary, "listremotes"])
    if proc.returncode != 0:
        return []
    names = []
    for line in (proc.stdout or "").splitlines():
        name = line.strip().rstrip(":")
        if name:
            names.append(name)
    return names


def should_fetch_media(
    media_mode: str,
    media_keep: str,
    transcribe_mode: str,
    transcript_done: bool,
) -> str:
    """Decide what to download so bulky files are not fetched only to be deleted."""
    if media_mode in {"none", "link"}:
        return media_mode
    if transcript_done and media_keep == "delete_after_text":
        return "link"
    if (
        media_mode == "video"
        and media_keep == "delete_after_text"
        and transcribe_mode in {
            "whisper",
            "official_then_whisper",
            "cloud_gpu",
            "official_then_cloud",
        }
        and not transcript_done
    ):
        return "audio"
    return media_mode


def _iter_bulky_files(folder: Path, *, images: bool = True, media: bool = True) -> list[Path]:
    found: list[Path] = []
    if not folder.exists():
        return found
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        if path.name in TEXT_KEEP or path.name.startswith("."):
            if path.suffix.lower() != ".part" and not path.name.endswith(".tmp"):
                continue
        suffix = path.suffix.lower()
        if media and (suffix in MEDIA_SUFFIXES or path.name.endswith(".tmp") or path.name.endswith(".part")):
            found.append(path)
        elif images and suffix in IMAGE_SUFFIXES:
            found.append(path)
    return found


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def load_storage_state(folder: Path) -> dict[str, Any]:
    path = folder / "storage.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def media_already_released(folder: Path) -> bool:
    state = load_storage_state(folder)
    return bool(state.get("released"))


def _relative_to(library_root: Path | None, path: Path) -> str:
    if not library_root:
        return path.name
    try:
        return str(path.resolve().relative_to(library_root.resolve()))
    except Exception:
        return path.name


def rclone_upload(
    paths: Iterable[Path],
    *,
    remote: str,
    root: str,
    library_root: Path | None,
    on_log: LogFn,
    dry_run: bool = False,
) -> list[str]:
    binary = which_rclone()
    if not binary:
        raise RuntimeError("未找到 rclone。请先安装：brew install rclone，再执行 rclone config")
    uploaded: list[str] = []
    remote = (remote or "gdrive").strip()
    root = (root or "BiliArchiver").strip().strip("/")
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        dest = f"{remote}:{root}/{_relative_to(library_root, path)}"
        if dry_run:
            uploaded.append(dest)
            continue
        proc = run_quiet([binary, "copyto", str(path), dest, "--retries", "5"])
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "rclone 上传失败")[-400:])
        uploaded.append(dest)
        on_log("ok", f"已上传 {path.name} → {dest}")
    return uploaded


def _compress_video(src: Path, ffmpeg: str, dry_run: bool) -> Path | None:
    if src.suffix.lower() not in {".mp4", ".mkv", ".webm", ".flv"}:
        return None
    dest = src.with_name(src.stem + ".small.mp4")
    if dry_run:
        return dest
    cmd = [
        ffmpeg, "-y", "-i", str(src),
        "-vf", "scale=-2:480",
        "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        str(dest),
    ]
    proc = run_quiet(cmd)
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        return None
    return dest


def _compress_image(src: Path, dry_run: bool) -> Path | None:
    if src.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    dest = src.with_name(src.stem + ".small.jpg")
    if dry_run:
        return dest
    from PIL import Image, ImageOps

    with Image.open(src) as image:
        rgb = ImageOps.exif_transpose(image).convert("RGB")
        max_side = max(rgb.size)
        if max_side > 1280:
            scale = 1280 / max_side
            rgb = rgb.resize(
                (max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))),
                Image.Resampling.LANCZOS,
            )
        rgb.save(dest, format="JPEG", quality=70, optimize=True)
    return dest if dest.exists() else None


def _unlink(path: Path, dry_run: bool) -> int:
    size = _file_size(path)
    if not dry_run:
        path.unlink(missing_ok=True)
    return size


def reclaim_folder(
    folder: Path,
    *,
    policy: str,
    page_url: str = "",
    transcript_ready: bool = False,
    ocr_ready: bool = False,
    ffmpeg_path: str = "ffmpeg",
    rclone_remote: str = "gdrive",
    rclone_root: str = "BiliArchiver",
    library_root: Path | None = None,
    on_log: LogFn | None = None,
    dry_run: bool = False,
    include_images: bool = True,
    include_media: bool = True,
) -> dict[str, Any]:
    """Shrink a video or dynamic folder after text extraction."""
    log = on_log or (lambda *_a, **_k: None)
    policy = policy if policy in KEEP_POLICIES else "keep"
    result: dict[str, Any] = {
        "policy": policy,
        "released": False,
        "page_url": page_url,
        "drive_uris": [],
        "bytes_freed": 0,
        "note": "",
        "updated_at": now_iso(),
    }
    if policy == "keep":
        result["note"] = "保留本地媒体"
        if not dry_run:
            write_json(folder / "storage.json", result)
        return result

    bulky = _iter_bulky_files(folder, images=include_images, media=include_media)
    if include_media and not transcript_ready:
        bulky = [path for path in bulky if path.suffix.lower() in IMAGE_SUFFIXES]
    if include_images and not ocr_ready:
        bulky = [path for path in bulky if path.suffix.lower() not in IMAGE_SUFFIXES]
    if not bulky:
        prev = load_storage_state(folder)
        if prev.get("released"):
            return prev
        result["note"] = "没有可回收的媒体文件"
        if not dry_run:
            write_json(folder / "storage.json", result)
        return result

    before = sum(_file_size(path) for path in bulky)
    drive_uris: list[str] = []

    if policy == "upload_then_delete":
        try:
            drive_uris = rclone_upload(
                bulky,
                remote=rclone_remote,
                root=rclone_root,
                library_root=library_root,
                on_log=log,
                dry_run=dry_run,
            )
        except Exception as exc:
            result["note"] = f"云盘上传失败，已保留本地文件：{exc}"
            log("warn", result["note"])
            if not dry_run:
                write_json(folder / "storage.json", result)
            return result
        freed = 0
        for path in bulky:
            freed += _unlink(path, dry_run)
        result["released"] = True
        result["drive_uris"] = drive_uris
        result["bytes_freed"] = freed
        result["note"] = "媒体已上传到 Google Drive，本地大文件已删除；可用页面链接或云盘回看"
        log("ok", f"已释放 {freed / 1024 / 1024:.1f} MB · {folder.name}")
    elif policy == "compress":
        ffmpeg = which_ffmpeg(ffmpeg_path)
        freed = 0
        kept: list[str] = []
        for path in bulky:
            replacement: Path | None = None
            if path.suffix.lower() in {".mp4", ".mkv", ".webm", ".flv"}:
                if not ffmpeg:
                    log("warn", "未找到 ffmpeg，无法压缩视频，已跳过")
                    continue
                replacement = _compress_video(path, ffmpeg, dry_run)
            elif path.suffix.lower() in IMAGE_SUFFIXES:
                replacement = _compress_image(path, dry_run)
            elif path.suffix.lower() in {".m4a", ".m4s", ".part"} or path.name.endswith(".tmp"):
                freed += _unlink(path, dry_run)
                continue
            if replacement is None:
                continue
            new_size = 0 if dry_run else _file_size(replacement)
            old_size = _file_size(path)
            if dry_run or (new_size and new_size < old_size):
                freed += max(0, old_size - new_size)
                if not dry_run:
                    final = path.with_name(path.stem + (".mp4" if replacement.suffix == ".mp4" else ".jpg"))
                    path.unlink(missing_ok=True)
                    if final.exists() and final != replacement:
                        final.unlink()
                    os.replace(replacement, final)
                    kept.append(str(final))
            elif not dry_run:
                replacement.unlink(missing_ok=True)
        result["released"] = False
        result["bytes_freed"] = max(0, freed)
        result["note"] = "已压缩媒体并删除中间文件"
        result["kept"] = kept
        log("ok", f"压缩回收 {freed / 1024 / 1024:.1f} MB · {folder.name}")
    else:
        freed = 0
        for path in bulky:
            freed += _unlink(path, dry_run)
        media_dir = folder / "media"
        image_dir = folder / "images"
        if not dry_run:
            if media_dir.exists() and not any(media_dir.iterdir()):
                media_dir.rmdir()
            if include_images and image_dir.exists() and not any(image_dir.iterdir()):
                image_dir.rmdir()
        result["released"] = True
        result["bytes_freed"] = freed or before
        result["note"] = "本地音视频/图片已删除，请用页面链接回看原内容"
        log("ok", f"已删除媒体 {result['bytes_freed'] / 1024 / 1024:.1f} MB · {folder.name}")

    if not dry_run:
        write_json(folder / "storage.json", result)
        cloud_job_path = folder / "cloud_job.json"
        if cloud_job_path.exists():
            try:
                job = json.loads(cloud_job_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                job = {}
            job["local_audio"] = "" if result.get("released") else job.get("local_audio", "")
            job["local_video"] = "" if result.get("released") else job.get("local_video", "")
            job["media_released"] = bool(result.get("released"))
            job["storage_policy"] = policy
            job["page_url"] = page_url or job.get("page_url") or ""
            job["drive_uris"] = drive_uris
            write_json(cloud_job_path, job)
    return result


def reclaim_library(
    library: Path,
    *,
    policy: str,
    ffmpeg_path: str = "ffmpeg",
    rclone_remote: str = "gdrive",
    rclone_root: str = "BiliArchiver",
    on_log: LogFn | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply a keep-policy to every completed video/dynamic already on disk."""
    log = on_log or (lambda *_a, **_k: None)
    summary = {"folders": 0, "bytes_freed": 0, "skipped_running": 0, "errors": 0}
    if not library.exists():
        return summary
    for pipeline_path in library.rglob(".pipeline.json"):
        folder = pipeline_path.parent
        try:
            stages = json.loads(pipeline_path.read_text(encoding="utf-8")).get("stages") or {}
        except (OSError, json.JSONDecodeError):
            continue
        running = any((item or {}).get("status") == "running" for item in stages.values())
        if running:
            summary["skipped_running"] += 1
            continue
        transcript_ready = (folder / "transcript.md").exists() or (stages.get("transcript") or {}).get("status") == "done"
        ocr_ready = (folder / "ocr.json").exists() or (stages.get("ocr") or {}).get("status") == "done"
        is_dynamic = (folder / "dynamic.md").exists() or "ocr" in stages
        is_video = (folder / "video.md").exists() or "media" in stages
        if not is_video and not is_dynamic:
            continue
        page_url = ""
        meta_path = folder / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                page_url = meta.get("page_url") or meta.get("jump_url") or ""
                if not page_url and meta.get("bvid"):
                    page_url = f"https://www.bilibili.com/video/{meta['bvid']}"
            except (OSError, json.JSONDecodeError):
                pass
        try:
            item = reclaim_folder(
                folder,
                policy=policy,
                page_url=page_url,
                transcript_ready=transcript_ready if is_video else True,
                ocr_ready=ocr_ready if is_dynamic else True,
                ffmpeg_path=ffmpeg_path,
                rclone_remote=rclone_remote,
                rclone_root=rclone_root,
                library_root=library,
                on_log=log,
                dry_run=dry_run,
                include_images=is_dynamic,
                include_media=is_video,
            )
            summary["folders"] += 1
            summary["bytes_freed"] += int(item.get("bytes_freed") or 0)
        except Exception as exc:
            summary["errors"] += 1
            log("warn", f"回收失败 {folder}: {exc}")
    return summary
