#!/usr/bin/env python3
"""构建图文分析库 data/analysis_dynamics.db（独立于 analysis_799.db，日后清洗/合并）。

口径：dynamics.text 过 match_labor，strong/phrase 命中且非 excluded_family。
moderate 单出不收（与视频口径一致）。
搬运：dynamics 全字段 + dynamic_images（按 dyn_id 归属）。

用法：.venv/bin/python tools/build_analysis_dynamics.py [--force]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bili.labor_lexicon import match_labor

SRC = ROOT / "data" / "corpus.db"
DST = ROOT / "data" / "analysis_dynamics.db"


def main() -> int:
    ap = argparse.ArgumentParser(description="构建图文分析库")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if DST.exists() and not args.force:
        print(f"[info] {DST} 已存在，加 --force 重建")
        return 0
    if DST.exists():
        DST.unlink()

    src = sqlite3.connect(SRC)
    src.row_factory = sqlite3.Row
    keep: list[tuple[str, str, str]] = []  # dyn_id, tier, terms
    for r in src.execute("SELECT dyn_id, text FROM dynamics"):
        m = match_labor(r["text"] or "")
        if m.hit and m.tier != "excluded_family":
            keep.append((r["dyn_id"], m.tier, ",".join(m.terms[:6])))
    print(f"[info] labor dynamics: {len(keep)}")

    dst = sqlite3.connect(DST)
    dcols = [r[1] for r in src.execute("PRAGMA table_info(dynamics)")]
    ddefs = ", ".join(f'"{r[1]}" {r[2]}' for r in src.execute("PRAGMA table_info(dynamics)"))
    dst.execute(f"CREATE TABLE dynamics ({ddefs})")
    dph = ",".join("?" for _ in dcols)
    batch: list[tuple] = []
    for dyn_id, _, _ in keep:
        row = src.execute("SELECT * FROM dynamics WHERE dyn_id=?", (dyn_id,)).fetchone()
        batch.append(tuple(row))
        if len(batch) >= 2000:
            dst.executemany(f"INSERT INTO dynamics VALUES ({dph})", batch)
            batch = []
    if batch:
        dst.executemany(f"INSERT INTO dynamics VALUES ({dph})", batch)
    # images
    try:
        icols = [r[1] for r in src.execute("PRAGMA table_info(dynamic_images)")]
        idefs = ", ".join(f'"{r[1]}" {r[2]}' for r in src.execute("PRAGMA table_info(dynamic_images)"))
        dst.execute(f"CREATE TABLE dynamic_images ({idefs})")
        iph = ",".join("?" for _ in icols)
        ibatch: list[tuple] = []
        for (dyn_id,) in [(k[0],) for k in keep]:
            for row in src.execute("SELECT * FROM dynamic_images WHERE dyn_id=?", (dyn_id,)):
                ibatch.append(tuple(row))
                if len(ibatch) >= 2000:
                    dst.executemany(f"INSERT INTO dynamic_images VALUES ({iph})", ibatch)
                    ibatch = []
        if ibatch:
            dst.executemany(f"INSERT INTO dynamic_images VALUES ({iph})", ibatch)
        print(f"[info] images: {dst.execute('SELECT COUNT(*) FROM dynamic_images').fetchone()[0]}")
    except sqlite3.OperationalError as exc:
        print(f"[warn] images 跳过：{exc}")
    dst.execute("CREATE TABLE dyn_provenance(dyn_id TEXT PRIMARY KEY, labor_tier TEXT, labor_terms TEXT)")
    dst.executemany("INSERT INTO dyn_provenance VALUES (?,?,?)", keep)
    dst.execute("CREATE INDEX idx_dyn_mid ON dynamics(mid)")
    dst.execute("CREATE INDEX idx_dyn_pub ON dynamics(pub_time_iso)")
    dst.commit()

    # 校验
    errs = []
    n = dst.execute("SELECT COUNT(*) FROM dynamics").fetchone()[0]
    if n != len(keep):
        errs.append(f"dynamics {n} != {len(keep)}")
    strays = dst.execute(
        f"SELECT COUNT(*) FROM dynamics WHERE dyn_id NOT IN ({','.join('?'*len(keep))})",
        [k[0] for k in keep],
    ).fetchone()[0]
    if strays:
        errs.append(f"越界 {strays}")
    print(f"[verify] dynamics={n}")
    if errs:
        print("[FAIL]", errs)
        return 1
    print(f"[ok] {DST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
