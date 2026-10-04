"""Shared data-hygiene helpers for analysis (soft flags, noise, offtopic).

Does not delete rows. Analysis scripts should prefer ``v_comment_analysis`` /
``v_video_analysis`` (or filter ``analysis_exclude`` / ``is_noise``).
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from typing import Any, Iterable

# Keep in sync with run_pilot_analysis.py lexicon for reproducible offtopic scores.
WORK_TERMS = [
    "加班", "打工", "牛马", "裁员", "薪资", "工资", "老板", "996", "007",
    "劳动法", "大厂", "失业", "找工作", "上班", "下班", "打工人", "社畜",
    "内卷", "躺平", "离职", "辞退", "加班费", "试用期", "外包", "派遣",
    "职场", "面试", "简历", "hr", "绩效", "kpi", "晋升", "升职",
    "压榨", "剥削", "资本家", "资方", "劳资", "公积金", "五险一金",
    "35岁", "中年危机", "铁饭碗", "体制内", "考公", "编制", "流水线",
    "工厂", "流水账", "班味", "耗材", "红利",
]

REPRO_TERMS = [
    "结婚", "相亲", "彩礼", "生娃", "生孩子", "三胎", "二胎", "软肋",
    "后代", "最后一代", "不婚", "不育", "婚姻", "离婚", "恋爱", "男女",
    "女朋友", "男朋友", "对象", "领证", "彩礼钱", "嫁妆", "生育", "怀孕",
    "带娃", "全职妈妈", "全职奶爸", "催婚", "催生", "丁克", "绝育",
    "传宗接代", "断后", "相亲角", "丈母娘", "婆婆", "婆媳", "彩礼焦虑",
    "伪娘", "婚恋", "适龄", "剩女", "剩男",
]

CROSS_PHRASES = [
    "牛马生耗材", "生个软肋", "软肋被拿捏", "给资本家生", "给老板生",
    "生孩子当耗材", "生牛马", "繁衍耗材", "给社会生", "给国家生",
    "结婚等于", "彩礼等于", "婚育成本", "养不起", "生不起", "结不起",
    "结婚打工", "结婚加班", "彩礼打工", "婚房", "首付",
]

EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)
AT_RE = re.compile(r"^(@\S+\s*)+$")
SYMBOL_RE = re.compile(r"^[\W_\d]+$", flags=re.UNICODE)
WS_RE = re.compile(r"\s+")

ANALYSIS_VIEWS_SQL = """
DROP VIEW IF EXISTS v_video_analysis;
CREATE VIEW v_video_analysis AS
SELECT v.*
FROM videos v
WHERE IFNULL(v.pass_filter, 0) = 1
  AND IFNULL(v.analysis_exclude, 0) = 0;

DROP VIEW IF EXISTS v_comment_analysis;
CREATE VIEW v_comment_analysis AS
SELECT
  c.rpid, c.target_kind, c.target_id, c.hierarchy_level,
  c.root_rpid, c.parent_rpid, c.bvid,
  c.user_mid, c.user_name, c.ip_location,
  c.content, c.text_length, c.like_count, c.reply_count,
  c.pub_ts, c.pub_time_iso,
  c.is_noise, c.noise_reason,
  v.title AS video_title, v.tname AS video_category, v.tid AS video_tid,
  v.view_count AS video_view, v.reply_count AS video_reply,
  v.depth AS snowball_depth, v.seed_bvid, v.run_id AS video_run_id,
  v.analysis_exclude, v.analysis_exclude_reason
FROM comments c
JOIN videos v ON v.bvid = c.bvid
WHERE IFNULL(v.pass_filter, 0) = 1
  AND IFNULL(v.analysis_exclude, 0) = 0
  AND IFNULL(c.is_noise, 0) = 0
  AND c.content IS NOT NULL
  AND TRIM(c.content) != '';
