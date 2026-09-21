from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from bili.client import BiliClient
from bili.util import write_json


def which_ffmpeg(explicit: str = "ffmpeg") -> str | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
        found = shutil.which(explicit)
        if found:
            candidates.append(Path(found))
    candidates.append(Path(__file__).resolve().parent.parent / "tools" / "bin" / "ffmpeg")
    which_default = shutil.which("ffmpeg")
    if which_default:
        candidates.append(Path(which_default))
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def _urls(stream: dict[str, Any]) -> list[str]:
    values = [
        stream.get("baseUrl") or stream.get("base_url") or stream.get("url") or "",
        *(stream.get("backupUrl") or stream.get("backup_url") or []),
    ]
    return list(dict.fromkeys(url for url in values if url))


def select_stream_candidates(play: dict[str, Any], qn: int) -> tuple[list[str], list[str]]:
    dash = play.get("dash") or {}
    videos = dash.get("video") or []
    audios = dash.get("audio") or []
    video_urls: list[str] = []
    if videos:
        at_or_below = [x for x in videos if int(x.get("id") or 0) <= qn]
        selected = max(at_or_below or videos, key=lambda x: int(x.get("id") or 0))
        video_urls = _urls(selected)
    audio_urls: list[str] = []
    if audios:
        audios = sorted(audios, key=lambda x: int(x.get("bandwidth") or 0), reverse=True)
        audio_urls = _urls(audios[0])
    if not audio_urls:
        durl = play.get("durl") or []
        if durl:
            video_urls = _urls(durl[0]) or video_urls
    return video_urls, audio_urls


def select_streams(play: dict[str, Any], qn: int) -> tuple[str, str]:
    video_urls, audio_urls = select_stream_candidates(play, qn)
    return (video_urls[0] if video_urls else "", audio_urls[0] if audio_urls else "")


async def _run_ffmpeg(cmd: list[str]) -> tuple[bool, str]:
    proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
    error = (proc.stderr or "")[-1200:]
    return proc.returncode == 0, error


def _existing_file(*paths: Path) -> Path | None:
    return next((path for path in paths if path.exists() and path.stat().st_size > 0), None)


