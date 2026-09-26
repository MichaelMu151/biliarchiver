# BiliArchiver 零基础完整教程

---

## 目录（建议按顺序读）

1. [这到底是什么](#1-这到底是什么)
2. [先认识几个词](#2-先认识几个词)
3. [你要准备什么](#3-你要准备什么)
4. [Mac 安装（从零开始）](#4-mac-安装从零开始)
5. [认识浏览器里的界面](#5-认识浏览器里的界面)
6. [必做：登录 B 站](#6-必做登录-b-站)
7. [第一次采集：1 个 UP、近 3 个月（强烈建议）](#7-第一次采集1-个-up近-3-个月强烈建议)
8. [可选设置：Bark / Google Drive / 云端 GPU](#8-可选设置bark--google-drive--云端-gpu)
9. [学术滚雪球：视频转写 + 评论树 + 相关推荐](#9-学术滚雪球视频转写--评论树--相关推荐)
10. [分析数据集：怎么查、怎么导出](#10-分析数据集怎么查怎么导出)
11. [文件都在哪、打开后看什么](#11-文件都在哪打开后看什么)
12. [出错了怎么办（对照表）](#12-出错了怎么办对照表)
13. [Windows + NVIDIA 用户](#13-windows--nvidia-用户)
14. [使用边界与隐私](#14-使用边界与隐私)

仓库地址：

- Mac / 通用：https://github.com/MichaelMu151/biliarchiver
- Windows + RTX 4060 等：https://github.com/MichaelMu151/biliarchiver-windows

---

## 1. 这到底是什么

**BiliArchiver** 是一个在你**自己电脑**上运行的小程序：

1. 你在浏览器里填 UID、选选项、点按钮；
2. 它在后台按规则去 B 站公开页面拉数据（视频信息、评论、字幕等）；
3. 结果写进你硬盘上的文件夹和数据库，供你读论文、做统计、写论文。

它**不是** B 站官方工具，也**不能**绕过登录去看私密内容。它**不会**自动帮你做主题模型或写论文——它负责**把材料采齐、整理成能分析的结构**。

你可以把它理解成三条采集，外加同一套「从头开始 / 续跑」：

| 页面 | 你什么时候用 | 不会做什么 |
| --- | --- | --- |
| **按 UP 主采集** | 已知几个 UP，要把一段时间里的投稿、动态、评论整库归档 | 不会沿相关推荐扩散 |
| **学术滚雪球** | 从几条像研究对象的视频出发，沿相关推荐扩，并用门禁筛掉游戏鬼畜等噪声 | 不会把某个 UP 的全部投稿都搬下来 |
| **按视频号采集** | 已经有一批 BV，只补转写 / 评论，或重跑失败的那几条 | 不扩散、不过门禁 |

三条**共存**。学术滚雪球里通过门禁的视频，会同时采 **视频台词** 和 **评论树**——评论和视频话语是两层分析单位，不要混成一张表做主题模型。

每个采集页底部都有两个按钮：

- **从头开始**：新建一个任务号，按你现在填的表单跑。
- **续跑选中任务**：接着下拉框里那一次（通常是中断或取消的）。沿用当时保存的配置和磁盘检查点，已完成的视频会跳过。

换 UID、换种子或换 BV 列表时用「从头开始」。只是电脑关机、风控停下、网页断了，用「续跑」。运行监控顶部的任务记录里也能续跑。

---

## 2. 先认识几个词

后面会反复出现，先扫一眼即可，用到时再回来查。

| 词 | 大白话 |
| --- | --- |
| **终端 / Terminal** | Mac 里一个黑底或白底的窗口，用键盘输入命令让电脑执行。打开方式：Spotlight（Command + 空格）输入 `Terminal` 回车。 |
| **命令** | 你在终端里输入一行文字后按回车，例如 `python run.py`。 |
| **项目文件夹** | 你下载的 `biliarchiver` 整个目录。里面有很多 `.py` 文件，**一般不要手改**。 |
| **虚拟环境 `.venv`** | 给这个项目单独装的一套 Python 库，不影响系统别的软件。激活后命令行前面会出现 `(.venv)`。 |
| **UID** | B 站 UP 主的数字 ID。打开 `https://space.bilibili.com/208259` 时，`208259` 就是 UID。 |
| **BV 号** | 单个视频的唯一编号，形如 `BV1xxxxxxxx`。 |
| **Cookie** | 浏览器登录 B 站后，网站发给你电脑的一串「通行证」。复制给本程序后，采集器可以**以你的身份**读评论等，只保存在本机。 |
| **转写 / 字幕** | 把视频里说的话变成文字。优先用 B 站官方字幕；没有时可用 Whisper 语音识别（较慢）。 |
| **Whisper** | 开源语音识别模型。Intel Mac 上很慢；有 NVIDIA 显卡或云端 GPU 会快很多。 |
| **SQLite / corpus.db** | 一种单文件数据库。用 DB Browser、pandas、R 等打开，一行一条评论或视频，方便统计。 |
| **rclone** | 命令行工具，把本机文件夹同步到 Google Drive 等网盘。 |
| **AutoDL** | 租 GPU 的云平台。本程序用它时：**爬虫仍在你电脑**，只把音频送过去做 Whisper。 |
| **Bark** | iPhone 上的一个推送 App。任务跑很久时，手机会收到「又完成一条视频」之类的提醒。 |
| **127.0.0.1:8765** | 本机网页地址。只有你自己这台电脑能打开，不是公网网站。 |
| **门禁** | 学术滚雪球里的筛选规则。只有同时满足分区、播放量、互动、标签的视频才会继续沿推荐往外滚。 |
| **滚雪球** | 从种子视频出发，看 B 站「相关推荐」，一层层扩散，像滚雪球一样扩大样本。 |

---

## 3. 你要准备什么

### 3.1 硬件与系统

- **Mac**：macOS，建议 8 GB 内存以上；硬盘请留 **至少 10 GB** 空闲（若下载视频则更多）。
- **Windows + NVIDIA**：见 [第 13 节](#13-windows--nvidia-用户) 和 [WINDOWS.md](WINDOWS.md)。
- **网络**：能稳定打开 bilibili.com。采集会持续几小时，尽量不要让电脑休眠（可在系统设置里临时关闭自动睡眠）。

### 3.2 账号与权限

- 一个能正常登录的 **B 站账号**（用于 Cookie / 扫码）。
- 只采集**你有权查看的公开内容**，遵守平台规则与学术伦理。

### 3.3 不需要提前会的

- 不需要会 Python 编程；
- 不需要会 SQL（教程里会给复制粘贴的查询）；
- 不需要会 Linux。

### 3.4 数据存在哪（重要）

| 内容 | 路径 | 会不会上传 GitHub |
| --- | --- | --- |
| 采集的 Markdown、JSONL | `data/library/` | **不会**（已在 .gitignore） |
| 分析库 | `data/corpus.db` | **不会** |
| Cookie、Bark Key、SSH 密码 | `data/settings.json` | **不会** |

**千万不要**把 `data/` 文件夹或 Cookie 发到群里、邮件、GitHub Issue。

---

## 4. Mac 安装（从零开始）

下面每一步：先 **做什么** → **输入什么** → **成功时长什么样** → **失败了怎么办**。

### 4.1 安装 Python 3.13

1. 打开 **终端**（Command + 空格，输入 `Terminal`，回车）。
2. 输入下面这一行（可以复制粘贴），按回车：

```bash
python3.13 --version
```

**成功：** 显示 `Python 3.13.x`（x 是小版本号）。

**失败：`command not found`**

1. 浏览器打开 https://www.python.org/downloads/
2. 下载 **Python 3.13.x** 的 macOS 安装包（不要选 3.14）。
3. 双击安装，一路继续。
4. **关掉终端窗口，重新开一个**，再执行 `python3.13 --version`。

### 4.2 安装 Git（若还没有）

在终端输入：

```bash
git --version
```

若提示没有 git：安装 Xcode 命令行工具（终端会弹窗，点安装），或从 https://git-scm.com 安装。

### 4.3 下载 BiliArchiver

在终端**依次**输入（每行回车一次）：

```bash
cd ~
git clone https://github.com/MichaelMu151/biliarchiver.git
cd biliarchiver
pwd
```

最后一行 `pwd` 应显示类似 `/Users/你的用户名/biliarchiver`。

**若你已经在桌面等地方有这份代码**（例如 `Desktop/bilibili_work`），则：

```bash
cd /Users/你的用户名/Desktop/bilibili_work
pwd
```

后面所有命令都在**这个项目文件夹里**执行。

### 4.4 创建并激活虚拟环境

```bash
python3.13 -m venv .venv
source .venv/bin/activate
```

**成功：** 命令行最左边出现 `(.venv)`，例如：

```
(.venv) yourname@MacBook biliarchiver %
```

**以后每次新开终端都要先做：**

```bash
cd ~/biliarchiver    # 改成你的实际路径
source .venv/bin/activate
```

没有 `(.venv)` 就装依赖、跑程序，会报 `ModuleNotFoundError`。

### 4.5 安装 Python 依赖

确认有 `(.venv)` 后：

```bash
pip install -U pip
pip install -r requirements.txt
```

需要几分钟。出现 `Successfully installed ...` 一类字样即可。

**暂时不要**执行 `pip install -r requirements-ai.txt`，除非你已经确定要用 Whisper 或动态 OCR。基础采集不需要。

### 4.6 启动程序

```bash
python run.py
```

**成功：** 终端里出现两行类似：

```
BiliArchiver 已启动 → http://127.0.0.1:8765
未检测到 NVIDIA GPU，使用 CPU
```

以及 `Uvicorn running on http://127.0.0.1:8765`。

**这个终端窗口要一直开着。** 最小化可以，但不要关。关了程序就停。

### 4.7 用浏览器打开界面

1. 打开 Chrome 或 Safari。
2. 地址栏输入：`http://127.0.0.1:8765` 回车。

**成功：** 看到左侧菜单分成两组。「三种采集」是 **按 UP 主采集**、**学术滚雪球**、**按视频号采集**；下面是使用指南、运行监控、采集结果、分析数据集、设置与登录。

**失败：`无法访问此网站` / `ERR_CONNECTION_REFUSED`**

- 回到终端看 `python run.py` 是否还在跑；若已退出，回到 4.4～4.6 重做。
- 确认地址是 `127.0.0.1` 不是 `localhost` 拼错，端口是 `8765`。

**页面还是旧菜单（「新建任务 / 学术采样 / 按 BV 转写」）：** 硬刷新（Command + Shift + R）。

---

## 5. 认识浏览器里的界面

左边菜单 = 不同页面。下面按**建议使用顺序**介绍。

### 5.1 使用指南

纯说明，不用填东西。第一次可快速浏览「操作步骤」和「学术滚雪球」两段。

### 5.2 按 UP 主采集

给 **一个或多个 UP 主 UID**，按时间范围采视频、动态、评论、字幕等。适合「档案馆」式采集。页底 **从头开始** 开新任务；若上次同一批账号没跑完，在下拉框选中它再点 **续跑选中任务**。

### 5.3 学术滚雪球

给 **种子视频或 BV 号**，沿相关推荐扩散，带门禁筛选。适合论文抽样。默认同时采 **视频转写** 和 **评论树**。中断后不要再点「从头开始」（那会新开一轮队列），选原来的任务号续跑。

### 5.4 按视频号采集

只处理你粘贴的 **BV 列表**。不扩相关推荐，也不过门禁。适合补漏、重跑 OCR 兜底或转写失败的视频。勾选「跳过已有口播转写」时，从头开始会放过已经有 Whisper / 官方字幕的 BV；续跑则沿用那次任务保存的列表和检查点。

### 5.5 运行监控

任务开始后自动跳转到这里。上方是三种采集的最近任务，可直接续跑；下面是当前进度、日志和取消。

### 5.6 采集结果

浏览已采账号、点进某个视频的 Markdown。给**人读**用。

### 5.7 分析数据集

看 `corpus.db` 里有多少视频/评论、门禁漏斗、执行 SQL、导出 CSV。给**统计软件**用。

### 5.8 设置与登录

扫码或 Cookie 登录；Bark、Google Drive、AutoDL；Whisper 模型、请求间隔等。**多数任务开始前至少要完成登录。**

### 5.9 左下角两个小提示

- **药丸**：是否已登录 B 站。
- **library 路径**：数据写在哪个文件夹（一般是 `data/library`）。

---

## 6. 必做：登录 B 站

**不登录也能跑，但你会吃亏：** 评论少、没有评论 IP 属地、字幕和空间列表更容易失败。学术滚雪球和完整归档**务必登录**。

### 6.1 方法一：扫码（推荐）

1. 点左侧 **设置与登录**。
2. 点 **生成二维码**。
3. 手机哔哩哔哩 App → 右上角扫一扫 → 扫电脑上的码 → 手机上确认登录。
4. 等几秒，左下角药丸应变为已登录状态。

二维码过期：再点「生成二维码」。多次失败改用 6.2。

### 6.2 方法二：粘贴 Cookie

适合：电脑 Chrome 已经登录 B 站。

1. Chrome 打开 https://www.bilibili.com ，确认右上角是你的头像。
2. 按 `F12` 打开开发者工具（Mac 部分键盘：`fn + F12`）。
3. 顶部点 **Application**（中文可能是「应用程序」）。
4. 左侧 **Cookies** → 点击 `https://www.bilibili.com`。
5. 在右侧表格找到至少这三项，记下了 `Name` 和 `Value`：
   - `SESSDATA`（最长的一串）
   - `bili_jct`
   - `DedeUserID`
6. 拼成**一行**（英文分号 + 空格分隔）：

```
SESSDATA=这里粘贴SESSDATA的值; bili_jct=这里粘贴bili_jct; DedeUserID=这里粘贴数字
```

7. 粘贴到界面 **Cookie 字符串** 大框里。
8. 点 **保存 Cookie**。框会清空，下面出现 `已保存 SESSDATA=xxxx****yyyy` 是正常的（脱敏显示）。
9. 左下角药丸显示已登录。

**Cookie 等于你的账号，不要发给任何人。**

---

## 7. 第一次采集：1 个 UP、近 3 个月（强烈建议）

目标：确认「登录 → 开任务 → 看监控 → 看结果」整条链路是通的。**先不要开学术滚雪球，先不要开 Whisper。**

### 7.1 找一个 UP 的 UID

1. 浏览器新开标签，打开任意 UP 空间，例如：

```
https://space.bilibili.com/208259
```

2. 地址里 `space.bilibili.com/` 后面的数字就是 **UID**（例：`208259`）。复制它。

### 7.2 填写按 UP 主采集

1. 回到 `http://127.0.0.1:8765`，点 **按 UP 主采集**。
2. 最上面三个方案卡片，点 **轻量采集**（会自动填好下面选项）。
3. **步骤 1**：在大文本框粘贴 UID（或整段空间链接）。时间范围点 **近 3 个月**（试跑用，正式可改近 1 年）。
4. **步骤 2**：保持默认全勾（档案、视频、动态、评论、弹幕、OCR）。续跑不在这里勾，而在页底单独按钮。
5. **步骤 3～5**：轻量采集已是「保留页面链接 + 官方字幕优先」，不用改。
6. 最下面点 **从头开始**。若上次同一批账号中断了，改为在下拉框选中那次任务，点 **续跑选中任务**。

### 7.3 看运行监控

- 自动跳到 **运行监控**。
- **状态** 会从 queued → running。
- 日志里会出现 `任务启动`、`视频完成 BVxxxx` 等。
- 第一个视频可能要等 1～3 分钟，属正常。

**不要关** 运行 `python run.py` 的那个终端。

### 7.4 看采集结果

1. 任务跑完或先跑完几个视频后，点 **采集结果**。
2. 左侧点你的 UID 对应账号名。
3. 右侧应能看到视频列表；点某个视频可打开 `video.md`。

磁盘上路径（Finder 里「前往 → 前往文件夹」粘贴）：

```
/Users/你的用户名/biliarchiver/data/library/
```

（若项目在 Desktop，则是 `.../Desktop/bilibili_work/data/library/`）

### 7.5 这次成功后你学到了什么

- 程序能连 B 站、能写文件；
- 登录有效；
- 你知道监控页和结果页在哪。

**然后再做** 第 8 节（可选）或第 9 节（学术滚雪球）。

---

## 8. 可选设置：Bark / Google Drive / 云端 GPU

**全部可以跳过。** 只有遇到对应需求再回来配。

### 8.1 Bark（iPhone 进度推送）

**干什么：** 任务跑 2～8 小时时，手机通知「开始 / 完成一条 / 全部结束 / 失败」，不用一直盯屏幕。

**什么时候要：** 你人不在电脑旁，又想知道进度。

**什么时候不要：** 没有 iPhone；任务很短。

**步骤：**

1. iPhone App Store 安装 **Bark**。
2. 打开 App，复制 **Key** 或整条链接 `https://api.day.app/xxxx/`。
3. 电脑界面 **设置与登录** → 滚到 **手机进度提醒（Bark）**。
4. 勾选「启用推送」，粘贴 Key，点 **保存设置**，点 **发送测试**。
5. 手机应收到测试通知。

没收到：检查 iPhone 通知权限、网络、Key 是否完整。

### 8.2 Google Drive（rclone）

**干什么：** 「文字研究 / 完整归档 / 学术滚雪球开 Whisper」会在本机留下大音频或 MP4。转写文字已经进数据库了，原片可以上传 Google Drive 后**删除本地**，省硬盘。

**什么时候要：** 你选了「上传云盘后删除」这类空间策略。

**什么时候不要：** 只用「轻量采集」或「提取后删除」且不上传。

**Mac 配置步骤：**

1. 终端（可新开一个窗口，项目目录下 `source .venv/bin/activate` 可有可无）：

```bash
brew install rclone
```

没有 Homebrew：先安装 https://brew.sh

2. 配置 Drive：

```bash
rclone config
```

交互式问答建议：

- `n` 新建
- name: `googledrive`（记住这个名字）
- Storage: 选 **Google Drive**（看列表编号，输入对应数字）
- client_id / secret: 直接回车
- scope: 选 **full access** 那一项（常见是 1）
- 其余大多回车
- 问是否在浏览器配置：选 `y`，浏览器登录 Google 并允许

3. 验证：

```bash
rclone listremotes
```

应看到 `googledrive:`。再：

```bash
rclone lsd googledrive:
```

能列出文件夹即成功。

4. 界面 **Google Drive（rclone）**：
   - 远程名：`googledrive`（与 listremotes 一致，**不要写冒号**）
   - 目录：`BiliArchiver`
5. **保存设置**。

之后在任务里选「上传云盘后删除」。若 rclone 没配好就点开始，会报错——改回「提取后删除」即可。

### 8.3 AutoDL 云端 GPU

**干什么：** 视频没有官方字幕时必须 Whisper。Intel Mac CPU 很慢；把**音频**送到 AutoDL 的 GPU 上识别，文字回写到本机。

**重要：** 爬虫、评论、门禁、滚雪球队列**仍在你的 Mac**；云端只算 Whisper。

**步骤：**

1. AutoDL 控制台开机实例（如 3090 / 4090），等「运行中」。
   - 镜像选 **PyTorch 官方镜像 + Python 3.10～3.12**。**不要**选空白 Ubuntu。
   - 控制台「驱动/CUDA」写的是**这张卡所在物理机的驱动上限**，不是镜像版本。换一张卡往往会变：
     | 驱动旁边写的 CUDA | 镜像应选 |
     | --- | --- |
     | 12.8（驱动 570.x） | PyTorch **CUDA 12.x**（12.1 / 12.4 / 12.8），不要选 13.0 |
     | 13.2（驱动 595.x） | CUDA **12 或 13** 都可以 |
   - 你之前那台 4090 是驱动 580 / CUDA 13.0，选 `PyTorch / 2.12.1 / 3.12 / 13.0` 是对的。
   - Whisper 的 pip 包仍要 `libcublas.so.12`，一键接入会自动补装，和上面选 12 还是 13 **无关**。
2. 在**该实例页**复制：
   - **SSH 登录指令**（整行，含 `-p 端口`），例：  
     `ssh -p 38491 root@connect.bjb1.seetacloud.com`
   - **登录密码**（实例页上的，**不是** AutoDL 网站登录密码）
3. 界面 **AutoDL 云端 GPU** 粘贴上述两项。
4. 点 **一键接入**，看下方日志滚动。**保持此浏览器标签页开着**。
5. 日志显示就绪后，按 UP 主采集的步骤 6，或学术滚雪球 / 按视频号采集里的算力选项，选 **云端 GPU**。

**不要**自己在终端 ssh 上去改项目路径；界面已封装。

### 8.4 采集节奏（第一次别改）

- 最小间隔 ~0.8 秒、最大 ~1.8 秒：防 B 站风控。
- **不要**调到 0.2 秒以下。
- Whisper 模型：Intel Mac 用 `small`；有 4060 用 `medium`（见 WINDOWS.md）。

---

## 9. 学术滚雪球：视频转写 + 评论树 + 相关推荐

**前提：** 已完成第 6 节登录，建议已完成第 7 节一次归档试跑。

### 9.1 你要得到什么（研究向）

对每一条**通过门禁**的视频：

| 层级 | 内容 | 进哪个表 / 视图 |
| --- | --- | --- |
| 视频元数据 | 标题、分区、标签、播放/评论数、在推荐图第几层 | `videos` / `v_video_corpus` |
| 视频话语 | 官方字幕或 Whisper 台词 | `transcripts` / `v_transcript_corpus` |
| 评论话语 | 一级热评 + 楼中楼，带父子关系 | `comments` / `v_comment_corpus` |
| 推荐关系 | 谁推荐了谁 | `snowball_edges` / `v_snowball_network` |
| 为什么被删 | 分区/标签/互动不达标 | `gate_decisions` / `v_gate_funnel` |

**分析时：** 台词用 `v_transcript_corpus`，评论用 `v_comment_corpus`，**不要 `UNION` 成一张表做 BERTopic**。

### 9.2 种子怎么选（决定成败）

**好的种子：** 你已经看过、确认在讨论就业/阶层/躺平等议题的**完整讨论视频**（知识区、社会议题向）。

**坏的种子：** 鬼畜切片、纯搞笑、只有画面没有讨论、标题党但评论跑题。

种子示例（学术滚雪球 → 步骤 1，一行一个）：

```
https://www.bilibili.com/video/BV1xxxxxxxx
BV1yyyyyyyyyy
```

也可贴 UID/空间链接：程序会取该 UP 在时间窗口内最近若干条**再跑门禁**——不等于全部会用，不合格的会剪枝。

### 9.3 门禁默认含义（步骤 2）

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| 最大深度 | 2 | 从种子算，沿推荐往外扩 2 层 |
| 节点上限 | 80 | 最多保留 80 个**通过门禁**的节点 |
| 最低播放 | 10000 | 低于则剪枝 |
| 最低评论 / 互动比 | 150 / 0.003 | **满足任一**即过（评论≥150 或 评论÷播放≥0.003） |
| 分区黑名单 | 游戏,动画,影视… | 分区名命中则剪枝 |
| 标签词 | 就业,学历,失业… | 标签、标题或简介命中**任一** |
| 时间窗口 | 近 1 年 | 太老的视频剪枝 |

研究「躺平」时，把标签词改为：

```
躺平,内卷,孔乙己,牛马,阶层
```

### 9.4 步骤 3～4（采什么、怎么转文字）

**步骤 3** 请保持勾选：

- ✅ 采集视频转写（台词 / 字幕）
- ✅ 采集评论树（热评 + 楼中楼）

**步骤 4 推荐默认（磁盘友好）：**

- 转写：**官方字幕 + Whisper 兜底**
- 媒体：**下载音频**（给 Whisper 用）
- 空间：**提取后删除音频**（文字进库后删本地音频）
- 算力：**本机**（慢但简单）；接好 AutoDL 后改 **云端 GPU**

**Intel Mac 试跑捷径：** 步骤 4 先选「只取官方字幕」+「不下载媒体」，确认滚雪球和评论都正常，再开 Whisper。

### 9.5 开始与监控

1. 点 **从头开始** → 自动到 **运行监控**。中途停下后，回到本页下拉框选这次任务，点 **续跑选中任务**（不要再从头开始，否则会新开一轮）。
2. 日志会出现：`学术滚雪球启动`、`剪枝 BVxxxx · category/tags/engagement`、`xxx 相关 N · 新入队 M`、`视频完成`。
3. **剪枝很多是正常的**——说明门禁在挡噪声。
4. 连续 **5 次风控** 会自动停，队列保留。改天在下拉框选中这一次，点 **续跑选中任务**。

### 9.6 跑完怎么判断「样本对不对」

到 **分析数据集**：

1. 看上方统计：视频数、评论数是否 > 0。
2. 看 **门禁漏斗**：`pass` vs `category` / `tags` / `engagement` 各占多少。
3. 点 **执行** 跑默认 SQL，看 `pass_filter=1` 的标题是否还像研究对象。

若几乎全被 `category` 剪掉：种子太娱乐，或黑名单不够/白名单要加 `知识,资讯`。  
若几乎全被 `tags` 剪掉：改标签词匹配你的题目。  
若几乎全被 `engagement` 剪掉：试跑可把最低评论临时降到 50（正式写论文再改回并记录偏差）。

---

## 10. 分析数据集：怎么查、怎么导出

### 10.1 打开页面

左侧 **分析数据集**。库文件路径：

```
data/corpus.db
```

与 `data/app.db`（任务进度）不同，**做论文用 corpus.db**。

### 10.2 复制粘贴这些 SQL（点「执行」）

**通过门禁的视频：**

```sql
SELECT bvid, title, tname, view_count, depth, pass_filter
FROM v_video_corpus
WHERE pass_filter = 1
ORDER BY depth, view_count DESC
LIMIT 30;
```

**视频台词（单独分析）：**

```sql
SELECT bvid, video_title, source, char_count, full_text
FROM v_transcript_corpus
WHERE pass_filter = 1
LIMIT 20;
```

**一级长评（单独分析，字数≥20）：**

```sql
SELECT bvid, content, like_count, text_length, ip_location
FROM v_comment_corpus
WHERE hierarchy_level = 'root' AND text_length >= 20
ORDER BY like_count DESC
LIMIT 50;
```

**剪枝原因统计：**

```sql
SELECT * FROM v_gate_funnel;
```

**推荐网络：**

```sql
SELECT src_bvid, dst_bvid, dst_passed, dst_reject_reason
FROM v_snowball_network
LIMIT 50;
```

### 10.3 导出 CSV

点 **导出 CSV**，到 Finder 打开：

```
data/exports/
```

用 Excel、R、Stata、Python pandas 打开均可。

### 10.4 回填旧档案

若你**先**用「按 UP 主采集」采过一堆，**后**才要用 SQLite：点 **回填现有档案**，程序会扫描 `data/library/` 灌进 `corpus.db`（可能要几分钟）。

---

## 11. 文件都在哪、打开后看什么

### 11.1 给人读的（Markdown）

```
data/library/<UID>_<UP主名>/
├── INDEX.md          # 账号总索引
├── profile.md        # 当前粉丝/简介等
├── stats.csv         # 快照表格
├── snapshots.jsonl   # 每次采集的快照一行
├── videos/
│   └── 2024-01-01_BVxxxx_标题/
│       ├── video.md       # 汇总：链接、数据、嵌入字幕
│       ├── meta.json      # 机器可读元数据
│       ├── transcript.md  # 字幕/转写全文
│       ├── comments.jsonl # 一行一条评论 JSON
│       └── danmaku.jsonl
└── dynamics/
    └── ...
```

用 **Typora、Obsidian、VS Code、记事本** 都能打开 `.md`。

### 11.2 给程序读的（SQLite）

| 表 | 一行代表什么 |
| --- | --- |
| `videos` | 一个视频 + 滚雪球深度、是否通过门禁 |
| `transcripts` | 一个视频的一路转写全文 |
| `comments` | 一条评论（含 root/parent 指针） |
| `danmaku` | 一条弹幕 |
| `dynamics` | 一条动态 |
| `snowball_edges` | 一条「A 推荐了 B」的边 |
| `gate_decisions` | 一次门禁判定（含拒绝原因） |

免费查看工具：[DB Browser for SQLite](https://sqlitebrowser.org/)。

### 11.3 永久链接 vs 播放直链

成果里应保存：

```
https://www.bilibili.com/video/BVxxxx
```

不要保存会过期的 CDN 播放 URL。

---

## 12. 出错了怎么办（对照表）

| 你看到的 | 可能原因 | 怎么办 |
| --- | --- | --- |
| `ERR_CONNECTION_REFUSED` | 程序没启动 | 终端 `cd` 到项目 → `source .venv/bin/activate` → `python run.py` |
| `ModuleNotFoundError` | 没激活虚拟环境 | `source .venv/bin/activate` 后重试 |
| `command not found: python3.13` | 没装 Python 3.13 | 见 4.1 |
| 左下角一直未登录 | Cookie 无效或未保存 | 重新扫码或粘贴 Cookie |
| 评论很少 / 无 IP | 未登录 | 第 6 节 |
| 日志里 `-412` / `412` | 请求太快或被风控 | 设置里间隔调大；等 30 分钟再续跑 |
| 学术滚雪球全剪枝 | 种子或门禁太严 | 看 `v_gate_funnel`；调标签/白名单/试跑降阈值 |
| 连续风控停止 | 5 次 B 站拦截 | 等一段时间，同一任务再开始（队列在） |
| 点开始提示要 rclone | 选了上传云盘但未配置 | 改「提取后删除」或完成 8.2 |
| 点开始提示 GPU 未就绪 | 选了云端 Whisper 但未接入 AutoDL | 改「本机」或完成 8.3 |
| Whisper 极慢 | Intel Mac CPU 正常 | 只取官方字幕，或 AutoDL / Windows GPU |
| 磁盘满 | 下载了太多视频 | `python tools/reclaim_storage.py --policy delete_after_text` |
| 页面像旧版 | 浏览器缓存 | Command + Shift + R |

**停止程序：** 在运行 `python run.py` 的终端按 `Control + C`。

**不要**把 Cookie、SSH 密码、整份 `data/` 发给他人帮你「远程调试」。

---

## 13. Windows + NVIDIA 用户

1. 克隆 https://github.com/MichaelMu151/biliarchiver-windows
2. 按 [WINDOWS.md](WINDOWS.md) 运行 `tools\setup_windows.ps1`
3. `python run.py` 打开 http://127.0.0.1:8765
4. **从本 README 第 5 节「认识界面」开始**，与 Mac 完全相同

4060 上 Whisper 建议：`medium` + `float16`（在设置 → 采集节奏里改）。

---

## 14. 使用边界与隐私

- 只采集公开、你有权使用的内容；控制频率；不用于骚扰、商用转售。
- Cookie、Bark Key、AutoDL 密码仅保存在本机 `data/settings.json`。
- 视频版权属于 UP 主与平台；本地与网盘副本仅供个人研究与转写。
- Web Cookie **拿不到** UP 主页旁的「IP 属地」（App 接口）；**可以**拿到评论里的 IP 属地（若登录且接口返回）。
- 发论文前请自行对用户 `mid`、昵称做匿名化；本工具默认存原始 ID 便于溯源，导出时再处理。

---

## 附录 A：推荐学习路径（照着做就会）

```
第 1 天：4 安装 → 5 认界面 → 6 登录 → 7 轻量采集 1 个 UID
第 2 天：9 学术滚雪球（只官方字幕试跑）→ 10 看漏斗和 SQL
第 3 天：9 开 Whisper 或 8.3 AutoDL → 10 导出 CSV
按需：8.1 Bark、8.2 Drive
```

---

## 附录 B：开发者自检（可跳过）

```bash
cd ~/biliarchiver
source .venv/bin/activate
python -m unittest discover -s tests -v
```

---

**还有哪一步卡住？** 记下：你停在第几节、屏幕上的**原话报错**、左下角是否已登录。对照第 12 节表，或把现象描述给维护者（**不要**附 Cookie）。
