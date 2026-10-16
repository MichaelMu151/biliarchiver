#!/usr/bin/env python3
"""导出任务历史归档：app.db(jobs) + corpus.db(collection_runs/gate漏斗)。

方法论写作需要“任务何时跑、配了什么参数、过了多少、卡在哪”——
这些只活在 app.db 的 progress_json/config_json 里，UI 重启内存日志就没了。
此脚本把它们固化成一份带时间的 JSON，方便写方法章节时引用。

用法：
  .venv/bin/python tools/dump_job_logs.py
  .venv/bin/python tools/dump_job_logs.py --out data/exports/job_history_20261016.json

输出结构：
  {"exported_at": ..., "jobs": [{"id","kind","label","status","created_at",
   "finished_at","config","progress","error","gate_funnel","videos_total",
   "videos_passed","edges"}, ...]}
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="导出任务历史归档（方法论写作留痕）")
    ap.add_argument("--out", default="", help="输出路径，默认 data/exports/job_history_<日期>.json")
    args = ap.parse_args()

    out = Path(args.out) if args.out else (
        ROOT / "data" / "exports" / f"job_history_{datetime.now().strftime('%Y%m%d')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    app = sqlite3.connect(ROOT / "data" / "app.db")
    app.row_factory = sqlite3.Row
    corpus = sqlite3.connect(ROOT / "data" / "corpus.db")
    corpus.row_factory = sqlite3.Row

    jobs = []
    for row in app.execute("SELECT * FROM jobs ORDER BY created_at"):
        job = dict(row)
        config = _load_json(job.get("config_json"))
        progress = _load_json(job.get("progress_json"))
        rid = job["id"]
        run = corpus.execute(
            "SELECT kind,label,status,started_at,finished_at,note FROM collection_runs WHERE run_id=?",
            (rid,),
        ).fetchone()
        funnel = [
            dict(g)
            for g in corpus.execute(
                "SELECT reason, COUNT(*) AS n FROM gate_decisions WHERE run_id=? GROUP BY reason ORDER BY n DESC",
                (rid,),
            )
        ]
        videos_total = corpus.execute(
            "SELECT COUNT(*) AS n FROM videos WHERE run_id=?", (rid,)
        ).fetchone()["n"]
        videos_passed = corpus.execute(
            "SELECT COUNT(*) AS n FROM videos WHERE run_id=? AND pass_filter=1", (rid,)
        ).fetchone()["n"]
        edges = corpus.execute(
            "SELECT COUNT(*) AS n FROM snowball_edges WHERE run_id=?", (rid,)
        ).fetchone()["n"]
        jobs.append(
            {
                "id": rid,
                "kind": config.get("kind") or (dict(run)["kind"] if run else ""),
                "label": (dict(run)["label"] if run else "") or config.get("label") or "",
                "status": job.get("status"),
                "created_at": job.get("created_at"),
                "finished_at": job.get("finished_at") or (dict(run)["finished_at"] if run else None),
                "run_started_at": dict(run)["started_at"] if run else None,
                "run_note": (dict(run)["note"] if run else "") or "",
                "config": config,
                "progress": progress,
                "error": job.get("error") or "",
                "gate_funnel": funnel,
                "videos_total": videos_total,
                "videos_passed": videos_passed,
                "snowball_edges": edges,
            }
        )

    payload = {
        "exported_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "jobs_count": len(jobs),
        "jobs": jobs,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    kinds: dict[str, int] = {}
    for job in jobs:
        kinds[job["kind"] or "?"] = kinds.get(job["kind"] or "?", 0) + 1
    print(f"[ok] {len(jobs)} 个任务 → {out}")
    print(f"[ok] kind 分布：{kinds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
