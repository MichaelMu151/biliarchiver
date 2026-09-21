#!/usr/bin/env python3
"""Idempotent ASR worker for a local or cloud GPU machine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bili.transcribe import _write_transcript_outputs, transcribe_local, whisper_available


def discover_jobs(target: Path) -> list[Path]:
    if target.is_file() and target.name == "cloud_job.json":
        return [target]
    if target.is_dir():
        return sorted(target.rglob("cloud_job.json"))
    return []


def resolve_audio(job_path: Path, job: dict) -> Path | None:
    configured = Path(job.get("local_audio") or "")
    if configured.is_file():
        return configured
    media_dir = job_path.parent / "media"
    for pattern in ("*.m4a", "*.audio.m4s", "*.mp3", "*.wav", "*.flac"):
        match = next(media_dir.glob(pattern), None) if media_dir.exists() else None
        if match:
            return match
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="扫描 cloud_job.json，把已经上传的本地音频转成 Markdown/JSON/SRT/WebVTT"
    )
    parser.add_argument("target", type=Path, help="data/library、账号目录、视频目录或 cloud_job.json")
    parser.add_argument("--model", default="small", help="tiny/base/small/medium/large-v3")
    parser.add_argument("--language", default="auto", help="auto/zh/en/ja...")
    parser.add_argument("--force", action="store_true", help="覆盖已有 transcript.json")
    args = parser.parse_args()

    if not whisper_available():
        print("缺少 faster-whisper：pip install -r requirements-ai.txt", file=sys.stderr)
        return 2
    jobs = discover_jobs(args.target.expanduser().resolve())
    if not jobs:
        print("没有找到 cloud_job.json", file=sys.stderr)
        return 2

    done = skipped = failed = 0
    for job_path in jobs:
        folder = job_path.parent
        if (folder / "transcript.json").exists() and not args.force:
            print(f"SKIP {folder.name} · 已有转写")
            skipped += 1
            continue
        try:
            job = json.loads(job_path.read_text(encoding="utf-8"))
            audio = resolve_audio(job_path, job)
            if not audio:
                print(f"WAIT {folder.name} · 缺少音频，请先上传 media 文件夹")
                skipped += 1
                continue
            print(f"RUN  {folder.name} · {audio.name} · model={args.model}")
            result = transcribe_local(str(audio), args.model, args.language)
            _write_transcript_outputs(folder, result)
            print(f"DONE {folder.name} · {len(result.get('segments') or [])} 段")
            done += 1
        except Exception as exc:
            print(f"FAIL {folder.name} · {exc}", file=sys.stderr)
            failed += 1
    print(f"\n完成 {done} · 跳过/等待 {skipped} · 失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

