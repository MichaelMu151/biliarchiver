#!/usr/bin/env python3
"""Re-run cloud Whisper / 2s-frame OCR for videos that missed a transcript."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bili.autodl import connect_exclusive, ssh_alive, ssh_fetch_and_transcribe, ssh_nvidia_smi
from bili.client import BiliClient
from bili.corpus import Corpus
from bili.export import write_video_markdown
from bili.gpu_remote import local_app_url
from bili.media import fetch_media, select_stream_candidates
from bili.ocr import OCR_FRAME_INTERVAL, transcribe_video_ocr
from bili.paths import CORPUS_DB_PATH, LIBRARY_DIR
from bili.pipeline import PipelineState
from bili.settings import load_settings
from bili.storage import reclaim_folder
from bili.transcribe import (
    _write_transcript_outputs,
    folder_transcript_has_text,
    folder_whisper_ran_empty,
    transcript_has_text,
)
from bili.util import write_json


def _log(level: str, message: str) -> None:
    print(f"[{level}] {message}", flush=True)


def _index_folders(library: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in library.rglob("*"):
        if not path.is_dir() or "/videos/" not in str(path) or path.name in {"media", "parts"}:
            continue
        for part in path.name.split("_"):
            if part.startswith("BV") and len(part) >= 12:
                found[part] = path
                break
    return found


def _comment_count(folder: Path) -> int:
    path = folder / "comments.jsonl"
    if not path.exists():
        return 0
    return sum(1 for line in path.open(encoding="utf-8") if line.strip())


def _ssh_log(message: str, level: str = "info") -> None:
    _log(level, message)


async def process_folder(
    folder: Path,
    client: BiliClient,
    settings,
    corpus: Corpus,
    run_id: str,
    ssh_client=None,
) -> str:
    meta_path = folder / "meta.json"
    if not meta_path.exists():
        return "no-meta"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    bvid = str(meta.get("bvid") or folder.name)
    cid = int(meta.get("cid") or (meta.get("pages") or [{}])[0].get("cid") or 0)
    if folder_transcript_has_text(folder):
        return "skip"
    result: dict = {}
    skip_whisper = folder_whisper_ran_empty(folder)
    if not skip_whisper:
        try:
            if ssh_client is None:
                raise RuntimeError("没有独占 SSH")
            play = await client.get_playurl(bvid, cid, qn=settings.video_quality)
            _, audio_urls = select_stream_candidates(play, settings.video_quality)
            if not audio_urls:
                raise RuntimeError("playurl 没有音频地址")
            result = await asyncio.to_thread(
                ssh_fetch_and_transcribe,
                ssh_client,
                bvid=bvid,
                urls=audio_urls,
                referer=f"https://www.bilibili.com/video/{bvid}",
                cookie=settings.cookie,
                token=settings.gpu_worker_token,
                model="large-v3",
                language=settings.whisper_language or "auto",
                log=_ssh_log,
            )
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            _log("warn", f"云端 GPU 转写失败 {bvid}：{detail}")
            result = {}
    if not transcript_has_text(result) and skip_whisper:
        video_path = ""
        media_dir = folder / "media"
        if media_dir.exists():
            mp4 = media_dir / f"{bvid}.mp4"
            if mp4.is_file():
                video_path = str(mp4)
        if not video_path:
            _log("info", f"补下视频用于画面 OCR：{bvid}")
            media = await fetch_media(
                client,
                bvid=bvid,
                cid=cid,
                folder=folder,
                mode="video",
                qn=settings.video_quality,
                ffmpeg_path=settings.ffmpeg_path,
                on_log=_log,
            )
            video_path = media.get("local_video") or ""
        if video_path:
            result = await transcribe_video_ocr(
                video_path=video_path,
                folder=folder,
                ffmpeg_path=settings.ffmpeg_path,
                interval=OCR_FRAME_INTERVAL,
                min_confidence=settings.ocr_min_confidence,
                compute_backend="cloud",
                gpu_worker_url=settings.gpu_worker_url,
                gpu_worker_token=settings.gpu_worker_token,
                on_log=_log,
            )
    if not transcript_has_text(result):
        return "fail"
    _write_transcript_outputs(folder, result)
    pipe = PipelineState.load(folder)
    sig = (
        f"v2:official_then_whisper:backend=cloud:model=large-v3:"
        f"lang={settings.whisper_language or 'auto'}:cid={cid}"
    )
    pipe.mark("transcript", sig, "done", source=result.get("source") or "")
    corpus.upsert_transcript(bvid=bvid, cid=cid, page=1, payload=result, run_id=run_id)
    meta["transcript_status"] = "done"
    write_json(folder / "meta.json", meta)
    write_video_markdown(folder, meta, result.get("markdown") or "", _comment_count(folder), 0)
    reclaim_folder(
        folder,
        policy="upload_then_delete",
        page_url=f"https://www.bilibili.com/video/{bvid}",
        transcript_ready=True,
        ocr_ready=True,
        ffmpeg_path=settings.ffmpeg_path,
        rclone_remote=settings.rclone_remote,
        rclone_root=settings.rclone_root,
        library_root=LIBRARY_DIR,
        on_log=_log,
        include_images=False,
        include_media=True,
    )
    _log("ok", f"{bvid} {result.get('source')}")
    return "ok"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", default="5ba1f9a96259")
    args = parser.parse_args()
    settings = load_settings()
    if not settings.gpu_worker_url:
        _log("error", "没有 GPU 工作机地址")
        return 2
    corpus = Corpus(CORPUS_DB_PATH)
    folders = _index_folders(LIBRARY_DIR)
    with corpus.connect() as conn:
        rows = conn.execute(
            "SELECT bvid FROM videos WHERE run_id=? AND pass_filter=1",
            (args.run,),
        ).fetchall()
        passed = [row["bvid"] for row in rows]
    todo: list[Path] = []
    for bvid in passed:
        folder = folders.get(bvid)
        if folder and not folder_transcript_has_text(folder):
            todo.append(folder)
    _log("info", f"待补转写 {len(todo)} 条（过门 {len(passed)}）")
    stats = {"ok": 0, "fail": 0, "skip": 0}
    try:
        import httpx

        httpx.post(f"{local_app_url()}/api/gpu/autodl/disconnect", timeout=10.0)
        _log("info", "已断开本机隧道。GPU 自己从 B 站下音频并转写，不再从 Mac 传文件")
    except Exception as exc:
        _log("warn", f"断开隧道时：{exc}")
    ssh_client = None
    client = BiliClient(settings, on_log=_log)
    try:
        await client.bootstrap()
        ssh_client = connect_exclusive(_ssh_log)
        smi = ssh_nvidia_smi(ssh_client)
        if smi:
            _log("info", f"GPU 现状 {smi}")
        for index, folder in enumerate(todo, 1):
            settings = load_settings()
            if not ssh_alive(ssh_client):
                _log("warn", "SSH 断了，正在重连")
                try:
                    ssh_client.close()
                except Exception:
                    pass
                ssh_client = connect_exclusive(_ssh_log)
            _log("info", f"[{index}/{len(todo)}] {folder.name}")
            try:
                status = await process_folder(
                    folder, client, settings, corpus, args.run, ssh_client=ssh_client
                )
            except Exception as exc:
                detail = str(exc).strip() or type(exc).__name__
                _log("error", f"{folder.name} {detail}")
                status = "fail"
            stats[status] = stats.get(status, 0) + 1
            if status == "fail" and not ssh_alive(ssh_client):
                _log("warn", "转写失败且 SSH 已断，下一条会重连")
    finally:
        if ssh_client is not None:
            try:
                ssh_client.close()
            except Exception:
                pass
        await client.close()
    _log("ok", f"补跑结束 ok={stats['ok']} fail={stats['fail']} skip={stats['skip']}")
    return 0 if stats["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
