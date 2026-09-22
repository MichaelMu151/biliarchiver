# Windows 版 · 红米 G Pro 2024 / RTX 4060 Laptop

这是给 **Windows 游戏本 + NVIDIA 独显** 用的说明。目标机器：红米 G Pro 2024，NVIDIA GeForce RTX 4060 Laptop GPU（约 8 GB 显存）。

仓库：https://github.com/MichaelMu151/biliarchiver-windows  
（从 https://github.com/MichaelMu151/biliarchiver 分出，功能相同，安装路径按 Windows / CUDA 写。）

Mac 请看主仓库 README。

## 这台机器上要改什么

Intel Mac 上 Whisper 走 CPU，一条 30 分钟视频大约十分钟。4060 上应走 **CUDA float16**，同样内容通常是几十秒到一两分钟。

还需要处理三件 Windows 特有的事：

1. `ffmpeg.exe`（不是 macOS 的 `tools/bin/ffmpeg`）
2. pip 安装的 `nvidia-cublas-cu12` / `nvidia-cudnn-cu12`，并登记 DLL 目录（否则常见报错 `cudnn_ops64_9.dll`）
3. 核显 + 独显切换：把 `python.exe` 指定为 **高性能 NVIDIA**，否则会落到 Intel/AMD 核显，CUDA 设备数为 0

## 安装

1. 安装 [Python 3.12](https://www.python.org/downloads/windows/)（3.11 也可以）。安装时勾选 **Add python.exe to PATH**。不要用 3.14。
2. 安装当前 NVIDIA Game Ready 或 Studio 驱动。PowerShell 里执行 `nvidia-smi`，应能看到 `RTX 4060`。
3. 打开 PowerShell：

```powershell
git clone https://github.com/MichaelMu151/biliarchiver-windows.git
cd biliarchiver-windows
powershell -ExecutionPolicy Bypass -File tools\setup_windows.ps1
```

脚本会建虚拟环境、安装 GPU 依赖、尝试用 winget 装 ffmpeg，并运行 `tools\check_gpu.py`。

若脚本失败，可手动：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -r requirements-gpu-windows.txt
winget install --id Gyan.FFmpeg -e
python tools\check_gpu.py
```

`check_gpu.py` 里 `cuda_devices` 应大于 0。若仍是 0：

- Windows 设置 → 系统 → 显示 → 图形设置 → 浏览，选中 `.venv\Scripts\python.exe`，选项设为 **高性能 / NVIDIA**
- 关掉终端重开，再跑 `python tools\check_gpu.py`

也可把 `ffmpeg.exe` 放到 `tools\bin\ffmpeg.exe`。

## 启动

```powershell
.\.venv\Scripts\Activate.ps1
python run.py
```

浏览器打开 http://127.0.0.1:8765  
启动时终端会打印是否用到 GPU。界面「设置」里：

- Whisper 设备：自动
- GPU 精度：自动，或 float16
- 模型：`medium` 更准；显存够用时可用 `large-v3`

4060 Laptop 8 GB：`medium` + float16 很稳；`large-v3` + float16 大约占用 4–5 GB，一般也能跑。若 OOM，改成 `int8_float16`。

## 和 Mac 版的差异

| | Mac（主仓库） | 这台 Windows 本 |
| --- | --- | --- |
| Whisper | CPU int8，默认 small | CUDA float16，建议 medium |
| ffmpeg | `tools/bin/ffmpeg` 或 Homebrew | `ffmpeg.exe` / winget |
| 依赖 | `requirements-ai.txt` | `requirements-gpu-windows.txt` |
| 路径 | POSIX | 同样用 pathlib，中文目录可用 |

采集逻辑、产物目录、空间策略（提取后删除 / 压缩 / 上传 Google Drive）两边一样。数据仍在本机 `data\library\`。界面里 UID、Cookie、Bark、rclone、AutoDL、学术采样门禁该填什么，见主 [README.md](README.md)「界面里该填什么」。

学术采样默认不下载视频、不跑 Whisper；评论树和分析漏斗在 `data\corpus.db`。

## 采集结果去哪找

`data\library\<UID>_<UP名>\videos\<日期>_BV...\`

- `transcript.md` 转写
- `video.md` 里的 B 站链接（本地视频按空间策略可能已删除）
- `comments.jsonl` / `danmaku.jsonl`

更完整的文件表见主 [README.md](README.md)。