"""


def normalize_text(text: str) -> str:
    return WS_RE.sub(" ", (text or "").strip())


def classify_topic(text: str) -> str:
    t = (text or "").lower()
    work_hit = any(term.lower() in t for term in WORK_TERMS)
    repro_hit = any(term.lower() in t for term in REPRO_TERMS)
    cross_phrase = any(phrase.lower() in t for phrase in CROSS_PHRASES)
    if cross_phrase or (work_hit and repro_hit):
        return "work_repro"
    if work_hit:
        return "work_only"
    if repro_hit:
        return "repro_only"
    return "other"


def is_noise(text: str) -> tuple[bool, str]:
    """Return (is_noise, reason) for empty / emoji-only / symbol-only / @-only / too short."""
    raw = text or ""
    cleaned = normalize_text(raw)
    if not cleaned:
        return True, "empty"
    no_emoji = EMOJI_RE.sub("", cleaned).strip()
    if not no_emoji:
        return True, "emoji_only"
    if AT_RE.match(cleaned):
        return True, "at_only"
    if SYMBOL_RE.match(no_emoji):
        return True, "symbol_only"
    if len(no_emoji) <= 1:
        return True, "too_short"
    return False, ""


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_analysis_columns(conn: sqlite3.Connection) -> list[str]:
    """Add soft-flag columns + analysis views. Idempotent. Returns applied ALTERs."""
    applied: list[str] = []
    video_cols = _table_columns(conn, "videos")
    if "analysis_exclude" not in video_cols:
        conn.execute("ALTER TABLE videos ADD COLUMN analysis_exclude INTEGER DEFAULT 0")
        applied.append("videos.analysis_exclude")
    if "analysis_exclude_reason" not in video_cols:
        conn.execute("ALTER TABLE videos ADD COLUMN analysis_exclude_reason TEXT")
        applied.append("videos.analysis_exclude_reason")

    comment_cols = _table_columns(conn, "comments")
    if "is_noise" not in comment_cols:
        conn.execute("ALTER TABLE comments ADD COLUMN is_noise INTEGER DEFAULT 0")
        applied.append("comments.is_noise")
    if "noise_reason" not in comment_cols:
        conn.execute("ALTER TABLE comments ADD COLUMN noise_reason TEXT")
        applied.append("comments.noise_reason")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_videos_analysis_exclude ON videos(analysis_exclude)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_comments_is_noise ON comments(is_noise)"
    )
    conn.executescript(ANALYSIS_VIEWS_SQL)
    return applied


def find_offtopic_videos(
    conn: sqlite3.Connection,
    *,
    other_pct_min: float = 98.0,
    min_comments: int = 50,
    only_passed: bool = True,
) -> list[dict[str, Any]]:
    """Videos whose comment bag is almost entirely lexicon-「other」."""
    sql = """
        SELECT c.bvid, v.title, v.depth, c.content
        FROM comments c
        JOIN videos v ON v.bvid = c.bvid
        WHERE c.content IS NOT NULL AND TRIM(c.content) != ''
    """
    if only_passed:
        sql += " AND IFNULL(v.pass_filter, 0) = 1"
    rows = conn.execute(sql).fetchall()

    by_video: dict[str, Counter] = defaultdict(Counter)
    titles: dict[str, str] = {}
    depths: dict[str, int] = {}
    for row in rows:
        bvid = row[0]
        titles[bvid] = row[1] or ""
        depths[bvid] = int(row[2] or 0)
        by_video[bvid][classify_topic(row[3] or "")] += 1

    out: list[dict[str, Any]] = []
    for bvid, ctr in by_video.items():
        total = sum(ctr.values())
        if total < min_comments:
            continue
        other_pct = 100.0 * ctr["other"] / total
        if other_pct >= other_pct_min:
            out.append({
                "bvid": bvid,
                "title": titles.get(bvid, "")[:80],
                "depth": depths.get(bvid, 0),
                "comments": total,
                "other_pct": round(other_pct, 2),
                "topic_counts": dict(ctr),
            })
    out.sort(key=lambda x: (-x["other_pct"], -x["comments"]))
    return out


def mark_offtopic_videos(
    conn: sqlite3.Connection,
    videos: Iterable[dict[str, Any]],
    *,
    reason_prefix: str = "offtopic_comment_bag",
    dry_run: bool = False,
) -> int:
    """Soft-exclude videos from analysis. Returns number of rows that would be / were updated."""
    n = 0
    for item in videos:
        bvid = item["bvid"]
        reason = (
            f"{reason_prefix}:other_pct={item['other_pct']}"
            f":n={item['comments']}"
        )
        if dry_run:
            n += 1
            continue
        conn.execute(
            """
            UPDATE videos
            SET analysis_exclude = 1,
                analysis_exclude_reason = ?
            WHERE bvid = ?
            """,
            (reason, bvid),
        )
        n += 1
    return n


def clear_analysis_excludes(conn: sqlite3.Connection, reason_prefix: str | None = None) -> int:
    """Reset analysis_exclude flags (optionally only those matching reason prefix)."""
    if reason_prefix:
        cur = conn.execute(
            """
            UPDATE videos
            SET analysis_exclude = 0, analysis_exclude_reason = NULL
            WHERE IFNULL(analysis_exclude, 0) = 1
              AND IFNULL(analysis_exclude_reason, '') LIKE ?
            """,
            (f"{reason_prefix}%",),
        )
    else:
        cur = conn.execute(
            """
            UPDATE videos
            SET analysis_exclude = 0, analysis_exclude_reason = NULL
            WHERE IFNULL(analysis_exclude, 0) = 1
            """
        )
    return int(cur.rowcount or 0)


def mark_noise_comments(
    conn: sqlite3.Connection,
    *,
    only_passed: bool = True,
    dry_run: bool = False,
    batch_size: int = 5000,
) -> dict[str, int]:
    """Scan comments and soft-flag noise. Returns reason -> count (and total)."""
    sql = "SELECT c.rpid, c.content FROM comments c"
    if only_passed:
        sql += " JOIN videos v ON v.bvid = c.bvid WHERE IFNULL(v.pass_filter, 0) = 1"
    rows = conn.execute(sql).fetchall()

    counts: Counter[str] = Counter()
    updates: list[tuple[str, str]] = []
    for rpid, content in rows:
        noisy, reason = is_noise(content or "")
        if not noisy:
            continue
        counts[reason] += 1
        updates.append((reason, rpid))

    if not dry_run and updates:
        # Clear previous noise flags on the scanned set first for idempotency.
        if only_passed:
            conn.execute(
                """
                UPDATE comments
                SET is_noise = 0, noise_reason = NULL
                WHERE bvid IN (SELECT bvid FROM videos WHERE IFNULL(pass_filter, 0) = 1)
                """
            )
        else:
            conn.execute("UPDATE comments SET is_noise = 0, noise_reason = NULL")

        for i in range(0, len(updates), batch_size):
            chunk = updates[i : i + batch_size]
            conn.executemany(
                "UPDATE comments SET is_noise = 1, noise_reason = ? WHERE rpid = ?",
                chunk,
            )
    counts["total"] = sum(v for k, v in counts.items() if k != "total")
    return dict(counts)
