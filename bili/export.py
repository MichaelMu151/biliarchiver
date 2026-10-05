from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from bili.util import safe_name, ts_iso, write_json, write_text


def account_dir(library: Path, mid: str, name: str) -> Path:
    folder = library / f"{mid}_{safe_name(name or mid)}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def video_dir(acc_dir: Path, pubdate: int, bvid: str, title: str) -> Path:
    folder = acc_dir / "videos" / f"{ts_iso(pubdate)[:10]}_{bvid}_{safe_name(title)}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def dynamic_dir(acc_dir: Path, pub_ts: int, dyn_id: str) -> Path:
    folder = acc_dir / "dynamics" / f"{ts_iso(pub_ts)[:10]}_{dyn_id}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def write_profile(acc_dir: Path, profile: dict[str, Any], snapshot: dict[str, Any]) -> None:
    write_json(acc_dir / "profile.json", profile)
    snaps_path = acc_dir / "snapshots.jsonl"
    with snaps_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    csv_path = acc_dir / "stats.csv"
    new_file = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["captured_at", "mid", "name", "follower", "following", "archive_count", "likes", "views"],
        )
        if new_file:
            writer.writeheader()
        writer.writerow(
            {
                "captured_at": snapshot.get("captured_at"),
                "mid": profile.get("mid"),
                "name": profile.get("name"),
                "follower": snapshot.get("follower"),
                "following": snapshot.get("following"),
                "archive_count": snapshot.get("archive_count"),
                "likes": snapshot.get("likes"),
                "views": snapshot.get("views"),
            }
        )
    official = profile.get("official") or {}
    md = f"""# {profile.get('name') or profile.get('mid')}

- UID：`{profile.get('mid')}`
- 主页：{profile.get('space_url')}
- 性别：{profile.get('sex') or '（未提供）'}
- 学校：{profile.get('school') or '（未填写）'}
- 生日：{profile.get('birthday') or '（未公开）'}
- 签名：{profile.get('sign') or '（无）'}
- 等级：{profile.get('level')}
- 认证：{official.get('title') or official or '无'}
- 采集时间：{snapshot.get('captured_at')}

## 当前快照

| 指标 | 数值 |
| --- | ---: |
| 粉丝 | {snapshot.get('follower')} |
| 关注 | {snapshot.get('following')} |
| 投稿数 | {snapshot.get('archive_count')} |
| 获赞 | {snapshot.get('likes')} |
| 播放 | {snapshot.get('views')} |

粉丝 / 关注是**时点数据**。每次采集会在 `snapshots.jsonl` 和 `stats.csv` 追加一行，便于观察涨粉曲线。不要只保留一个数字去覆盖历史。
"""
    write_text(acc_dir / "profile.md", md)


def write_video_markdown(
    folder: Path,
    meta: dict[str, Any],
    transcript: str = "",
    comment_count: int = 0,
    danmaku_count: int = 0,
) -> Path:
    stat = meta.get("stat") if isinstance(meta.get("stat"), dict) else {}
    owner = meta.get("owner") if isinstance(meta.get("owner"), dict) else {}
    pages = meta.get("pages") or []
    page_lines = "\n".join(
        f"- P{p.get('page')}: {p.get('part')} · cid `{p.get('cid')}` · {p.get('duration')}s"
        for p in pages
        if isinstance(p, dict)
    )
    media_parts = meta.get("media_parts") or []
    part_asset_lines = "\n".join(
        (
            f"- P{part.get('page')} {part.get('title')}："
            f"音频 `{part.get('local_audio') or '未下载'}`；"
            f"视频 `{part.get('local_video') or '未下载'}`；"
            f"转写 {part.get('transcript_status') or '未处理'}"
        )
        for part in media_parts
    )
    md = f"""# {meta.get('title')}

- BV：`{meta.get('bvid')}`
- AV：`{meta.get('aid')}`
- 链接：https://www.bilibili.com/video/{meta.get('bvid')}
- UP：{owner.get('name')} (`{owner.get('mid')}`)
- 发布时间：{ts_iso(meta.get('pubdate'))}
- 分区：{meta.get('tname') or ''}
- 时长：{meta.get('duration')} 秒
- 采集时间：{meta.get('captured_at')}

## 互动数据（采集瞬间快照）

| 播放 | 点赞 | 投币 | 收藏 | 转发 | 评论 | 弹幕 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| {stat.get('view')} | {stat.get('like')} | {stat.get('coin')} | {stat.get('favorite')} | {stat.get('share')} | {stat.get('reply')} | {stat.get('danmaku')} |

## 分P

{page_lines or '单P'}

## 简介

{meta.get('desc') or '（无）'}

## 本地媒体

- 页面链接：https://www.bilibili.com/video/{meta.get('bvid')}
- 媒体状态：{meta.get('media_status') or '未处理'}
- 空间策略：{(meta.get('storage') or {}).get('policy') or 'keep'}
- 说明：{(meta.get('storage') or {}).get('note') or '未处理'}
- 音频缓存：{meta.get('local_audio') or '未保留本地文件'}
- 视频缓存：{meta.get('local_video') or '未保留本地文件'}
- 云盘：{', '.join((meta.get('storage') or {}).get('drive_uris') or []) or '未上传'}
- 云端处理：同目录 `cloud_job.json`（使用页面链接重新解析，或上传本地音频）

{('### 分P媒体与转写' + chr(10) + chr(10) + part_asset_lines) if len(media_parts) > 1 else ''}

## 评论与弹幕

- 评论条数：{comment_count}（`comments.jsonl`）
- 弹幕条数：{danmaku_count}（`danmaku.jsonl`）

## 语音转文字

{transcript.strip() or '（尚未转录。可在任务里开启官方字幕 / 本地 Whisper，或把 `cloud_job.json` 交给云端 GPU。）'}

可复用格式：`transcript.json`（结构化分段）、`transcript.srt`、`transcript.vtt`。
"""
    path = folder / "video.md"
    write_text(path, md)
    return path


