#!/usr/bin/env python3
"""Launch full BERTopic on AutoDL GPU with a thin comment export (not whole library).

Prerequisites:
  1. AutoDL instance running (PyTorch image with NVIDIA GPU).
  2. In BiliArchiver UI → 设置与登录 → AutoDL 选「主题分析」→ SSH + 密码 → 接入分析 GPU
     (do NOT pick 采集转写; that would prefetch Whisper large-v3).
     Keep the page open, OR leave SSH credentials saved and this script will
     open an exclusive SSH for the analysis job.

What transfers (once):
  - comment_analysis_full.jsonl.gz  (~tens of MB; text+metadata only)
  - analyze-role allowlist: run_gpu_full_bertopic.py + requirements-analysis-gpu.txt

What does NOT transfer:
  - Whisper / gpu_worker / crawler / UI / media library / full corpus.db
    (optional --upload-db)

Pulls back:
  - data/exports/gpu_full_bertopic_metrics.json
  - TOPIC_EVOLUTION_FINDINGS.md
  - embedding cache meta (optional)

Example:
  .venv/bin/python tools/launch_bertopic_autodl.py
  .venv/bin/python tools/launch_bertopic_autodl.py --min-topic-size 150 --batch-size 512
"""

from __future__ import annotations

import argparse
import shlex
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bili.autodl import (
    DEFAULT_REMOTE_DIR,
    SESSION,
    _pick_python,
    _run,
    connect_exclusive,
    session_alive,
    ssh_nvidia_smi,
    stop_whisper_worker,
    upload_role_files,
)
from bili.settings import load_settings

EXPORT_LOCAL = ROOT / "data" / "exports" / "comment_analysis_full.jsonl.gz"
SCRIPT_LOCAL = ROOT / "run_gpu_full_bertopic.py"


def _emit(msg: str, level: str = "info") -> None:
    prefix = {"info": "[info]", "ok": "[ok]", "warn": "[warn]", "error": "[error]"}.get(level, "[info]")
    print(f"{prefix} {msg}", flush=True)


def _ensure_export(force: bool = False) -> Path:
    if EXPORT_LOCAL.is_file() and not force and EXPORT_LOCAL.stat().st_size > 1_000_000:
        _emit(f"reuse export {EXPORT_LOCAL} ({EXPORT_LOCAL.stat().st_size/1e6:.1f} MB)")
        return EXPORT_LOCAL
    _emit("exporting v_comment_analysis → JSONL.GZ (one-time thin transfer)…")
    import subprocess

    cmd = [
        str(ROOT / ".venv" / "bin" / "python"),
        str(SCRIPT_LOCAL),
        "--export-only",
        "--export-path",
        str(EXPORT_LOCAL),
    ]
    # fall back to system python if venv missing
    if not (ROOT / ".venv" / "bin" / "python").is_file():
        cmd[0] = sys.executable
    subprocess.check_call(cmd, cwd=str(ROOT))
    return EXPORT_LOCAL


def _sftp_put(client, local: Path, remote: str) -> None:
    sftp = client.open_sftp()
    try:
        # mkdir -p
        parts = remote.strip("/").split("/")
        cur = ""
        for p in parts[:-1]:
            cur += "/" + p
            try:
                sftp.stat(cur)
            except OSError:
                sftp.mkdir(cur)
        _emit(f"SFTP {local.name} → {remote} ({local.stat().st_size/1e6:.1f} MB)")
        sftp.put(str(local), remote)
    finally:
        sftp.close()


