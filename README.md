# BiliArchiver

面向研究与个人归档的 B 站公开数据采集工作台。在项目目录启动后用浏览器操作，不必改脚本。

采集结果写在本机 `data/library/`。Cookie、Bark Key、SSH 密码和 SQLite **只在 `data/`，不会进 Git**。

两条仓库：

- Mac / 通用：https://github.com/MichaelMu151/biliarchiver
- Windows + NVIDIA（CUDA）：https://github.com/MichaelMu151/biliarchiver-windows

## 两条工作线，不要混

| 你要做什么 | 去哪一页 | 分析单位 |
| --- | --- | --- |
| 把某几个 UP 主近一年的视频、动态、转写存下来 | **新建任务** | 账号档案 |
| 从几条「像研究对象」的视频出发，沿相关推荐抽样，主要要评论 | **学术采样** | 评论树 + 推荐图 |

学术采样默认**不下载视频、不跑 Whisper**。主题模型请用 `v_comment_corpus`，不要把视频台词和评论混在一起。

## 安装与启动

需要 **Python 3.13**（3.14 装不上 `onnxruntime`）。

```bash
cd biliarchiver
python3.13 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

浏览器打开 http://127.0.0.1:8765

Windows CUDA 步骤见 [WINDOWS.md](WINDOWS.md)。可选：`pip install -r requirements-ai.txt`（Whisper / OCR）。Intel Mac 可把 ffmpeg 放到 `tools/bin/ffmpeg`。

## 界面里该填什么

### 新建任务 · UID

打开 UP 主页，地址栏类似：

```
https://space.bilibili.com/208259
```

数字 `208259` 就是 UID。可以直接贴整段链接，多个账号换行。

### 设置 · 登录

优先扫码。手贴 Cookie 时：Chrome 打开 bilibili.com → F12 → Application → Cookies，复制整行，至少包含：

```
SESSDATA=....; bili_jct=....; DedeUserID=123456
```

Cookie 等同账号，不要发到网上。

### 设置 · Bark（可选，iPhone 进度）

安装 Bark 后，把 App 里的 Key 或完整链接贴进「Bark Key」：

```
https://api.day.app/你的Key/
```

没有 iPhone 就关掉「启用推送」。

### 设置 · rclone / Google Drive（可选）

```bash
brew install rclone    # Windows 见 rclone.org
rclone config          # 选 Google Drive，远程名例如 gdrive
```

界面「rclone 远程名」必须和 `rclone listremotes` 里的名字一致（常见 `gdrive` 或 `googledrive`）。「Drive 目录」例：`BiliArchiver`。

### 设置 · AutoDL 云端 GPU（可选）

爬虫仍在本机。到 AutoDL 实例页复制 **SSH 登录指令** 整行，不要自己改：

```
ssh -p 38491 root@connect.bjb1.seetacloud.com
```

密码是同一页的「登录密码」，不是 AutoDL 网站账号密码。点「一键接入」，保持页面开着。然后再到「新建任务」选文字研究或完整归档，步骤 6 选云端 GPU。

### 学术采样 · 种子与门禁

种子用**你已经判断像研究对象**的视频，不要用搞笑切片：

```
https://www.bilibili.com/video/BVxxxxxxxxxx
BV1yyyyyyyyyy
https://space.bilibili.com/208259
```

默认门禁（可改，但研究亚文化时建议先保持）：

| 栏位 | 默认 | 含义 |
| --- | --- | --- |
| 最低播放量 | 10000 | 去掉几乎没人看的切片 |
| 最低评论数 / 互动比 | 150 / 0.003 | 满足**任一**即过；挡住「高播低评」的视觉稿 |
| 分区黑名单 | 游戏,动画,番剧,… | 分区名里出现这些词就剪枝 |
| 分区白名单 | （空） | 若填 `知识,资讯`，则只留这些分区 |
| 标签词 | 就业,学历,失业,… | 标签、标题或简介命中任一。研究躺平可改成 `躺平,内卷,孔乙己,牛马,阶层` |
| 深度 / 节点 | 2 / 80 | 先小后大；推荐接口有频控 |

只有**通过门禁**的视频才会继续扩「相关推荐」。被剪掉的也会写入漏斗。连续风控 5 次会停，队列还在，可续跑。请先登录，否则评论会被截短。

### 分析数据集 · SQL 例子

```sql
-- 通过门禁的视频
SELECT bvid, title, tname, view_count FROM v_video_corpus WHERE pass_filter=1 LIMIT 20;

-- 一级长评（字数阈值可改）
SELECT content, like_count, text_length FROM v_comment_corpus
WHERE hierarchy_level='root' AND text_length>=20 LIMIT 50;

-- 剪枝原因
SELECT * FROM v_gate_funnel;

-- 推荐边
SELECT src_bvid, dst_bvid, dst_passed, dst_reject_reason FROM v_snowball_network LIMIT 50;
```

「回填现有档案」会把已经采好的 `data/library` 灌进 `corpus.db`。「导出 CSV」写到 `data/exports/`。

## 推荐工作方案（账号归档）

「新建任务」三个预设：

| 方案 | 下载什么 | 转写 | 之后 |
| --- | --- | --- | --- |
| 轻量采集 | 不下载媒体 | 官方字幕 | 几乎不占空间 |
| 文字研究 | 仅音频 | 官方字幕，缺则 Whisper | 音频上传 Drive 后删除 |
| 完整归档 | 音视频 | 同上 | 视频上传 Drive 后删除；Whisper/OCR 可选本机或云端 GPU |

空间策略还可选：提取后删除、压缩后保留、全部留在本机。云端 GPU 不是第四种档案类型，而是文字研究 / 完整归档的算力分支。

已占满磁盘的旧结果：

```bash
python tools/reclaim_storage.py --policy delete_after_text
```

## 产物目录

`data/library/<UID>_<UP名>/`

```
INDEX.md  profile.md  stats.csv  snapshots.jsonl
videos/<日期>_BVxxxx_标题/
  video.md  meta.json  transcript.md/.json/.srt/.vtt
  comments.jsonl  danmaku.jsonl  storage.json  .pipeline.json
dynamics/<日期>_<动态ID>/
  dynamic.md  ocr.md  comments.jsonl
```

分析库：`data/corpus.db`（与任务状态库 `data/app.db` 分开）。

长期入口永远是 `https://www.bilibili.com/video/BVxxxx`，播放直链会过期，不当成果保存。

## 续跑与风控

- 下载写 `.part`，支持 HTTP Range。
- 每个视频/动态有 `.pipeline.json`。
- 请求间隔随机抖动；HTTP 412 / B 站 -412、-352 会退避重试。
- 学术采样连续风控 5 次会停住并保留队列。
- 不要把最小间隔调到 0.2 秒以下。

## 验证

```bash
python -m unittest discover -s tests -v
```

## 使用边界

只采集你有权查看的公开内容，控制频率，不要用于骚扰或商业转售。Cookie 只保存在本机 `data/settings.json`。视频版权归 UP 主与平台；本地或云盘缓存仅供个人研究与转写。主页「IP 属地」是 App 接口，Web Cookie 拿不到；评论里的 IP 属地可以。
