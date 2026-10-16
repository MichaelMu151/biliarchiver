#!/usr/bin/env python3
"""精准补弹幕：只拉 danmaku，不碰评论 / 转写 / 媒体。

用法（都在项目根目录，用 .venv 的 python）：

  # 1. 先看哪些视频在库里还没有弹幕（只读，不写库）
  .venv/bin/python tools/backfill_danmaku.py --report

  # 2. 对某个任务的 BV 清单补弹幕（dry-run，只打印会做什么）
  .venv/bin/python tools/backfill_danmaku.py \
      --list data/exports/recrawl_shortlist.csv

  # 3. 真正执行
  .venv/bin/python tools/backfill_danmaku.py \
      --list data/exports/recrawl_shortlist.csv --apply

原理
----
* 逐个视频读 ``meta.json``（已有 cid / aid / duration / 分P），
  用 ``BiliClient.get_danmaku(cid, duration, aid)`` 拉取全部分段
  （protobuf ``seg.so``；首段为空时自动回退 ``comment.bilibili.com/{cid}.xml``）。
* 写入与归档管线一致的 ``danmaku.jsonl``（每条附 ``cid/bvid/part``），
  并把 ``.pipeline.json`` 的 ``danmaku`` 阶段标记为 ``done``，
  之后正常任务会直接复用，不再重拉。
* 入库走 ``Corpus.upsert_danmaku``（按 ``danmaku_id`` 去重），
  重跑不会产生重复行；同时写 research 库。
* 默认跳过已有非空 ``danmaku.jsonl`` 的视频（除非 ``--force``），
  所以 240 条清单里 56 条 rescued（弹幕本就有）会自动跳过，
  只补真正缺的那 184 条。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bili.client import BiliClient
from bili.corpus import Corpus
from bili.paths import LIBRARY_DIR
from bili.pipeline import PipelineState
from bili.settings import load_settings

DANMAKU_SIGNATURE = "v2:full-segments"


def parse_bv_list(path: Path) -> list[str]:
    out: list[str] = []
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                bvid = (row.get("bvid") or "").strip()
                if bvid.startswith("BV"):
                    out.append(bvid)
    else:
        for line in path.read_text(encoding="utf-8").splitlines():
            token = line.strip().split()[0] if line.strip() else ""
            if token.startswith("BV"):
                out.append(token)
    seen: set[str] = set()
    uniq = [b for b in out if not (b in seen or seen.add(b))]
    return uniq


_FOLDER_INDEX: dict[str, Path] | None = None


def find_folder(bvid: str) -> Path | None:
    global _FOLDER_INDEX
    if _FOLDER_INDEX is None:
        index: dict[str, Path] = {}
        for mid_dir in LIBRARY_DIR.iterdir():
            videos = mid_dir / "videos"
            if not videos.is_dir():
                continue
            for folder in videos.iterdir():
                if not folder.is_dir() or folder.name in {"media", "parts"}:
                    continue
                for part in folder.name.split("_"):
                    if part.startswith("BV") and len(part) >= 12:
                        index.setdefault(part, folder)
        _FOLDER_INDEX = index
    return _FOLDER_INDEX.get(bvid)


def folder_pages(meta: dict) -> list[dict]:
    pages = meta.get("pages") or []
    if pages:
        return pages
    cid = int(meta.get("cid") or 0)
    if cid:
        return [
            {
                "cid": cid,
                "page": 1,
                "part": meta.get("title") or "P1",
                "duration": meta.get("duration") or 0,
            }
        ]
    return []


async def backfill_one(
    client: BiliClient,
    corpus: Corpus,
    folder: Path,
    bvid: str,
    *,
    run_id: str,
    force: bool = False,
) -> tuple[str, int]:
    danmaku_path = folder / "danmaku.jsonl"
    pipeline = PipelineState.load(folder)
    existing = 0
    if danmaku_path.exists():
        try:
            with danmaku_path.open(encoding="utf-8") as fh:
                existing = sum(1 for _ in fh)
        except OSError:
            existing = -1
    if existing > 0 and not force:
        return "skip-exists", existing
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return "no-meta", 0
    pages = folder_pages(meta)
    if not pages:
        return "no-cid", 0
    aid = int(meta.get("aid") or 0) or None
    duration_default = int(meta.get("duration") or 0)
    all_rows: list[dict] = []
    for page in pages:
        cid = int(page.get("cid") or 0)
        if not cid:
            continue
        dms = await client.get_danmaku(
            cid,
            int(page.get("duration") or duration_default or 0),
            aid,
        )
        for dm in dms:
            dm["cid"] = cid
            dm["bvid"] = bvid
            dm["part"] = page.get("part")
            all_rows.append(dm)
    if not all_rows:
        pipeline.mark("danmaku", DANMAKU_SIGNATURE, "done", count=0)
        return "empty", 0
    tmp = danmaku_path.with_name(danmaku_path.name + ".part")
    with tmp.open("w", encoding="utf-8") as fh:
        for dm in all_rows:
            fh.write(json.dumps(dm, ensure_ascii=False) + "\n")
    tmp.replace(danmaku_path)
    pipeline.mark("danmaku", DANMAKU_SIGNATURE, "done", count=len(all_rows))
    corpus.upsert_danmaku(iter(all_rows), run_id=run_id)
    return "ok", len(all_rows)


async def run_list(bvids: list[str], *, apply: bool, force: bool, run_id: str) -> dict:
    settings = load_settings()
    corpus = Corpus()
    summary = {"ok": 0, "rows": 0, "skip-exists": 0, "empty": 0, "no-folder": 0, "no-meta": 0, "no-cid": 0}
    if not apply:
        for bvid in bvids:
            folder = find_folder(bvid)
            if folder is None:
                summary["no-folder"] += 1
                print(f"[dry] {bvid} 无本地文件夹（先走按视频号采集建档）")
                continue
            dm = folder / "danmaku.jsonl"
            n = sum(1 for _ in dm.open(encoding="utf-8")) if dm.exists() else 0
            print(f"[dry] {bvid} 现有弹幕 {n} 条 → {'跳过' if n > 0 and not force else '会补拉'}")
        return summary
    client = BiliClient(settings)
    try:
        for idx, bvid in enumerate(bvids, 1):
            folder = find_folder(bvid)
            if folder is None:
                summary["no-folder"] += 1
                print(f"[{idx}/{len(bvids)}] {bvid} 无本地文件夹，跳过")
                continue
            try:
                status, count = await backfill_one(client, corpus, folder, bvid, run_id=run_id, force=force)
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响整批
                print(f"[{idx}/{len(bvids)}] {bvid} 失败：{exc}")
                continue
            summary[status] = summary.get(status, 0) + 1
            if status == "ok":
                summary["rows"] += count
            print(f"[{idx}/{len(bvids)}] {bvid} {status} · {count} 条")
    finally:
        await client.close()
    return summary


def _missing_danmaku_from_db(corpus: Corpus, limit: int) -> list[tuple[str, str]]:
    with corpus.connect() as conn:
        rows = conn.execute(
            """
            SELECT v.bvid AS bvid, v.title AS title
            FROM videos v
            WHERE NOT EXISTS (
              SELECT 1 FROM danmaku d WHERE d.bvid = v.bvid
            )
            ORDER BY v.reply_count DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [(str(r["bvid"]), str(r["title"] or "")) for r in rows if r["bvid"]]