async def fetch_media(
    client: BiliClient,
    *,
    bvid: str,
    cid: int,
    folder: Path,
    mode: str,
    qn: int,
    ffmpeg_path: str,
    on_log,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    page_url = f"https://www.bilibili.com/video/{bvid}"
    result: dict[str, Any] = {
        "mode": mode,
        "status": "skipped",
        "local_audio": "",
        "local_video": "",
        "page_url": page_url,
        "note": "",
    }
    cloud_job = {
        "schema_version": 2,
        "bvid": bvid,
        "cid": cid,
        "page_url": page_url,
        "media_mode": mode,
        "local_audio": "",
        "local_video": "",
        "cloud_instruction": "云端应使用 page_url 重新解析媒体，或上传 local_audio；不要依赖会过期的 playurl。",
    }
    if mode == "none":
        result["note"] = "未请求播放地址，也未下载媒体"
        write_json(folder / "cloud_job.json", cloud_job)
        return result
    if mode == "link":
        result["status"] = "linked"
        result["note"] = "仅保留永久页面链接；未请求容易过期的媒体直链"
        write_json(folder / "cloud_job.json", cloud_job)
        return result

    media_dir = folder / "media"
    ffmpeg = which_ffmpeg(ffmpeg_path)
    existing_audio = _existing_file(
        media_dir / f"{bvid}.m4a",
        media_dir / f"{bvid}.audio.m4s",
    )
    existing_video = _existing_file(media_dir / f"{bvid}.mp4")
    if existing_audio:
        result["local_audio"] = str(existing_audio)
    if existing_video:
        result["local_video"] = str(existing_video)
    if mode == "audio" and existing_audio:
        result["status"] = "downloaded"
        result["note"] = "复用已下载音频"
        cloud_job.update(result)
        write_json(folder / "cloud_job.json", cloud_job)
        return result
    if mode == "video" and existing_video:
        result["status"] = "downloaded"
        result["note"] = "复用已合成视频"
        cloud_job.update(result)
        write_json(folder / "cloud_job.json", cloud_job)
        return result

    try:
        play = await client.get_playurl(bvid, cid, qn=qn)
    except Exception as exc:
        result["status"] = "error"
        result["note"] = f"playurl 失败：{exc}"
        on_log("warn", result["note"])
        cloud_job.update(result)
        write_json(folder / "cloud_job.json", cloud_job)
        return result
    video_urls, audio_urls = select_stream_candidates(play, qn)

    async def download_candidates(urls: list[str], dest: Path) -> None:
        last_error: Exception | None = None
        for index, url in enumerate(urls):
            try:
                await client.download_file(
                    url,
                    dest,
                    referer=page_url,
                    should_cancel=should_cancel,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if index + 1 < len(urls):
                    on_log("warn", f"主 CDN 下载失败，切换备用地址：{exc}")
        raise last_error or RuntimeError("没有可用的媒体下载地址")

    cloud_job.update(
        {
            "quality": play.get("quality"),
            "accept_description": play.get("accept_description"),
            "direct_urls_saved": False,
        }
    )
    if audio_urls and not existing_audio:
        raw_audio = media_dir / f"{bvid}.audio.m4s"
        on_log("info", f"下载音频 {bvid}")
        try:
            await download_candidates(audio_urls, raw_audio)
        except Exception:
            # CDN URLs can expire; refresh playurl once while retaining the .part file.
            play = await client.get_playurl(bvid, cid, qn=qn)
            _, audio_urls = select_stream_candidates(play, qn)
            await download_candidates(audio_urls, raw_audio)
        audio_output = raw_audio
        if ffmpeg:
            m4a = media_dir / f"{bvid}.m4a"
            temp_m4a = media_dir / f".{bvid}.m4a.tmp"
            ok, error = await _run_ffmpeg(
                [ffmpeg, "-y", "-i", str(raw_audio), "-vn", "-c:a", "copy", "-f", "ipod", str(temp_m4a)]
            )
            if ok and temp_m4a.exists():
                os.replace(temp_m4a, m4a)
                raw_audio.unlink(missing_ok=True)
                audio_output = m4a
            elif temp_m4a.exists():
                temp_m4a.unlink()
            if not ok:
                on_log("warn", f"音频封装为 m4a 失败，保留 m4s：{error[-180:]}")
        result["local_audio"] = str(audio_output)

    if mode == "video" and video_urls and not existing_video:
        vdest = media_dir / f"{bvid}.video.m4s"
        on_log("info", f"下载视频流 {bvid}")
        try:
            await download_candidates(video_urls, vdest)
        except Exception:
            play = await client.get_playurl(bvid, cid, qn=qn)
            video_urls, _ = select_stream_candidates(play, qn)
            await download_candidates(video_urls, vdest)
        muxed = media_dir / f"{bvid}.mp4"
        temp_muxed = media_dir / f".{bvid}.mp4.tmp"
        if ffmpeg and result.get("local_audio"):
            cmd = [
                ffmpeg, "-y", "-i", str(vdest), "-i", result["local_audio"],
                "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(temp_muxed),
            ]
            ok, error = await _run_ffmpeg(cmd)
            if ok and temp_muxed.exists():
                os.replace(temp_muxed, muxed)
                result["local_video"] = str(muxed)
                vdest.unlink(missing_ok=True)
            else:
                temp_muxed.unlink(missing_ok=True)
                result["local_video"] = str(vdest)
                result["note"] = f"ffmpeg 合并失败，已保留分离视频流：{error[-180:]}"
        else:
            result["local_video"] = str(vdest)
            if not ffmpeg:
                result["note"] = "未找到 ffmpeg，视频/音频仍为分离的 DASH 分片"
    result["status"] = "downloaded" if result["local_audio"] or result["local_video"] else "unavailable"
    if not result["note"]:
        result["note"] = "媒体下载完成"
    # Drop leftover DASH fragments and temp files once a usable file exists.
    media_dir = folder / "media"
    if media_dir.exists():
        keep = set()
        for item in (result.get("local_audio"), result.get("local_video")):
            if item:
                keep.add(Path(item).resolve())
        for leftover in media_dir.iterdir():
            if not leftover.is_file() or leftover.resolve() in keep:
                continue
            if leftover.suffix.lower() in {".m4s", ".part", ".tmp"} or leftover.name.startswith("."):
                leftover.unlink(missing_ok=True)
    cloud_job.update(result)
    write_json(folder / "cloud_job.json", cloud_job)
    return result
