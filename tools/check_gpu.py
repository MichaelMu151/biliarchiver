#!/usr/bin/env python3
"""Print whether Whisper can see the NVIDIA GPU."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bili.media import which_ffmpeg
from bili.runtime import prepare_process, probe_compute, register_cuda_dlls


def main() -> int:
    prepare_process()
    dlls = register_cuda_dlls()
    info = probe_compute()
    info["ffmpeg"] = which_ffmpeg()
    info["dll_dirs"] = dlls
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if info["cuda_devices"] > 0:
        print("\nGPU 可用。设置里把 Whisper 设备留在“自动”，模型建议 medium 或 large-v3。")
        return 0
    print("\n还不能用 CUDA。请检查：")
    print("1. 已安装 Game Ready / Studio 驱动，nvidia-smi 能看到 RTX 4060")
    print("2. 已执行：pip install -r requirements-gpu-windows.txt")
    print("3. Windows 设置 → 系统 → 显示 → 图形设置：把这个 Python 设为“高性能 NVIDIA”")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
