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
10. [关键词采样：导出 BV 列表 + 全站图文](#10-关键词采样导出-bv-列表--全站图文动态与专栏)
11. [分析数据集：怎么查、怎么导出](#11-分析数据集怎么查怎么导出)
12. [文件都在哪、打开后看什么](#12-文件都在哪打开后看什么)
13. [出错了怎么办（对照表）](#13-出错了怎么办对照表)
14. [Windows + NVIDIA 用户](#14-windows--nvidia-用户)
15. [使用边界与隐私](#15-使用边界与隐私)

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

你可以把它理解成四条采集，外加同一套「从头开始 / 续跑」：

| 页面 | 你什么时候用 | 不会做什么 |
| --- | --- | --- |
| **按 UP 主采集** | 已知几个 UP，要把一段时间里的投稿、动态、评论整库归档 | 不会沿相关推荐扩散 |
| **学术滚雪球** | 从几条像研究对象的视频出发，沿相关推荐扩，并用门禁筛掉游戏鬼畜等噪声 | 不会把某个 UP 的全部投稿都搬下来 |
| **关键词采样** | 同一组关键词全站搜**视频列表** + 搜索「**图文**」（动态和专栏）；导出带标题的 BV 列表，**不自动转写** | 不沿推荐扩散；视频转写请到「按视频号采集」 |
| **按视频号采集** | 把 `bvids.txt` 或任意 BV 列表贴进来：评论、弹幕、Whisper、画面 OCR、Google Drive 上传 | 不扩散、不过门禁 |

四条**共存**。学术滚雪球里通过门禁的视频，会同时采 **视频台词** 和 **评论**。关键词采样的视频**只导出列表**，不在这一步转写；图文（动态 + 专栏）会采正文、图片和评论。评论和视频话语是两层分析单位，不要混成一张表做主题模型。

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
| **SQLite / corpus.db** | 工作库：续爬队列、门禁过程、旧版字段。用 DB Browser、pandas、R 等打开。 |
| **research.db** | 合并后的分析库（`data/merge/research.db`）：视频 / 评论 / 弹幕 / 转写 / 发现日志。新爬取的结构化结果会写进这里；论文分析优先用它。 |
| **rclone** | 命令行工具，把本机文件夹同步到 Google Drive 等网盘。 |
| **AutoDL** | 租 GPU 的云平台。接入时先选**通道**：**采集转写**只同步约 13 个工作机文件并预热 Whisper；**主题分析**只同步 2 个脚本跑 BGE/BERTopic，不加载语音模型。爬虫仍在你电脑。 |
| **Bark** | iPhone 上的一个推送 App。任务跑很久时，手机会收到「又完成一条视频」之类的提醒。 |
| **127.0.0.1:8765** | 本机网页地址。只有你自己这台电脑能打开，不是公网网站。 |
| **门禁** | 学术滚雪球 / 关键词采样里的筛选规则。滚雪球要同时满足分区、播放量、互动、标签才会完整采集。关键词采样的视频用标题约束和播放/互动门槛决定是否进 BV 列表；图文用单独的点赞/评论/转发门槛。 |
| **滚雪球** | 从种子视频出发，看 B 站「相关推荐」，一层层扩散，像滚雪球一样扩大样本。 |
| **关键词采样** | 同一组关键词分别走全站**视频搜索**与搜索页 **「图文」**（动态/专栏）；视频过门禁后只导出列表（含标题），图文过单独互动门槛后照单采集；不自动开 Whisper。 |
| **bvids.txt** | 关键词任务导出目录里的纯 BV 列表，可直接贴进「按视频号采集」。 |

---

## 3. 你要准备什么

### 3.1 硬件与系统

- **Mac**：macOS，建议 8 GB 内存以上；硬盘请留 **至少 10 GB** 空闲（若下载视频则更多）。
- **Windows + NVIDIA**：见 [第 14 节](#14-windows--nvidia-用户) 和 [WINDOWS.md](WINDOWS.md)。
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
| 工作库（续爬 / 门禁） | `data/corpus.db` | **不会** |
| 合并分析库 | `data/merge/research.db` | **不会** |
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

**成功：** 看到左侧菜单分成两组。「四种采集」是 **按 UP 主采集**、**学术滚雪球**、**关键词采样**、**按视频号采集**；下面是使用指南、运行监控、采集结果、分析数据集、设置与登录。

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

### 5.4 关键词采样

同一组 **搜索关键词** 分别搜视频和搜索页「图文」。视频侧：标题约束 + 发布时间窗 + 播放/互动门禁 → 导出 **BV + 标题**（`videos.csv` / `videos.md` / `bvids.txt`），**不自动转写**。图文侧同时收 **动态** 和 **专栏**：动态走 Opus 详情，专栏走 `cv` 正文和评论区（评论类型 12）。与视频**共用时间窗**，但用**单独的**点赞/评论/转发门槛；门槛全 0 即时间窗内照单全收。两个勾选可 **只出列表**、**只爬图文**，或两项都做。结果在 `data/exports/keyword_任务号/`。若上次只写入了搜索摘要（库里 `dyn_type=search`），续跑会补采全文。

### 5.5 按视频号采集

处理你粘贴的 **BV 列表**（或关键词导出目录里的 `bvids.txt`）。不扩推荐、不过门禁。可选：评论树、弹幕、官方字幕 + Whisper、无声画面 OCR、下载音频/视频、**上传 Google Drive 后删本地**。云端 Whisper **优先 GPU 直拉 B 站音频**；SFTP 仅作备选。勾选「跳过已有口播转写」时，从头开始会放过已有 Whisper / 官方字幕的 BV。

### 5.6 运行监控

任务开始后自动跳转到这里。上方是各采集模式的最近任务，可直接续跑；下面是当前进度、日志和取消。

### 5.7 采集结果

浏览已采账号、点进某个视频的 Markdown。给**人读**用。

### 5.8 分析数据集

看 `corpus.db` / `research.db` 里有多少视频/评论、门禁漏斗、执行 SQL、导出 CSV。给**统计软件**用。结构化分析优先打开 `data/merge/research.db`。

### 5.9 设置与登录

扫码或 Cookie 登录；Bark、Google Drive、AutoDL（先选通道再接入）；Whisper 模型、请求间隔等。**多数任务开始前至少要完成登录。**

### 5.10 左下角两个小提示

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

**干什么：** 同一台 AutoDL 可以干两件完全不同的事。接入前必须先选**通道**；界面会展开该通道的上传清单、会预热的模型、以及明确跳过的内容。

| 通道 | 界面选项 | 同步什么 | 会不会加载 Whisper | 覆盖哪些任务 |
| --- | --- | --- | --- | --- |
| **采集转写** | 选「采集转写通道」 | **约 13 个**工作机文件（`gpu_worker.py` + Whisper/OCR 依赖），**不**上传爬虫 UI / `corpus.db` / 分析脚本 | 会，预热 `large-v3`（约 3GB，只需一次） | 按 UP 主 · 滚雪球 · 按视频号（Whisper）；关键词动态 OCR 若选云端也需此通道 |
| **主题分析** | 选「主题分析通道」 | **2 个**文件：`run_gpu_full_bertopic.py` + `requirements-analysis-gpu.txt` | **不会**；若已有转写工作机会先停掉腾显存 | 分析数据集 · GPU 全量 BERTopic |

爬虫、评论、门禁、搜索队列**仍在你的 Mac**。采集通道云端只算 Whisper / OCR；分析通道云端只算向量和聚类。换通道等于换用途——不要用采集通道去跑 BERTopic，也不要用分析通道去转写。

**步骤：**

1. AutoDL 控制台开机实例（如 3090 / 4090），等「运行中」。
   - 镜像选 **PyTorch 官方镜像 + Python 3.10～3.12**。**不要**选空白 Ubuntu。
   - 控制台「驱动/CUDA」写的是**这张卡所在物理机的驱动上限**，不是镜像版本。换一张卡往往会变：
     | 驱动旁边写的 CUDA | 镜像应选 |
     | --- | --- |
     | 12.8（驱动 570.x） | PyTorch **CUDA 12.x**（12.1 / 12.4 / 12.8），不要选 13.0 |
     | 13.2（驱动 595.x） | CUDA **12 或 13** 都可以 |
   - Whisper 的 pip 包仍要 `libcublas.so.12`，**采集转写**接入会自动补装，和上面选 12 还是 13 **无关**。主题分析不装 Whisper，也不补这套库。
2. 在**该实例页**复制：
   - **SSH 登录指令**（整行，含 `-p 端口`），例：  
     `ssh -p 38491 root@connect.bjb1.seetacloud.com`
   - **登录密码**（实例页上的，**不是** AutoDL 网站登录密码）
3. 界面 **AutoDL 云端 GPU** 先点选 **采集转写通道** 或 **主题分析通道**，核对下方深色说明卡里的上传/模型清单，再粘贴上述两项。
4. 点 **接入采集转写通道** / **接入主题分析通道**，看下方日志滚动。**保持此浏览器标签页开着**。
5. 采集转写就绪后，在 **按 UP 主 / 滚雪球 / 按视频号** 把算力选成 **云端 GPU**。Whisper 在 GPU 上**直拉 B 站音频**再转写；只有直拉失败且本机已有音频文件时，才走 **SFTP 备选**（经已有 SSH 传到卡上，不在 Mac 隧道 HTTP 上转写）。**Google Drive 上传**仍通过 rclone，在任务里选「上传云盘后删除」即可。主题分析就绪后运行 `tools/launch_bertopic_autodl.py`（见 11.4），不要再走 Whisper 工作机。

**不要**自己在终端 ssh 上去改项目路径；界面已封装。

### 8.4 采集节奏（第一次别改）

- 默认请求间隔约 **1.0～2.1 秒**；全站搜索翻页额外约 1～2 秒（越深略加长）。中等限速，兼顾稳定与速度。
- **不要**调到 0.2 秒以下。
- 若 `data/settings.json` 里仍写着更短的间隔，以文件为准；想更稳可在设置里略调大。
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

## 10. 关键词采样：导出 BV 列表 + 全站图文（动态与专栏）

**什么时候用：** 某议题正在全站发酵（如「8 小时 / 双休 / 调休」），要用**同一组关键词**同时抓**视频候选**和搜索页 **「图文」**（动态 + 专栏），先按标题筛 BV，再决定是否开 Whisper。

**和学术滚雪球 / 按视频号的差别：**

| | 学术滚雪球 | 关键词采样 | 按视频号采集 |
| --- | --- | --- | --- |
| 起点 | 种子 BV / UP | 搜索关键词 | 你贴的 BV 列表 |
| 扩散 | 相关推荐 | **不**扩散 | **不**扩散 |
| 视频 | 过门禁后**直接采**转写+评论 | 过门禁后**只导出列表**（含标题） | 不过门禁，**采**转写+评论+弹幕 |
| 图文 | UP 空间时间线里的动态（按 UP 归档时） | **全站「图文」搜索**：动态走 Opus，专栏走 `cv` 正文；单独互动门槛 | 不采 |
| 转写 | 本页可开 Whisper | **不自动开**；用 `bvids.txt` 去按视频号 | 本页开 Whisper / OCR |

**前提：** 已登录（第 6 节）。图文若勾选「云端 OCR」，需先完成 8.3 **采集转写通道**；只导出视频列表则不必接 GPU。

图文搜索偶发「验票 / 风控 0 OK」是 B 站限速，程序会自己退避重试。连续出现就暂停，过一会儿再续跑，并把设置里的请求间隔调到大约 2.5–5 秒。

### 10.1 推荐工作流（两步走）

```
① 关键词采样 → 导出 data/exports/keyword_<任务号>/
   ├── videos.md / videos.csv   # 带标题，方便人工筛
   ├── bvids.txt                # 纯 BV，一行一个
   └── dynamics.csv             # 若勾选了采集图文（动态 + 专栏）

② 打开 videos.md 筛标题 → 把 bvids.txt（或删改后的 BV）贴进「按视频号采集」
   → 开评论 / 弹幕 / Whisper / Google Drive 上传
```

### 10.2 填写要点

1. 左侧点 **关键词采样**。左下角应能看到界面版本号（例如 `界面 20261006a`）。看不到就强刷，或清掉 `127.0.0.1` 的网站数据。
2. **搜索关键词**：一行一个或逗号分隔，例如 `八小时`、`8小时`、`双休`（视频与图文**共用**这一组词）。
3. **标题约束（视频侧，推荐）**：例如 `8小时,八小时,双休,单休`——标题至少命中其一才进列表。图文不套这组标题词。
4. **发布时间**：填开始日期；结束可留空表示「到现在」。**图文与视频共用**此时间窗。
5. **视频门禁**：播放 / 点赞 / 评论 / 互动比、分区黑白名单（步骤 2）。
6. **步骤 3 · 本轮做什么**（两个勾选）：
   - ✅ **导出视频列表**（BV + 标题，不转写）
   - ✅ **采集匹配动态与专栏**（走搜索「图文」）
   - 可只勾一项：只出列表、或只爬图文。
7. **步骤 4 · 图文门槛**（与视频分开）：最低点赞 / 评论 / 转发；**全 0 = 时间窗内照单全收**。可采评论、图片 OCR（本机 RapidOCR 或云端 GPU）。动态评论类型是 17，专栏是 12，程序会按链接自动区分。
8. **列表上限**：约束导出的**视频**条数；图文另有安全上限，避免搜爆。

### 10.3 导出文件在哪

任务结束后，路径形如：

```
data/exports/keyword_<12位任务号>/
├── README.md
├── videos.csv / videos.md / videos.json
├── bvids.txt
└── dynamics.csv          # 仅当勾选了采集图文；kind 列区分动态 / 专栏
```

每条图文还会写到账号目录下的 `dynamic.md`（正文）、`comments.jsonl`、图片 OCR。运行监控日志里会打印导出目录。`videos.md` 最适合筛选题；`bvids.txt` 可直接粘贴到 **按视频号采集**。

### 10.4 开始、续跑与检查

1. 改过代码后先停掉旧的 `python run.py`，再重新启动，然后打开 http://127.0.0.1:8765/ 。
2. 点 **从头开始** 或 **续跑选中任务**。日志会出现「搜索视频」「搜索图文/动态」。专栏成功时是「专栏完成」，动态成功时是「动态完成」。
3. **不会**自动下载视频或跑 Whisper。若需要台词，必须做 10.1 第 ② 步。
4. 续跑会复用已导出的 `videos.json`。图文从断点继续；若某条之前只有搜索摘要（`dyn_type=search`），续跑会补采全文和评论。
5. 分析数据集里可能出现 `discovery=keyword` 的候选视频（有标题、无转写），用于统计；正式转写仍以按视频号任务为准。

---

## 11. 分析数据集：怎么查、怎么导出

### 11.1 打开页面

左侧 **分析数据集** 默认连工作库：

```
data/corpus.db
```

合并后的研究库（实体表 + 发现日志，新爬取也会写入）在：

```
data/merge/research.db
```

| 库 | 用途 |
| --- | --- |
| `data/app.db` | 界面任务进度 |
| `data/corpus.db` | 续爬队列、门禁过程、旧拓扑字段 |
| `data/merge/research.db` | **论文分析优先**：视频 / 评论 / 弹幕 / 转写 / 批次发现日志 |

界面「分析数据集」页目前仍打开 `corpus.db`。查 `research.db` 时用 DB Browser、pandas，或：

```bash
sqlite3 data/merge/research.db
```

视频评论用 `comments.bvid`（且 `target_kind='video'`）；动态评论用 `target_kind='dynamic'` 且 `target_id = dyn_id`。按某次任务筛样本时，先从 `video_discovery_log` 按 `batch_id` 取出 `bvid`，再连评论或转写，避免同一视频被多种方法发现时把评论算重。

### 11.2 复制粘贴这些 SQL（点「执行」）

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

### 11.3 导出 CSV

点 **导出 CSV**，到 Finder 打开：

```
data/exports/
```

用 Excel、R、Stata、Python pandas 打开均可。

### 11.4 数据治理与全量 GPU BERTopic

预分析之后先软标记清洗，再用 **AutoDL 云端 GPU** 跑全量语义主题（约 51.5 万条 `v_comment_analysis`）。本机 Intel Mac **不要**硬跑全量 BGE；本地 `run_bertopic_topics.py` 仅作抽样预览。

```bash
# 1) 标记脱靶视频 + 噪音评论（软标志，不删行）
.venv/bin/python tools/corpus_hygiene.py --other-pct-min 98

# 2) 本机导出精简评论包（仅文本+元数据，约数十 MB；不是整库/媒体）
.venv/bin/python run_gpu_full_bertopic.py --export-only

# 3) UI「设置与登录」→ AutoDL 选「主题分析」→ 粘贴 SSH + 密码 → 接入分析 GPU
#    （不要选「采集转写」，否则会预热 Whisper large-v3、占用显存）

# 4) 投递到 GPU：上传 JSONL、安装分析依赖、跑 BGE + BERTopic、拉回报告
.venv/bin/python tools/launch_bertopic_autodl.py
```

**为什么不能「只登录 UI 就让本机直接用云端显存」：**  
「主题分析」接入只同步 BERTopic 脚本并保持 SSH；GPU **读不到** 本机 `corpus.db`。还要把精简 JSONL **上传一次**到 `/root/autodl-tmp/biliarchiver/`，在卡上算完再把 `TOPIC_EVOLUTION_FINDINGS.md` 与 metrics 拉回。评论正文合计约 24MB 字符，远小于整库或媒体库。不要用「采集转写」接入来跑 BERTopic：那条路径会同步 Whisper 工作机并预热 `large-v3`。

| 视图 / 字段 | 含义 |
| --- | --- |
| `v_video_analysis` | `pass_filter=1` 且未 `analysis_exclude` |
| `v_comment_analysis` | 上表视频下、非 `is_noise` 的评论 |
| `videos.analysis_exclude` | 高度脱靶视频软剔除 |
| `comments.is_noise` | 空文本 / 纯 emoji / 纯符号 / 纯 @ |

产出：`data/exports/hygiene_report.json`、`gpu_full_bertopic_metrics.json`、`TOPIC_EVOLUTION_FINDINGS.md`、嵌入缓存 `embeddings_bge_full.npy`（在 GPU 盘上）。

### 11.5 回填旧档案

若你**先**用「按 UP 主采集」采过一堆，**后**才要用 SQLite：点 **回填现有档案**，程序会扫描 `data/library/` 灌进 `corpus.db`（可能要几分钟）。

---

## 12. 文件都在哪、打开后看什么

### 12.1 给人读的（Markdown）

```
data/library/<UID>_<UP主名>/
├── INDEX.md
├── profile.md
├── videos/ ...
└── dynamics/ ...

data/exports/keyword_<任务号>/     # 关键词采样导出（筛选用）
├── videos.md / videos.csv         # BV + 标题 + 互动数
├── bvids.txt                      # 纯 BV 列表
└── dynamics.csv                   # 全站图文：动态 + 专栏（若已采集）
```

用 **Typora、Obsidian、VS Code、记事本** 都能打开 `.md`。`data/library/` 下仍是按 UP 归档的完整 Markdown；关键词**列表**优先看 `data/exports/keyword_*/`。

### 12.2 给程序读的（SQLite）

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

### 12.3 永久链接 vs 播放直链

成果里应保存：

```
https://www.bilibili.com/video/BVxxxx
```

不要保存会过期的 CDN 播放 URL。

---

## 13. 出错了怎么办（对照表）

| 你看到的 | 可能原因 | 怎么办 |
| --- | --- | --- |
| `ERR_CONNECTION_REFUSED` | 程序没启动 | 终端 `cd` 到项目 → `source .venv/bin/activate` → `python run.py` |
| `ModuleNotFoundError` | 没激活虚拟环境 | `source .venv/bin/activate` 后重试 |
| `command not found: python3.13` | 没装 Python 3.13 | 见 4.1 |
| 左下角一直未登录 | Cookie 无效或未保存 | 重新扫码或粘贴 Cookie |
| 评论很少 / 无 IP | 未登录 | 第 6 节 |
| 日志里 `-412` / `412` | 请求太快或被风控 | 设置里间隔调大（默认约 1～2 秒）；等 30 分钟再续跑 |
| SFTP 转写失败 / 隧道 HTTP 失败 | 本机到 GPU 隧道端口过期 | 正常：程序会改走 **GPU 直拉**；仍失败则检查 AutoDL 是否仍「已接入」 |
| 关键词任务没有转写 | 设计如此 | 用 `bvids.txt` 到 **按视频号采集** 开 Whisper |
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

## 14. Windows + NVIDIA 用户

1. 克隆 https://github.com/MichaelMu151/biliarchiver-windows
2. 按 [WINDOWS.md](WINDOWS.md) 运行 `tools\setup_windows.ps1`
3. `python run.py` 打开 http://127.0.0.1:8765
4. **从本 README 第 5 节「认识界面」开始**，与 Mac 完全相同

4060 上 Whisper 建议：`medium` + `float16`（在设置 → 采集节奏里改）。

---

## 15. 使用边界与隐私

- 只采集公开、你有权使用的内容；控制频率；不用于骚扰、商用转售。
- Cookie、Bark Key、AutoDL 密码仅保存在本机 `data/settings.json`。
- 视频版权属于 UP 主与平台；本地与网盘副本仅供个人研究与转写。
- Web Cookie **拿不到** UP 主页旁的「IP 属地」（App 接口）；**可以**拿到评论里的 IP 属地（若登录且接口返回）。
- 发论文前请自行对用户 `mid`、昵称做匿名化；本工具默认存原始 ID 便于溯源，导出时再处理。

---

## 附录 A：推荐学习路径（照着做就会）

```
第 1 天：4 安装 → 5 认界面 → 6 登录 → 7 轻量采集 1 个 UID
第 2 天：9 学术滚雪球（只官方字幕试跑）→ 11 看漏斗和 SQL
第 3 天：10 关键词采样导出列表 + 图文（动态/专栏） → 用 bvids.txt 按视频号开 Whisper
按需：8.1 Bark、8.2 Drive；8.3 AutoDL 采集转写；全量主题用 8.3 主题分析 + 11.4
```

---

## 附录 B：开发者自检（可跳过）

```bash
cd ~/biliarchiver
source .venv/bin/activate
python -m unittest discover -s tests -v
```

---

**还有哪一步卡住？** 记下：你停在第几节、屏幕上的**原话报错**、左下角是否已登录。对照第 13 节表，或把现象描述给维护者（**不要**附 Cookie）。