def _download(client, remote: str, local: Path) -> bool:
    sftp = client.open_sftp()
    try:
        try:
            sftp.stat(remote)
        except OSError:
            return False
        local.parent.mkdir(parents=True, exist_ok=True)
        sftp.get(remote, str(local))
        _emit(f"downloaded {remote} → {local}", "ok")
        return True
    finally:
        sftp.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run full BERTopic on AutoDL GPU")
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    parser.add_argument("--embedding-model", default="BAAI/bge-large-zh-v1.5")
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--min-topic-size", type=int, default=120)
    parser.add_argument("--force-export", action="store_true")
    parser.add_argument("--upload-db", action="store_true",
                        help="Also upload full corpus.db (usually unnecessary)")
    parser.add_argument("--exclusive", action="store_true",
                        help="Open dedicated SSH (disconnects UI tunnel first via connect_exclusive)")
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--skip-prefetch", action="store_true",
                        help="Skip re-downloading the embedding model (reuse HF cache)")
    parser.add_argument("--kill-whisper-worker", action="store_true", default=True,
                        help="Free VRAM by stopping gpu_worker before BERTopic (default on)")
    args = parser.parse_args()

    settings = load_settings()
    export = _ensure_export(force=args.force_export)
    if not SCRIPT_LOCAL.is_file():
        _emit(f"missing {SCRIPT_LOCAL}", "error")
        return 1

    # Acquire SSH
    exclusive_client = None
    if args.exclusive or not session_alive():
        if not (settings.autodl_ssh_command and settings.autodl_ssh_password):
            _emit(
                "AutoDL 未接入，且设置里没有 SSH。请先在 UI 选「主题分析」接入，"
                "或保存 SSH 指令/密码后再跑本脚本。",
                "error",
            )
            return 2
        _emit("opening exclusive SSH for BERTopic…")
        exclusive_client = connect_exclusive(_emit)
        client = exclusive_client
    else:
        role = SESSION.status().get("role") or ""
        if role == "collect":
            _emit("reusing collect-mode SSH; Whisper worker will be stopped to free VRAM", "warn")
        _emit(f"using existing AutoDL session → {SESSION.status().get('target')}")
        client = SESSION.client

    remote_dir = args.remote_dir.rstrip("/")
    try:
        smi = ssh_nvidia_smi(client)
        if smi:
            _emit(f"nvidia-smi {smi}", "ok")
        else:
            _emit("nvidia-smi empty — confirm PyTorch+GPU image", "warn")

        if args.kill_whisper_worker:
            stop_whisper_worker(client, _emit)

        upload_role_files(client, remote_dir, _emit, role="analyze")
        _sftp_put(
            client,
            export,
            f"{remote_dir}/data/exports/comment_analysis_full.jsonl.gz",
        )
        if args.upload_db:
            db = ROOT / "data" / "corpus.db"
            _sftp_put(client, db, f"{remote_dir}/data/corpus.db")

        python = _pick_python(client, _emit)
        quoted = shlex.quote(remote_dir)

        if not args.skip_install:
            _emit("installing GPU analysis stack (bertopic / torch already on image)…")
            _run(
                client,
                f"cd {quoted} && {shlex.quote(python)} -m pip install -q "
                f"-r requirements-analysis-gpu.txt",
                _emit,
                timeout=2400,
            )

        if not args.skip_prefetch:
            _emit(f"prefetch embedding model {args.embedding_model}…")
            _run(
                client,
                "export HF_ENDPOINT=https://hf-mirror.com HF_HOME=/root/autodl-tmp/huggingface "
                f"&& cd {quoted} && {shlex.quote(python)} -c "
                + shlex.quote(
                    "from sentence_transformers import SentenceTransformer; "
                    f"SentenceTransformer('{args.embedding_model}')"
                ),
                _emit,
                timeout=3600,
            )
        else:
            _emit("skip model prefetch (reuse HF / embedding cache)")

        run_cmd = (
            f"export HF_ENDPOINT=https://hf-mirror.com HF_HOME=/root/autodl-tmp/huggingface "
            f"TOKENIZERS_PARALLELISM=false && cd {quoted} && "
            f"{shlex.quote(python)} -u run_gpu_full_bertopic.py "
            f"--docs-jsonl data/exports/comment_analysis_full.jsonl.gz "
            f"--embedding-model {shlex.quote(args.embedding_model)} "
            f"--batch-size {args.batch_size} "
            f"--min-topic-size {args.min_topic_size}"
        )
        _emit("starting full BERTopic on GPU (this can take 1–3 hours on 515k)…")
        t0 = time.time()
        _run(client, run_cmd, _emit, timeout=6 * 3600, chatter=True, get_pty=True)
        _emit(f"remote job finished in {(time.time()-t0)/60:.1f} min", "ok")

        # Pull artifacts
        pairs = [
            (f"{remote_dir}/data/exports/gpu_full_bertopic_metrics.json",
             ROOT / "data" / "exports" / "gpu_full_bertopic_metrics.json"),
            (f"{remote_dir}/TOPIC_EVOLUTION_FINDINGS.md",
             ROOT / "TOPIC_EVOLUTION_FINDINGS.md"),
            (f"{remote_dir}/data/exports/embeddings_bge_full.meta.json",
             ROOT / "data" / "exports" / "embeddings_bge_full.meta.json"),
        ]
        for remote, local in pairs:
            _download(client, remote, local)

        _emit("done. Open TOPIC_EVOLUTION_FINDINGS.md and gpu_full_bertopic_metrics.json", "ok")
        return 0
    finally:
        if exclusive_client is not None:
            try:
                exclusive_client.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
