#!/usr/bin/env python3
"""Launch BiliArchiver on http://127.0.0.1:8765"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.chdir(ROOT)

from bili.runtime import prepare_process, probe_compute  # noqa: E402

prepare_process()


def main() -> None:
    parser = argparse.ArgumentParser(description="BiliArchiver · B站数据采集工作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    gpu = probe_compute()
    import uvicorn

    print(f"\n  BiliArchiver 已启动 → http://{args.host}:{args.port}")
    print(f"  {gpu['note']}\n")
    uvicorn.run("app.server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
