#!/usr/bin/env python3
"""构建删减观察库 data/analysis_censor.db（轻量：只留删减研究要的字段）。

内容（三批，不做劳工词过滤、不判离题）：
  A. 星宇视频：dca8ad252178 建档 36 + 747ef4ab4c22 清单 391（去重≈391）
  B. 图文：keyword_0d5d430c59e2/dynamics.csv 175（工时）+
           keyword_dca8ad252178/dynamics.csv 51（星宇）
  C. work_time.xlsx：178 行（BV + 标题，第一列，桌面文件，只读不改）

表：
  censor_videos(bvid,title,author_name,mid,pubdate_iso,duration,
    view_count,like_count,reply_count,danmaku_count,source,offtopic,alive,checked_at)
  censor_articles(dyn_id,mid,author_name,pub_time_iso,text_snippet,like_count,
    comment_count,forward_count,keyword,jump_url,source,offtopic,alive,checked_at)

offtopic 默认 0（由人工 UPDATE 标离题）；alive 默认 1，checked_at=建库时间，
日后复查任务只 UPDATE 这两列。

用法：.venv/bin/python tools/build_analysis_censor.py [--force]
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = ROOT / "data" / "corpus.db"
DST = ROOT / "data" / "analysis_censor.db"
NOW = datetime.now(timezone.utc).astimezone().isoformat()


def main() -> int:
    ap = argparse.ArgumentParser(description="构建删减观察库")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if DST.exists() and not args.force:
        print(f"[info] {DST} 已存在，加 --force 重建")
        return 0
    if DST.exists():
        DST.unlink()

    src = sqlite3.connect(SRC)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(DST)
    dst.execute(
        """CREATE TABLE censor_videos(
          bvid TEXT PRIMARY KEY, title TEXT, author_name TEXT, mid TEXT,
          pubdate_iso TEXT, duration INTEGER,
          view_count INTEGER, like_count INTEGER, reply_count INTEGER, danmaku_count INTEGER,
          source TEXT, offtopic INTEGER DEFAULT 0, alive INTEGER DEFAULT 1, checked_at TEXT)"""
    )
    dst.execute(
        """CREATE TABLE censor_articles(
          dyn_id TEXT PRIMARY KEY, mid TEXT, author_name TEXT, pub_time_iso TEXT,
          text_snippet TEXT, like_count INTEGER, comment_count INTEGER, forward_count INTEGER,
          keyword TEXT, jump_url TEXT, kind TEXT,
          source TEXT, offtopic INTEGER DEFAULT 0, alive INTEGER DEFAULT 1, checked_at TEXT)"""
    )

    # ---- A. 星宇视频 ----
    star: dict[str, str] = {}
    for bvid in [r[0] for r in src.execute("SELECT bvid FROM videos WHERE run_id='dca8ad252178'")]:
        star[bvid] = "dca8ad252178/建档36"
    with open(ROOT / "data/exports/keyword_747ef4ab4c22/bvids.txt", encoding="utf-8") as fh:
        for line in fh:
            bvid = line.strip()
            if bvid:
                star.setdefault(bvid, "747ef4ab4c22/清单391")
    nv = 0
    for bvid, source in star.items():
        v = src.execute("SELECT * FROM videos WHERE bvid=?", (bvid,)).fetchone()
        if v is None:
            dst.execute(
                "INSERT INTO censor_videos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (bvid, "", "", "", "", 0, 0, 0, 0, 0, source + "/库无建档", 0, 0, NOW),
            )
            continue
        dst.execute(
            "INSERT INTO censor_videos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                bvid, v["title"], v["author_name"], str(v["mid"] or ""),
                v["pubdate_iso"], v["duration"] or 0,
                v["view_count"] or 0, v["like_count"] or 0,
                v["reply_count"] or 0, v["danmaku_count"] or 0,
                source, 0, 1, NOW,
            ),
        )
        nv += 1

    # ---- B. 图文 ----
    na = 0
    for path, source in [
        ("data/exports/keyword_0d5d430c59e2/dynamics.csv", "工时图文0d5d"),
        ("data/exports/keyword_dca8ad252178/dynamics.csv", "星宇图文dca8"),
    ]:
        with open(ROOT / path, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                text = r.get("text") or ""
                try:
                    dst.execute(
                        "INSERT OR IGNORE INTO censor_articles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            str(r.get("dyn_id")), str(r.get("mid")), r.get("author_name"),
                            r.get("pub_time_iso"), text[:500],
                            int(float(r.get("like") or 0)), int(float(r.get("comment") or 0)),
                            int(float(r.get("forward") or 0)),
                            r.get("keyword"), r.get("jump_url"), r.get("kind"),
                            source, 0, 1, NOW,
                        ),
                    )
                    na += 1
                except (ValueError, TypeError):
                    continue

    # ---- C. work_time.xlsx 清单（只记录在案，不强制建档） ----
    dst.execute("CREATE TABLE worktime_list(bvid TEXT PRIMARY KEY, title TEXT)")
    nw = 0
    try:
        import openpyxl

        wb = openpyxl.load_workbook("/Users/mushiquan/Desktop/work_time.xlsx", read_only=True)
        for row in wb.active.iter_rows(values_only=True):
            cell = str(row[0] or "").strip()
            if not cell.startswith("BV"):
                continue
            parts = cell.split(None, 1)
            bvid, title = parts[0], (parts[1] if len(parts) > 1 else "")
            dst.execute("INSERT OR IGNORE INTO worktime_list VALUES (?,?)", (bvid, title))
            nw += 1
    except ImportError:
        print("[warn] 无 openpyxl，worktime_list 跳过")
    dst.commit()

    # ---- 校验 ----
    errs = []
    n_v = dst.execute("SELECT COUNT(*) FROM censor_videos").fetchone()[0]
    n_a = dst.execute("SELECT COUNT(*) FROM censor_articles").fetchone()[0]
    if n_v != len(star):
        errs.append(f"videos {n_v} != {len(star)}")
    print(f"[verify] videos={n_v}(建档{nv}/无档{len(star)-nv}) articles={n_a} worktime_list={nw}")
    if errs:
        print("[FAIL]", errs)
        return 1
    print(f"[ok] {DST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