def write_dynamic_markdown(
    folder: Path,
    dyn: dict[str, Any],
    ocr_blocks: list[dict[str, str]] | None = None,
    comment_count: int = 0,
) -> Path:
    pics = dyn.get("pictures") or []
    pic_md = "\n".join(f"- {p}" for p in pics) or "（无图）"
    ocr_md = ""
    for block in ocr_blocks or []:
        lines = block.get("lines") or []
        average = (
            sum(float(line.get("confidence") or 0) for line in lines) / len(lines)
            if lines
            else 0
        )
        ocr_md += (
            f"\n### {block.get('file')}\n\n"
            f"> 识别行数：{len(lines)} · 平均置信度：{average:.2f}\n\n"
            f"{block.get('text') or '（未识别到高置信度文字）'}\n"
        )
    kind_label = "专栏" if str(dyn.get("dyn_type") or "") in {"article", "DYNAMIC_TYPE_ARTICLE"} or int(dyn.get("comment_type") or 0) == 12 else "动态"
    md = f"""# {kind_label} {dyn.get('dyn_id')}

- 类型：{dyn.get('dyn_type')}
- 发布时间：{ts_iso(dyn.get('pub_ts'))}
- 链接：{dyn.get('jump_url') or ''}
- 点赞：{dyn.get('like')} · 评论：{dyn.get('comment')} · 转发：{dyn.get('forward')}
- 采集时间：{dyn.get('captured_at')}

## 正文

{dyn.get('text') or '（无文字）'}

## 图片

{pic_md}

## OCR 识别
{ocr_md or chr(10)+'（未启用 OCR 或无图片）'}

结构化坐标与逐行置信度见 `ocr.json`，便于后续校对或导入分析工具。

## 评论

- 条数：{comment_count}（`comments.jsonl`）
"""
    path = folder / "dynamic.md"
    write_text(path, md)
    return path


def write_scout_catalog(
    out_dir: Path,
    *,
    job_id: str,
    accounts: list[dict[str, Any]],
    videos: list[dict[str, Any]],
    dynamics: list[dict[str, Any]],
) -> Path:
    """Write screening CSVs for scout mode (titles/tags/dynamics only)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    acc_path = out_dir / "accounts.csv"
    with acc_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "mid",
                "name",
                "space_url",
                "video_n",
                "dynamic_n",
                "follower",
                "sign",
            ],
        )
        writer.writeheader()
        for row in accounts:
            writer.writerow({key: row.get(key, "") for key in writer.fieldnames})

    vid_path = out_dir / "videos.csv"
    with vid_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "mid",
                "name",
                "bvid",
                "title",
                "tags",
                "tname",
                "pubdate_iso",
                "view",
                "like",
                "reply",
                "duration",
                "page_url",
                "description_short",
            ],
        )
        writer.writeheader()
        for row in videos:
            writer.writerow({key: row.get(key, "") for key in writer.fieldnames})

    dyn_path = out_dir / "dynamics.csv"
    with dyn_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "mid",
                "name",
                "dyn_id",
                "pub_time_iso",
                "text",
                "like",
                "comment",
                "forward",
                "picture_count",
                "jump_url",
            ],
        )
        writer.writeheader()
        for row in dynamics:
            writer.writerow({key: row.get(key, "") for key in writer.fieldnames})

    readme = f"""# 选题预览 · 任务 {job_id}

本目录是「按 UP 主 → 选题预览」的轻量结果，**只含标题 / 标签 / 动态正文**，不含评论、弹幕、音视频。

## 文件

| 文件 | 内容 |
| --- | --- |
| `accounts.csv` | 每个 UP 主一行：昵称、主页、本窗口内视频/动态数 |
| `videos.csv` | 每条投稿：标题、标签、分区、播放/点赞/评论数 |
| `dynamics.csv` | 每条主页动态：正文、互动数、链接 |

## 建议用法

1. 用 Excel / Numbers 打开 `videos.csv`，按标签或标题筛选职场/劳工相关 BV。
2. 对照 `dynamics.csv` 看该 UP 是否经常发相关动态。
3. 筛完后，把保留的 mid / BV 贴回「按 UP 主采集」或「学术滚雪球」做完整归档。

