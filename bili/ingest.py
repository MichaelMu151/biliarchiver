"""Backfill existing Markdown/JSONL archives into corpus.db."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bili.corpus import Corpus
from bili.crawler import iter_jsonl
from bili.paths import LIBRARY_DIR


def ingest_library(library: Path | None = None, corpus: Corpus | None = None, run_id: str = "backfill") -> dict[str, int]:
    root = Path(library or LIBRARY_DIR)
    db = corpus or Corpus()
    db.start_run(run_id, "archive", {"source": str(root)}, label="回填已有档案")
    counts = {"accounts": 0, "videos": 0, "comments": 0, "danmaku": 0, "dynamics": 0, "transcripts": 0}
    try:
        for acc_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            profile = _read_json(acc_dir / "profile.json")
            if profile.get("mid"):
                db.upsert_author(profile)
                counts["accounts"] += 1
            snaps = acc_dir / "snapshots.jsonl"
            if snaps.exists():
                last = None
                for row in iter_jsonl(snaps):
                    last = row
                if last:
                    db.add_author_snapshot(last, run_id=run_id)
            videos_root = acc_dir / "videos"
            if videos_root.is_dir():
                for folder in videos_root.iterdir():
                    if not folder.is_dir():
                        continue
                    meta = _read_json(folder / "meta.json")
                    bvid = str(meta.get("bvid") or "")
                    if not bvid:
                        continue
                    owner = meta.get("owner") or {}
                    stat = meta.get("stat") or {}
                    db.upsert_video(
                        {
                            "bvid": bvid,
                            "aid": meta.get("aid"),
                            "cid": meta.get("cid"),
                            "mid": profile.get("mid") or owner.get("mid") or "",
                            "author_name": owner.get("name") or profile.get("name") or "",
                            "title": meta.get("title"),
                            "description": meta.get("desc"),
                            "tid": meta.get("tid"),
                            "tname": meta.get("tname"),
                            "pubdate": meta.get("pubdate"),
                            "duration": meta.get("duration"),
                            "view_count": stat.get("view"),
                            "like_count": stat.get("like"),
                            "coin_count": stat.get("coin"),
                            "favorite_count": stat.get("favorite"),
                            "share_count": stat.get("share"),
                            "reply_count": stat.get("reply"),
                            "danmaku_count": stat.get("danmaku"),
                            "tags": meta.get("tags") or [],
                            "pages": meta.get("pages") or [],
                            "page_url": meta.get("page_url"),
                            "discovery": "space",
                            "run_id": run_id,
                            "captured_at": meta.get("captured_at"),
                        }
                    )
                    counts["videos"] += 1
                    comments = folder / "comments.jsonl"
                    if comments.exists():
                        n = db.upsert_comments(
                            iter_jsonl(comments),
                            target_kind="video",
                            target_id=bvid,
                            bvid=bvid,
                            run_id=run_id,
                        )
                        counts["comments"] += n
                    danmaku = folder / "danmaku.jsonl"
                    if danmaku.exists():
                        counts["danmaku"] += db.upsert_danmaku(iter_jsonl(danmaku), run_id=run_id)
                    for part_dir in [folder, *sorted(p for p in folder.iterdir() if p.is_dir())]:
                        transcript = part_dir / "transcript.json"
                        if not transcript.exists():
                            continue
                        payload = _read_json(transcript)
                        if not payload or payload.get("multipart"):
                            continue
                        db.upsert_transcript(
                            bvid=bvid,
                            cid=int(payload.get("cid") or meta.get("cid") or 0),
                            page=int(payload.get("page") or 1),
                            payload=payload,
                            run_id=run_id,
                        )
                        counts["transcripts"] += 1
            dyn_root = acc_dir / "dynamics"
            if dyn_root.is_dir():
                for folder in dyn_root.iterdir():
                    if not folder.is_dir():
                        continue
                    dyn = _read_json(folder / "meta.json")
                    if not dyn.get("dyn_id"):
                        continue
                    ocr = _read_json(folder / "ocr.json")
                    db.upsert_dynamic(
                        dyn,
                        ocr.get("images") if isinstance(ocr, dict) else None,
                        author_name=profile.get("name") or "",
                        run_id=run_id,
                    )
                    counts["dynamics"] += 1
                    comments = folder / "comments.jsonl"
                    if comments.exists():
                        counts["comments"] += db.upsert_comments(
                            iter_jsonl(comments),
                            target_kind="dynamic",
                            target_id=str(dyn["dyn_id"]),
                            run_id=run_id,
                        )
        db.finish_run(run_id, "done")
    except Exception:
        db.finish_run(run_id, "error")
        raise
    return counts


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
