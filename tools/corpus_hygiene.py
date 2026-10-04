#!/usr/bin/env python3
"""Apply pilot-report data hygiene soft flags on corpus.db.

Marks:
  - highly offtopic gate-passed videos (comment bag other_pct >= threshold)
  - noise comments (empty / emoji-only / symbol-only / @-only / too short)

Never deletes rows. Downstream analysis should use v_comment_analysis /
v_video_analysis.

Examples:
  .venv/bin/python tools/corpus_hygiene.py --dry-run
  .venv/bin/python tools/corpus_hygiene.py --other-pct-min 98
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

from bili.analysis_hygiene import (
    clear_analysis_excludes,
    ensure_analysis_columns,
    find_offtopic_videos,
    mark_noise_comments,
    mark_offtopic_videos,
)
from bili.paths import CORPUS_DB_PATH


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def main() -> int:
    parser = argparse.ArgumentParser(description="Corpus data hygiene (soft flags)")
    parser.add_argument("--db", type=Path, default=CORPUS_DB_PATH)
    parser.add_argument("--other-pct-min", type=float, default=98.0,
                        help="Exclude videos whose comment bag other%% >= this (default 98)")
    parser.add_argument("--min-comments", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reset-excludes", action="store_true",
                        help="Clear previous offtopic_comment_bag excludes before re-marking")
    parser.add_argument("--skip-noise", action="store_true")
    parser.add_argument("--skip-videos", action="store_true")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "data" / "exports" / "hygiene_report.json")
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"[error] corpus db not found: {args.db}", file=sys.stderr)
        return 1

    conn = _connect(args.db)
    try:
        applied = ensure_analysis_columns(conn)
        if applied:
            print(f"[info] schema columns added: {', '.join(applied)}")
        else:
            print("[info] analysis columns already present")

        cleared = 0
        if args.reset_excludes and not args.dry_run:
            cleared = clear_analysis_excludes(conn, reason_prefix="offtopic_comment_bag")
            print(f"[info] cleared previous offtopic excludes: {cleared}")

        offtopic: list[dict] = []
        n_videos = 0
        if not args.skip_videos:
            offtopic = find_offtopic_videos(
                conn,
                other_pct_min=args.other_pct_min,
                min_comments=args.min_comments,
            )
            print(f"[info] offtopic candidates (other>={args.other_pct_min}%, "
                  f"n>={args.min_comments}): {len(offtopic)}")
            for item in offtopic:
                print(
                    f"  - {item['bvid']}  other={item['other_pct']:5.2f}%  "
                    f"n={item['comments']:5d}  depth={item['depth']}  "
                    f"{item['title'][:48]}"
                )
            n_videos = mark_offtopic_videos(conn, offtopic, dry_run=args.dry_run)
            action = "would mark" if args.dry_run else "marked"
            print(f"[info] {action} {n_videos} videos as analysis_exclude=1")

        noise_counts: dict[str, int] = {}
        if not args.skip_noise:
            noise_counts = mark_noise_comments(conn, dry_run=args.dry_run)
            action = "would mark" if args.dry_run else "marked"
            print(f"[info] {action} noise comments: {noise_counts}")

        if not args.dry_run:
            conn.commit()

        # Post counts
        exclude_n = conn.execute(
            "SELECT COUNT(*) FROM videos WHERE IFNULL(analysis_exclude,0)=1"
        ).fetchone()[0]
        noise_n = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE IFNULL(is_noise,0)=1"
        ).fetchone()[0]
        analysis_comments = conn.execute(
            "SELECT COUNT(*) FROM v_comment_analysis"
        ).fetchone()[0]
        analysis_videos = conn.execute(
            "SELECT COUNT(*) FROM v_video_analysis"
        ).fetchone()[0]

        report = {
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "db": str(args.db),
            "dry_run": args.dry_run,
            "other_pct_min": args.other_pct_min,
            "min_comments": args.min_comments,
            "columns_added": applied,
            "cleared_excludes": cleared,
            "offtopic_videos": offtopic,
            "videos_marked": n_videos,
            "noise_counts": noise_counts,
            "totals_after": {
                "videos_analysis_exclude": exclude_n,
                "comments_is_noise": noise_n,
                "v_video_analysis": analysis_videos,
                "v_comment_analysis": analysis_comments,
            },
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[info] report → {args.out}")
        print(
            f"[done] exclude_videos={exclude_n}  noise_comments={noise_n}  "
            f"v_video_analysis={analysis_videos}  v_comment_analysis={analysis_comments}"
            + ("  (dry-run, DB unchanged)" if args.dry_run else "")
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