async def report_missing(limit: int = 50) -> None:
    corpus = Corpus()
    rows = _missing_danmaku_from_db(corpus, limit=limit * 5)
    shown = 0
    for bvid, title in rows:
        folder = find_folder(bvid)
        if folder is None:
            continue
        dm = folder / "danmaku.jsonl"
        n = sum(1 for _ in dm.open(encoding="utf-8")) if dm.exists() else 0
        if n == 0:
            print(f"{bvid} · {(title or '')[:40]}")
            shown += 1
            if shown >= limit:
                break
    print(f"[report] 缺弹幕（本地 0 条）示例 {shown} 条")


def main() -> int:
    ap = argparse.ArgumentParser(description="精准补弹幕（只动 danmaku.jsonl + 入库）")
    ap.add_argument("--report", action="store_true", help="只列出缺弹幕的视频")
    ap.add_argument("--list", default="", help="BV 清单（csv 需有 bvid 列，或纯文本）")
    ap.add_argument("--apply", action="store_true", help="真正执行；不加则 dry-run")
    ap.add_argument("--force", action="store_true", help="已有弹幕也重拉覆盖")
    ap.add_argument("--run-id", default="danmaku-backfill", help="写入 corpus 的 run_id 标记")
    args = ap.parse_args()

    if args.report:
        asyncio.run(report_missing())
        return 0
    if not args.list:
        ap.error("需要 --list 或 --report")
    bvids = parse_bv_list(Path(args.list))
    print(f"[info] 清单 {len(bvids)} 条，apply={args.apply} force={args.force}")
    summary = asyncio.run(run_list(bvids, apply=args.apply, force=args.force, run_id=args.run_id))
    print(f"[done] {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
