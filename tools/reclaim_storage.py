#!/usr/bin/env python3
"""Recycle bulky media that already has transcript/OCR text."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bili.paths import LIBRARY_DIR  # noqa: E402
from bili.settings import load_settings  # noqa: E402
from bili.storage import KEEP_POLICIES, reclaim_library  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="删除、压缩或上传已完成转写的本地视频/图片")
    parser.add_argument(
        "--policy",
        default="delete_after_text",
        choices=KEEP_POLICIES,
        help="keep / delete_after_text / compress / upload_then_delete",
    )
    parser.add_argument("--library", default=str(LIBRARY_DIR), help="采集库目录")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    settings = load_settings()

    def on_log(level: str, message: str) -> None:
        print(f"[{level}] {message}")

    summary = reclaim_library(
        Path(args.library),
        policy=args.policy,
        ffmpeg_path=settings.ffmpeg_path,
        rclone_remote=settings.rclone_remote,
        rclone_root=settings.rclone_root,
        on_log=on_log,
        dry_run=args.dry_run,
    )
    mb = summary["bytes_freed"] / 1024 / 1024
    print(
        f"完成：{summary['folders']} 个目录，释放约 {mb:.1f} MB，"
        f"跳过进行中 {summary['skipped_running']}，错误 {summary['errors']}"
    )
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
