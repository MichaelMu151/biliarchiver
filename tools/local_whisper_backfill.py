#!/usr/bin/env python3
"""Backfill missing transcripts with local CPU Whisper (no cloud GPU)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bili.client import BiliClient
from bili.corpus import Corpus
from bili.export import write_video_markdown
from bili.media import fetch_media
from bili.paths import CORPUS_DB_PATH, LIBRARY_DIR
from bili.pipeline import PipelineState
from bili.settings import load_settings
from bili.transcribe import (
    _write_transcript_outputs,
    folder_transcript_has_text,
    transcript_has_text,
    transcribe_local,
    whisper_available,
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


def _resolve_audio(folder: Path, bvid: str) -> Path | None:
    media = folder / "media"
    for name in (f"{bvid}.m4a", f"{bvid}.mp3", f"{bvid}.wav"):
        path = media / name
        if path.is_file():
            return path
    if media.is_dir():
        for pattern in ("*.m4a", "*.mp3", "*.wav", "*.flac"):
            match = next(media.glob(pattern), None)
            if match:
                return match
    return None


async def process_bvid(
    bvid: str,
    *,
    folder: Path,
    client: BiliClient,
    settings,
    corpus: Corpus,
    run_id: str,
    model: str,
    force: bool,
) -> str:
    meta_path = folder / "meta.json"
    if not meta_path.exists():
        return "no-meta"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    cid = int(meta.get("cid") or (meta.get("pages") or [{}])[0].get("cid") or 0)
    if folder_transcript_has_text(folder) and not force:
        return "skip"

    audio = _resolve_audio(folder, bvid)
    if audio is None:
        _log("info", f"本地无音频，重新下载 {bvid}")
        media = await fetch_media(
            client,
            bvid=bvid,
            cid=cid,
            folder=folder,
            mode="audio",
            qn=settings.video_quality,
            ffmpeg_path=settings.ffmpeg_path,
            on_log=_log,
        )
        audio_path = media.get("local_audio") or ""
        if not audio_path or not Path(audio_path).is_file():
            return "no-audio"
        audio = Path(audio_path)

    _log("info", f"本地 Whisper {bvid} · {audio.name} · model={model} · cpu/int8")
    result = await asyncio.to_thread(
        transcribe_local,
        str(audio),
        model,
        settings.whisper_language or "auto",
        None,
        "cpu",
        "int8",
    )
    if not transcript_has_text(result):
        return "empty"
    _write_transcript_outputs(folder, result)
    pipe = PipelineState.load(folder)
    sig = (
        f"v2:whisper:backend=local:model={model}:"
        f"lang={settings.whisper_language or 'auto'}:cid={cid}"
    )
    pipe.mark("transcript", sig, "done", source=result.get("source") or "")
    corpus.upsert_transcript(bvid=bvid, cid=cid, page=1, payload=result, run_id=run_id)
    meta["transcript_status"] = "done"
    meta["transcript_source"] = result.get("source") or ""
    write_json(folder / "meta.json", meta)
    write_video_markdown(folder, meta, result.get("markdown") or "", _comment_count(folder), 0)
    _log("ok", f"{bvid} · {len(result.get('segments') or [])} 段 · {result.get('source')}")
    return "ok"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Local CPU Whisper backfill for specific BVs")
    parser.add_argument("bvids", nargs="+", help="BV ids to backfill")
    parser.add_argument("--run", default="53c6a9380810")
    parser.add_argument("--model", default="small")
    parser.add_argument("--force", action="store_true", help="Re-run even if transcript exists")
    args = parser.parse_args()
    if not whisper_available():
        _log("error", "缺少 faster-whisper")
        return 2

    settings = load_settings()
    folders = _index_folders(LIBRARY_DIR)
    corpus = Corpus(CORPUS_DB_PATH)
    stats = {"ok": 0, "skip": 0, "fail": 0}
    client = BiliClient(settings, on_log=_log)
    await client.bootstrap()
    try:
        for index, bvid in enumerate(args.bvids, 1):
            folder = folders.get(bvid)
            if not folder:
                _log("error", f"[{index}] 找不到目录 {bvid}")
                stats["fail"] += 1
                continue
            _log("info", f"[{index}/{len(args.bvids)}] {folder.name}")
            try:
                status = await process_bvid(
                    bvid,
                    folder=folder,
                    client=client,
                    settings=settings,
                    corpus=corpus,
                    run_id=args.run,
                    model=args.model,
                    force=args.force,
                )
            except Exception as exc:
                detail = str(exc).strip() or type(exc).__name__
                _log("error", f"{bvid} {detail}")
                status = "fail"
            if status == "ok":
                stats["ok"] += 1
            elif status == "skip":
                stats["skip"] += 1
            else:
                stats["fail"] += 1
    finally:
        await client.close()
    _log("ok", f"完成 ok={stats['ok']} skip={stats['skip']} fail={stats['fail']}")
    return 0 if stats["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