账号 {len(accounts)} · 视频 {len(videos)} · 动态 {len(dynamics)}
"""
    write_text(out_dir / "README.md", readme)
    return out_dir


def write_account_index(acc_dir: Path, profile: dict[str, Any], videos: list[dict[str, Any]], dynamics: list[dict[str, Any]]) -> None:
    v_lines = "\n".join(
        f"- [{item.get('title')}]({Path(item.get('markdown_path') or '').name if False else _rel(acc_dir, item.get('markdown_path'))}) · {ts_iso(item.get('pubdate'))[:10]} · 播放 {item.get('view')}"
        for item in videos[:400]
    )
    d_lines = "\n".join(
        f"- [{item.get('dyn_id')}]({_rel(acc_dir, item.get('markdown_path'))}) · {ts_iso(item.get('pub_ts'))[:10]} · {str(item.get('text') or '')[:40]}"
        for item in dynamics[:400]
    )
    md = f"""# {profile.get('name')} 采集索引

主页：{profile.get('space_url')}

- 账号档案：`profile.md` / `profile.json`
- 粉丝关注时间序列：`stats.csv` / `snapshots.jsonl`
- 视频 {len(videos)} 条，动态 {len(dynamics)} 条

## 视频

{v_lines or '（无）'}

## 动态

{d_lines or '（无）'}
"""
    write_text(acc_dir / "INDEX.md", md)


def _rel(base: Path, target: str | None) -> str:
    if not target:
        return ""
    try:
        return str(Path(target).resolve().relative_to(base.resolve()))
    except Exception:
        return target


def write_keyword_catalog(
    out_dir: Path,
    job_id: str,
    *,
    keywords: list[str],
    videos: list[dict[str, Any]],
    dynamics: list[dict[str, Any]] | None = None,
) -> Path:
    """Export keyword-discovery results for screening: BV+title list, optional dynamics."""
    out_dir.mkdir(parents=True, exist_ok=True)
    video_fields = [
        "bvid",
        "title",
        "author_name",
        "mid",
        "pubdate_iso",
        "duration",
        "views",
        "likes",
        "replies",
        "danmaku",
        "tname",
        "keyword",
        "page_url",
        "description_short",
    ]
    vid_path = out_dir / "videos.csv"
    with vid_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=video_fields)
        writer.writeheader()
        for row in videos:
            writer.writerow({key: row.get(key, "") for key in video_fields})

    bvid_path = out_dir / "bvids.txt"
    write_text(bvid_path, "\n".join(str(row.get("bvid") or "") for row in videos if row.get("bvid")) + ("\n" if videos else ""))

    md_lines = [
        f"# 关键词采样列表 · 任务 {job_id}",
        "",
        f"关键词：{' / '.join(keywords) if keywords else '（无）'}",
        "",
        "本目录**只导出候选列表**，不自动转写。把 `bvids.txt` 贴进「按视频号采集」即可补评论、弹幕、Whisper。",
        "",
        f"视频候选 **{len(videos)}** 条。",
        "",
        "| BV | 标题 | UP | 播放 | 点赞 | 评论 | 日期 |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for row in videos:
        title = str(row.get("title") or "").replace("|", "\\|")
        author = str(row.get("author_name") or "").replace("|", "\\|")
        bvid = str(row.get("bvid") or "")
        url = row.get("page_url") or (f"https://www.bilibili.com/video/{bvid}" if bvid else "")
        md_lines.append(
            f"| [{bvid}]({url}) | {title} | {author} | {row.get('views') or 0} | "
            f"{row.get('likes') or 0} | {row.get('replies') or 0} | {row.get('pubdate_iso') or ''} |"
        )
    write_text(out_dir / "videos.md", "\n".join(md_lines) + "\n")

    dyn_rows = list(dynamics or [])
    if dyn_rows:
        dyn_fields = [
            "dyn_id",
            "mid",
            "author_name",
            "pub_time_iso",
            "text",
            "like",
            "comment",
            "forward",
            "picture_count",
            "keyword",
            "jump_url",
            "kind",
        ]
        with (out_dir / "dynamics.csv").open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=dyn_fields)
            writer.writeheader()
            for row in dyn_rows:
                writer.writerow({key: row.get(key, "") for key in dyn_fields})

    readme = f"""# 关键词采样 · 任务 {job_id}

关键词：{' / '.join(keywords) if keywords else '（无）'}

| 文件 | 内容 |
| --- | --- |
| `videos.csv` | 过视频门禁的候选：BV、标题、UP、互动数 |
| `videos.md` | 同一份，方便在编辑器里筛选 |
| `bvids.txt` | 只有 BV 号，可直接贴进「按视频号采集」 |
| `dynamics.csv` | 过门槛并已采集的图文：动态 + 专栏（若勾选了爬图文） |

本任务**不自动开转写**。筛完标题后，用 `bvids.txt` 开「按视频号采集」。
"""
    write_text(out_dir / "README.md", readme)
    write_json(out_dir / "videos.json", videos)
    return out_dir
