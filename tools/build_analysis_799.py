#!/usr/bin/env python3
"""构建 799 分析库 data/analysis_799.db（区别于 corpus.db 全量库）。

S = {v : labor-hit(v) ∧ transcribed(v)}，799 条 distinct bvid。
搬运：videos 全字段 + comments + danmaku + transcripts + transcript_segments
     + video_tags（全部按 bvid 精确归属）+ snowball_edges 子图
     （src/dst 都在 799 内）+ provenance 归属表。

provenance 桶（优先级：backfill74 > 建档/转写任务归属）：
  snowball_r2 206 · xingyu 187 · worktime 197 · snowball_r1 115
  backfill74 74 · manual20（MANUAL-20，手填/试跑/非系统性）

校验：每表搬完后按 bvid 对账（评论/弹幕逐条 bvid 必须在 799 内；
danmaku 额外校验 cid 归属唯一）。

用法：.venv/bin/python tools/build_analysis_799.py [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bili.labor_lexicon import match_labor

SRC = ROOT / "data" / "corpus.db"
DST = ROOT / "data" / "analysis_799.db"

BUCKETS = {
    "snowball_r1": ("53c6a9380810", "上轮滚雪球+其转写后代"),
    "snowball_r2": ("e9f81b4b80fd", "本轮滚雪球"),
    "xingyu": ("1b54ee358b71/747ef4ab4c22", "星宇关键词391→389→187"),
    "worktime": ("07f0fa77c98f/e0d37090530d", "工时关键词"),
    "backfill74": ("277ca9b4aa63", "回炉74（rescued+scout精选）"),
    "manual20": ("MANUAL-20", "手填/试跑/非系统性（含归档副产品与早期零散）"),
}


def labor_ok(cur: sqlite3.Cursor, bvid: str) -> bool:
    v = cur.execute("SELECT * FROM videos WHERE bvid=?", (bvid,)).fetchone()
    if not v:
        return False
    try:
        tj = json.loads(v["tags_json"] or "[]")
    except (ValueError, TypeError):
        tj = []
    m = match_labor(" ".join(list(tj) + [v["title"] or "", v["description"] or ""]))
    return bool(m.hit and m.tier != "excluded_family")


def main() -> int:
    ap = argparse.ArgumentParser(description="构建 799 分析库")
    ap.add_argument("--force", action="store_true", help="已存在则重建")
    args = ap.parse_args()
    if DST.exists() and not args.force:
        print(f"[info] {DST} 已存在，加 --force 重建")
        return 0
    if DST.exists():
        DST.unlink()

    src = sqlite3.connect(SRC)
    src.row_factory = sqlite3.Row
    trset = [r[0] for r in src.execute("SELECT DISTINCT bvid FROM transcripts")]
    S = sorted(b for b in trset if labor_ok(src, b))
    assert len(S) == len(set(S)) == 799, f"S 去重失败: {len(S)}"

    backfill74: set[str] = set()
    import csv

    with open(ROOT / "data/exports/recrawl_shortlist.csv", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["bucket"] == "rescued_tags":
                backfill74.add(row["bvid"])
    with open(ROOT / "data/exports/scout_labor_34.csv", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            backfill74.add(row["bvid"])

    def bucket(bvid: str) -> tuple[str, str, str]:
        if bvid in backfill74:
            return "backfill74", "277ca9b4aa63", "回炉清单命中"
        v = src.execute("SELECT run_id FROM videos WHERE bvid=?", (bvid,)).fetchone()
        rid = v["run_id"] if v else ""
        t = src.execute(
            "SELECT run_id FROM transcripts WHERE bvid=? ORDER BY captured_at LIMIT 1", (bvid,)
        ).fetchone()
        trid = t["run_id"] if t else ""
        for cand in (rid, trid):
            if cand == "53c6a9380810":
                return "snowball_r1", cand, "建档/转写归属上轮滚雪球"
            if cand == "e9f81b4b80fd":
                return "snowball_r2", cand, "建档/转写归属本轮滚雪球"
            if cand == "1b54ee358b71":
                return "xingyu", cand, "星宇关键词转写链"
            if cand in ("07f0fa77c98f", "e0d37090530d"):
                return "worktime", cand, "工时关键词链"
        return "manual20", "MANUAL-20", f"非系统性（建档{rid}/转写{trid}）"

    prov = {b: bucket(b) for b in S}
    assert len(prov) == 799

    dst = sqlite3.connect(DST)
    tables = ["videos", "comments", "danmaku", "transcripts", "transcript_segments", "video_tags"]
    for tbl in tables:
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({tbl})")]
        coldefs = ", ".join(
            f'"{r[1]}" {r[2]}' for r in src.execute(f"PRAGMA table_info({tbl})")
        )
        dst.execute(f'CREATE TABLE "{tbl}" ({coldefs})')
        placeholders = ",".join("?" for _ in cols)
        batch: list[tuple] = []
        for (bvid,) in [(b,) for b in S]:
            for row in src.execute(f'SELECT * FROM "{tbl}" WHERE bvid=?', (bvid,)):
                batch.append(tuple(row))
                if len(batch) >= 2000:
                    dst.executemany(
                        f'INSERT INTO "{tbl}" VALUES ({placeholders})', batch
                    )
                    batch = []
        if batch:
            dst.executemany(f'INSERT INTO "{tbl}" VALUES ({placeholders})', batch)
        for col in cols:
            try:
                dst.execute(f'CREATE INDEX "idx_{tbl}_{col}" ON "{tbl}" ("{col}")')
            except sqlite3.OperationalError:
                pass
    # snowball 子图：src/dst 都在 S 内
    ecoldefs = ", ".join(
        f'"{r[1]}" {r[2]}' for r in src.execute("PRAGMA table_info(snowball_edges)")
    )
    dst.execute(f"CREATE TABLE snowball_edges ({ecoldefs})")
    ecols = [r[1] for r in src.execute("PRAGMA table_info(snowball_edges)")]
    eph = ",".join("?" for _ in ecols)
    sset = set(S)
    ebatch = [
        tuple(r)
        for r in src.execute("SELECT * FROM snowball_edges")
        if r["src_bvid"] in sset and r["dst_bvid"] in sset
    ]
    dst.executemany(f"INSERT INTO snowball_edges VALUES ({eph})", ebatch)
    # provenance
    dst.execute(
        """CREATE TABLE provenance (
          bvid TEXT PRIMARY KEY, bucket TEXT, primary_run TEXT, note TEXT,
          labor_tier TEXT, labor_terms TEXT, transcript_status TEXT)"""
    )
    for bvid in S:
        bkt, prun, note = prov[bvid]
        v = src.execute("SELECT * FROM videos WHERE bvid=?", (bvid,)).fetchone()
        try:
            tj = json.loads(v["tags_json"] or "[]")
        except (ValueError, TypeError):
            tj = []
        m = match_labor(" ".join(list(tj) + [v["title"] or "", v["description"] or ""]))
        best = src.execute(
            "SELECT IFNULL(char_count, LENGTH(full_text)) AS cc FROM transcripts WHERE bvid=? ORDER BY char_count DESC LIMIT 1",
            (bvid,),
        ).fetchone()
        cc = best["cc"] or 0
        status = "full" if cc >= 500 else ("empty" if cc == 0 else "short")
        dst.execute(
            "INSERT INTO provenance VALUES (?,?,?,?,?,?,?)",
            (bvid, bkt, prun, note, m.tier, ",".join(m.terms[:6]), status),
        )
    dst.commit()

    # ---- 校验 ----
    errs = []
    for tbl in tables:
        strays = dst.execute(
            f'SELECT COUNT(*) FROM "{tbl}" WHERE bvid NOT IN ({",".join("?" * len(S))})', S
        ).fetchone()[0]
        if strays:
            errs.append(f"{tbl} 有 {strays} 条 bvid 越界")
    n_v = dst.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    if n_v != 799:
        errs.append(f"videos 行数 {n_v} != 799")
    n_c = dst.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    n_d = dst.execute("SELECT COUNT(*) FROM danmaku").fetchone()[0]
    n_t = dst.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
    # danmaku cid 归属：同一 cid 只对一个 bvid
    bad_cid = dst.execute(
        "SELECT COUNT(*) FROM (SELECT cid FROM danmaku GROUP BY cid HAVING COUNT(DISTINCT bvid)>1)"
    ).fetchone()[0]
    if bad_cid:
        errs.append(f"danmaku 有 {bad_cid} 个 cid 对应多个 bvid")
    print(f"[verify] videos={n_v} comments={n_c} danmaku={n_d} transcripts={n_t} edges={len(ebatch)}")
    from collections import Counter

    print("[verify] buckets:", dict(Counter(b[0] for b in prov.values())))
    print("[verify] transcript_status:", dict(dst.execute("SELECT transcript_status, COUNT(*) FROM provenance GROUP BY 1").fetchall()))
    digest = hashlib.sha256()
    for bvid in S:
        digest.update(bvid.encode())
    print("[verify] S sha256:", digest.hexdigest()[:16])
    if errs:
        print("[FAIL]", errs)
        return 1
    print(f"[ok] {DST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
