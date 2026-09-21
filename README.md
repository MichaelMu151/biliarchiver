# BiliArchiver

面向研究与个人归档的 B 站公开数据采集工作台。在项目目录启动后，用浏览器操作，不需要改脚本。

采集结果默认写在本机 `data/library/`。Cookie、设置和 SQLite 也只保存在 `data/`，**不会提交到 Git**。

## 能采集什么

- 账号档案：粉丝 / 关注快照、签名、等级
- 视频：标题、简介、互动数据、评论、弹幕
- 语音转文字：官方/AI 字幕，或本地 Whisper
- 动态：正文、图片 OCR、评论
- 媒体：不下载、只留页面链接、下载音频、下载并合成视频

播放直链会过期，因此不会当成永久成果保存。长期入口始终是 BV 页面链接。

## 安装与启动

需要 **Python 3.13**（3.14 目前装不上 `onnxruntime`，OCR 会失败）。

```bash
cd bilibili_work
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

浏览器打开 http://127.0.0.1:8765

**Windows 游戏本（红米 G Pro 2024 / RTX 4060）请改用独立仓库和安装说明：**

- 仓库：https://github.com/MichaelMu151/biliarchiver-windows
- 步骤：[WINDOWS.md](WINDOWS.md)

可选增强（语音识别、OCR、合成 MP4、上传 Google Drive）：

```bash
pip install -r requirements-ai.txt
# ffmpeg：用于封装 m4a、合并 DASH、压缩视频
# rclone：用于上传 Google Drive（见下文）
```

Intel Mac 若 Homebrew 安装 ffmpeg 困难，可把静态 ffmpeg 放到 `tools/bin/ffmpeg`。

## 推荐工作方案

界面「新建任务」里三个预设会填好选项，仍可逐项改：

| 方案 | 下载什么 | 转写 | 媒体处理完后 |
| --- | --- | --- | --- |
| 轻量采集 | 不下载媒体 | 官方字幕 | 没什么可删 |
| 文字研究 | 仅音频 | 官方字幕，缺失时 Whisper | **删除音频，只留链接和文字** |
| 完整归档 | 音视频 | 同上 | **压缩到约 480p 后保留** |

另外可选手动空间策略：

- **提取后删除**：转写 / OCR 成功后删除本地视频、音频、图片；Markdown 里保留 B 站链接
- **压缩后保留**：视频压到约 480p，图片转 JPEG
- **上传云盘后删除**：用 rclone 传到 Google Drive，再删本地大文件
- **全部留在本机**：原始 MP4 / M4A / 图片都留着（最占磁盘）

运算过程中的优化：

1. 先尝试官方字幕。成功则不再下载视频。
2. 若最终会删除媒体，Whisper 只下载音频，不拉视频流。
3. DASH 分片合并后立即丢掉 `.m4s` / `.part` / `.tmp`。
4. 每一条视频处理完就回收，而不是等整个任务结束。
5. 进行中的 Whisper 条目不会被回收。

已经占满磁盘的旧结果，不必重跑任务：

```bash
python tools/reclaim_storage.py --policy delete_after_text
```

或在界面「设置与登录」点「回收已有媒体」。可加 `--dry-run` 先看将删除什么。

## 产物目录：去哪里找文件

根目录：`data/library/<UID>_<UP名>/`

```
data/library/
  3706976810830116_天选OMG/
    INDEX.md                 该账号索引
    profile.md / profile.json  档案
    stats.csv / snapshots.jsonl  粉丝关注时间序列
    videos/
      2026-08-22_BV1mj8C6LEvx_奴工理论…/
        video.md             可读摘要（含 B 站链接）
        meta.json            结构化元数据
        transcript.md        转写（阅读用）
        transcript.json      转写（带时间戳，分析用）
        transcript.srt       字幕
        transcript.vtt       字幕
        comments.jsonl       评论
        danmaku.jsonl        弹幕
        cloud_job.json       云端转写清单
        storage.json         媒体是否已删除/压缩/上传
        .pipeline.json       断点
        media/               仅在“保留/压缩”时存在
    dynamics/
      2026-08-01_<动态ID>/
        dynamic.md
        ocr.md / ocr.json    OCR 正文、坐标、置信度
        comments.jsonl
        images/              仅在保留图片时存在
```

| 你想找 | 打开 |
| --- | --- |
| 这个 UP 有哪些视频 | `INDEX.md` 或界面「采集结果」 |
| 转写全文 | 对应视频目录 `transcript.md` |
| 带时间戳的分段 | `transcript.json` / `.srt` / `.vtt` |
| 评论 | `comments.jsonl` |
| 弹幕 | `danmaku.jsonl` |
| 动态文字 | `dynamic.md` 与 `ocr.md` |
| 回看原片 | `video.md` 里的 `https://www.bilibili.com/video/BVxxxx` |
| 是否已删本地媒体 | 同目录 `storage.json` |
| 云盘路径 | `storage.json` 的 `drive_uris` |

`data/`、`.venv/`、Cookie 和 `tools/bin/ffmpeg` 都已被 `.gitignore` 排除。

## Google Drive（可选）

适合有 Google One 空间、又不想把原始视频堆在笔记本上的情况。

1. 安装 [rclone](https://rclone.org/downloads/)：`brew install rclone`
2. 配置远程，名称与界面里「rclone 远程名」一致，默认 `gdrive`：

```bash
rclone config
# 选 n → Google Drive → 按提示用浏览器登录你的 Google 账号
# 远程名填 gdrive
```

3. 任务里把空间策略选成「上传云盘后删除」，或：

```bash
python tools/reclaim_storage.py --policy upload_then_delete
```

文件会传到 `gdrive:BiliArchiver/<相对 data/library 的路径>`。本地只留下文字、链接和 `storage.json`。

## 云端 GPU 批量转写

若本机 CPU 转写太慢：任务中选择「下载音频」且**先不要删音频**，把对应账号目录传到 GPU 机器后：

```bash
pip install -r requirements-ai.txt
python tools/transcribe_worker.py data/library --model large-v3 --language auto
```

Worker 扫描 `cloud_job.json`，跳过已有 `transcript.json` 的视频。

## 续跑

- 下载先写 `.part`，支持 HTTP Range。
- 每个视频/动态有 `.pipeline.json`。重启或新任务只要签名一致，会复用评论、弹幕、OCR、转写。
- 媒体按空间策略删除后，不会在续跑时重新下载（除非你改回「全部留在本机」）。

## 验证

```bash
python -m unittest discover -s tests -v
```

## 使用边界

只采集你有权查看的公开内容，控制频率，不要用于骚扰或商业转售。Cookie 等同账号，只保存在本机 `data/settings.json`。视频版权归 UP 主与平台；本地或云盘缓存仅供个人研究与转写。
