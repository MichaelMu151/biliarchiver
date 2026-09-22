"""Analysis-grade relational corpus.

The operational database (``data/app.db``) tracks UI and job state. This module owns
a second, strictly relational dataset (``data/corpus.db``) that is meant to be opened
directly by R / pandas / Stata: one row per observation, explicit foreign keys,
both unix and ISO timestamps, and provenance on every row.

Everything here is idempotent. Re-running a job, resuming it, or re-ingesting the
same JSONL sidecars updates rows in place instead of duplicating observations.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from bili.paths import CORPUS_DB_PATH, ensure_dirs
from bili.util import now_iso, ts_iso

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

-- Provenance: every observation points back at the run that produced it.
CREATE TABLE IF NOT EXISTS collection_runs (
  run_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,              -- 'archive' (UP 主归档) | 'academic' (滚雪球)
  label TEXT,
  config_json TEXT,
  started_at TEXT,
  finished_at TEXT,
  status TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS authors (
  mid TEXT PRIMARY KEY,
  name TEXT,
  sex TEXT,
  sign TEXT,
  level INTEGER,
  school TEXT,
  official_role INTEGER,
  official_title TEXT,
  birthday TEXT,
  face TEXT,
  space_url TEXT,
  first_seen TEXT,
  last_seen TEXT
);

CREATE TABLE IF NOT EXISTS author_snapshots (
  snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
  mid TEXT NOT NULL REFERENCES authors(mid),
  captured_at TEXT NOT NULL,
  follower INTEGER,
  following INTEGER,
  archive_count INTEGER,
  likes INTEGER,
  views INTEGER,
  run_id TEXT,
  UNIQUE (mid, captured_at)
);

CREATE TABLE IF NOT EXISTS videos (
  bvid TEXT PRIMARY KEY,
  aid INTEGER,
  cid INTEGER,
  mid TEXT,
  author_name TEXT,
  title TEXT,
  description TEXT,
  tid INTEGER,
  tname TEXT,
  pubdate INTEGER,
  pubdate_iso TEXT,
  duration INTEGER,
  view_count INTEGER,
  like_count INTEGER,
  coin_count INTEGER,
  favorite_count INTEGER,
  share_count INTEGER,
  reply_count INTEGER,
  danmaku_count INTEGER,
  tags_json TEXT,
  page_url TEXT,
  page_count INTEGER,
  -- Snowball topology (NULL/0 for plain UP 主归档)
  discovery TEXT,                  -- 'space' | 'seed' | 'snowball'
  depth INTEGER DEFAULT 0,
  parent_bvid TEXT,
  seed_bvid TEXT,
  pass_filter INTEGER,             -- 1 通过门禁 / 0 被剪枝 / NULL 未评估
  reject_reason TEXT,
  -- Derived text metrics, precomputed so queries stay cheap
  title_length INTEGER,
  description_length INTEGER,
  engagement_ratio REAL,           -- reply_count / view_count
  run_id TEXT,
  captured_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_videos_mid ON videos(mid);
CREATE INDEX IF NOT EXISTS idx_videos_pubdate ON videos(pubdate);
CREATE INDEX IF NOT EXISTS idx_videos_tid ON videos(tid);
CREATE INDEX IF NOT EXISTS idx_videos_depth ON videos(depth);
CREATE INDEX IF NOT EXISTS idx_videos_pass ON videos(pass_filter);
CREATE INDEX IF NOT EXISTS idx_videos_run ON videos(run_id);

-- Long form of tags_json: one row per (video, tag) for co-occurrence analysis.
CREATE TABLE IF NOT EXISTS video_tags (
  bvid TEXT NOT NULL REFERENCES videos(bvid),
  tag TEXT NOT NULL,
  position INTEGER,
  PRIMARY KEY (bvid, tag)
);
CREATE INDEX IF NOT EXISTS idx_video_tags_tag ON video_tags(tag);

CREATE TABLE IF NOT EXISTS video_pages (
  bvid TEXT NOT NULL REFERENCES videos(bvid),
  cid INTEGER NOT NULL,
  page INTEGER,
  part TEXT,
  duration INTEGER,
  PRIMARY KEY (bvid, cid)
);

-- Directed recommendation graph: edge (src -> dst) means dst appeared in src's
-- related-video list. Keeps rank so "how strongly related" survives.
CREATE TABLE IF NOT EXISTS snowball_edges (
  run_id TEXT NOT NULL,
  src_bvid TEXT NOT NULL,
  dst_bvid TEXT NOT NULL,
  rank INTEGER,
  discovered_at TEXT,
  PRIMARY KEY (run_id, src_bvid, dst_bvid)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON snowball_edges(src_bvid);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON snowball_edges(dst_bvid);

-- Resumable BFS frontier. A restart reloads this instead of rescanning.
CREATE TABLE IF NOT EXISTS frontier (
  run_id TEXT NOT NULL,
  bvid TEXT NOT NULL,
  depth INTEGER NOT NULL,
  parent_bvid TEXT,
  seed_bvid TEXT,
  state TEXT NOT NULL,             -- 'pending' | 'visiting' | 'done' | 'pruned' | 'error'
  reason TEXT,
  enqueued_at TEXT,
  updated_at TEXT,
  PRIMARY KEY (run_id, bvid)
);
CREATE INDEX IF NOT EXISTS idx_frontier_state ON frontier(run_id, state, depth);

-- Every gate decision, including rejections, so sampling bias is auditable.
CREATE TABLE IF NOT EXISTS gate_decisions (
  run_id TEXT NOT NULL,
  bvid TEXT NOT NULL,
  passed INTEGER NOT NULL,
  reason TEXT,
  checks_json TEXT,
  depth INTEGER,
  decided_at TEXT,
  PRIMARY KEY (run_id, bvid)
);
CREATE INDEX IF NOT EXISTS idx_gate_reason ON gate_decisions(run_id, passed, reason);

CREATE TABLE IF NOT EXISTS comments (
  rpid TEXT PRIMARY KEY,
  target_kind TEXT NOT NULL,       -- 'video' | 'dynamic'
  target_id TEXT NOT NULL,         -- bvid or dyn_id
  oid TEXT,
  comment_type INTEGER,
  bvid TEXT,
  aid INTEGER,
  root_rpid TEXT,
  parent_rpid TEXT,
  hierarchy_level TEXT,            -- 'root' | 'reply'
  user_mid TEXT,
  user_name TEXT,
  user_level INTEGER,
  user_sex TEXT,
  ip_location TEXT,
  content TEXT,
  text_length INTEGER,
  like_count INTEGER,
  reply_count INTEGER,
  pub_ts INTEGER,
  pub_time_iso TEXT,
  run_id TEXT,
  captured_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_comments_target ON comments(target_kind, target_id);
CREATE INDEX IF NOT EXISTS idx_comments_bvid ON comments(bvid);
CREATE INDEX IF NOT EXISTS idx_comments_root ON comments(root_rpid);
CREATE INDEX IF NOT EXISTS idx_comments_level ON comments(hierarchy_level);
CREATE INDEX IF NOT EXISTS idx_comments_len ON comments(text_length);
CREATE INDEX IF NOT EXISTS idx_comments_user ON comments(user_mid);
CREATE INDEX IF NOT EXISTS idx_comments_ts ON comments(pub_ts);

CREATE TABLE IF NOT EXISTS danmaku (
  danmaku_id TEXT PRIMARY KEY,
  bvid TEXT,
  cid INTEGER,
  part TEXT,
  progress_ms INTEGER,
  mode INTEGER,
  fontsize INTEGER,
  color INTEGER,
  pool INTEGER,
  sender_hash TEXT,
  content TEXT,
  text_length INTEGER,
  send_ts INTEGER,
  send_time_iso TEXT,
  run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_danmaku_bvid ON danmaku(bvid);
CREATE INDEX IF NOT EXISTS idx_danmaku_cid ON danmaku(cid);
CREATE INDEX IF NOT EXISTS idx_danmaku_progress ON danmaku(progress_ms);

CREATE TABLE IF NOT EXISTS dynamics (
  dyn_id TEXT PRIMARY KEY,
  mid TEXT,
  author_name TEXT,
  dyn_type TEXT,
  pub_ts INTEGER,
  pub_time_iso TEXT,
  text TEXT,
  text_length INTEGER,
  like_count INTEGER,
  comment_count INTEGER,
  forward_count INTEGER,
  picture_count INTEGER,
  jump_url TEXT,
  ocr_text TEXT,
  run_id TEXT,
  captured_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_dynamics_mid ON dynamics(mid);
CREATE INDEX IF NOT EXISTS idx_dynamics_ts ON dynamics(pub_ts);

CREATE TABLE IF NOT EXISTS dynamic_images (
  dyn_id TEXT NOT NULL REFERENCES dynamics(dyn_id),
  position INTEGER NOT NULL,
  file_name TEXT,
  url TEXT,
  ocr_text TEXT,
  ocr_line_count INTEGER,
  ocr_confidence REAL,
  PRIMARY KEY (dyn_id, position)
);

CREATE TABLE IF NOT EXISTS transcripts (
  bvid TEXT NOT NULL,
  cid INTEGER NOT NULL,
  page INTEGER,
  source TEXT,                     -- 'official' | 'ai' | 'whisper …'
  language TEXT,
  language_probability REAL,
  segment_count INTEGER,
  char_count INTEGER,
  full_text TEXT,
  run_id TEXT,
  captured_at TEXT,
  PRIMARY KEY (bvid, cid)
);

CREATE TABLE IF NOT EXISTS transcript_segments (
  bvid TEXT NOT NULL,
  cid INTEGER NOT NULL,
  position INTEGER NOT NULL,
  start_ms INTEGER,
  end_ms INTEGER,
  text TEXT,
  text_length INTEGER,
  PRIMARY KEY (bvid, cid, position)
);
CREATE INDEX IF NOT EXISTS idx_segments_bvid ON transcript_segments(bvid);

-- Ready-made analysis views ------------------------------------------------

DROP VIEW IF EXISTS v_comment_corpus;
CREATE VIEW v_comment_corpus AS
SELECT
  c.rpid, c.target_kind, c.target_id, c.hierarchy_level,
  c.root_rpid, c.parent_rpid,
  c.user_mid, c.user_name, c.ip_location,
  c.content, c.text_length, c.like_count, c.reply_count,
  c.pub_ts, c.pub_time_iso,
  v.title AS video_title, v.tname AS video_category, v.tid AS video_tid,
  v.view_count AS video_view, v.reply_count AS video_reply,
  v.depth AS snowball_depth, v.seed_bvid, v.run_id AS video_run_id
FROM comments c
LEFT JOIN videos v ON v.bvid = c.bvid;

DROP VIEW IF EXISTS v_video_corpus;
CREATE VIEW v_video_corpus AS
SELECT
  v.*,
  (SELECT COUNT(*) FROM comments c WHERE c.bvid = v.bvid) AS comments_collected,
  (SELECT COUNT(*) FROM comments c WHERE c.bvid = v.bvid AND c.hierarchy_level = 'root') AS root_comments_collected,
  (SELECT COUNT(*) FROM danmaku d WHERE d.bvid = v.bvid) AS danmaku_collected,
  (SELECT COUNT(*) FROM transcripts t WHERE t.bvid = v.bvid) AS transcript_parts
FROM videos v;

DROP VIEW IF EXISTS v_snowball_network;
CREATE VIEW v_snowball_network AS
SELECT
  e.run_id, e.src_bvid, e.dst_bvid, e.rank,
  s.title AS src_title, s.tname AS src_category, s.depth AS src_depth,
  t.title AS dst_title, t.tname AS dst_category, t.depth AS dst_depth,
  t.pass_filter AS dst_passed, t.reject_reason AS dst_reject_reason
FROM snowball_edges e
LEFT JOIN videos s ON s.bvid = e.src_bvid
LEFT JOIN videos t ON t.bvid = e.dst_bvid;

DROP VIEW IF EXISTS v_gate_funnel;
CREATE VIEW v_gate_funnel AS
SELECT run_id, passed, COALESCE(reason, 'pass') AS reason, COUNT(*) AS n
FROM gate_decisions
GROUP BY run_id, passed, COALESCE(reason, 'pass');
"""

CORPUS_TABLES = (
    "collection_runs",
    "authors",
    "author_snapshots",
    "videos",
    "video_tags",
    "video_pages",
    "snowball_edges",
    "frontier",
    "gate_decisions",
    "comments",
    "danmaku",
    "dynamics",
    "dynamic_images",
    "transcripts",
    "transcript_segments",
)

EXPORTABLE_VIEWS = ("v_video_corpus", "v_comment_corpus", "v_snowball_network", "v_gate_funnel")


def _text_length(value: Any) -> int:
    return len(str(value or "").strip())


def _as_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _upsert_sql(table: str, columns: Sequence[str], keys: Sequence[str]) -> str:
    placeholders = ",".join(f":{c}" for c in columns)
    updates = ",".join(f"{c}=excluded.{c}" for c in columns if c not in keys)
    conflict = ",".join(keys)
    tail = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
    return (
        f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders}) "
        f"ON CONFLICT({conflict}) {tail}"
    )


@dataclass
class FrontierItem:
    bvid: str
    depth: int
    parent_bvid: str = ""
    seed_bvid: str = ""


class Corpus:
    """Writer/reader for the analysis dataset."""

    def __init__(self, path: Path | None = None) -> None:
        ensure_dirs()
        self.path = Path(path or CORPUS_DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- runs ---------------------------------------------------------------

    def start_run(self, run_id: str, kind: str, config: dict[str, Any], label: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                _upsert_sql(
                    "collection_runs",
                    ("run_id", "kind", "label", "config_json", "started_at", "finished_at", "status", "note"),
                    ("run_id",),
                ),
                {
                    "run_id": run_id,
                    "kind": kind,
                    "label": label,
                    "config_json": json.dumps(config, ensure_ascii=False),
                    "started_at": now_iso(),
                    "finished_at": None,
                    "status": "running",
                    "note": "",
                },
            )

    def finish_run(self, run_id: str, status: str, note: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE collection_runs SET status=?, finished_at=?, note=? WHERE run_id=?",
                (status, now_iso(), note, run_id),
            )

    # -- authors ------------------------------------------------------------

    def upsert_author(self, row: dict[str, Any]) -> None:
        official = row.get("official") or {}
        if isinstance(official, str):
            try:
                official = json.loads(official)
            except json.JSONDecodeError:
                official = {}
        payload = {
            "mid": str(row.get("mid") or ""),
            "name": row.get("name") or "",
            "sex": row.get("sex") or "",
            "sign": row.get("sign") or "",
            "level": _as_int(row.get("level")),
            "school": row.get("school") or "",
            "official_role": _as_int(official.get("role") if isinstance(official, dict) else None),
            "official_title": (official.get("title") if isinstance(official, dict) else "") or "",
            "birthday": row.get("birthday") or "",
            "face": row.get("face") or "",
            "space_url": row.get("space_url") or "",
            "last_seen": now_iso(),
        }
        if not payload["mid"]:
            return
        columns = tuple(payload) + ("first_seen",)
        payload["first_seen"] = payload["last_seen"]
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO authors({','.join(columns)}) "
                f"VALUES({','.join(':' + c for c in columns)}) "
                "ON CONFLICT(mid) DO UPDATE SET "
                + ",".join(f"{c}=excluded.{c}" for c in columns if c not in {"mid", "first_seen"}),
                payload,
            )

    def add_author_snapshot(self, row: dict[str, Any], run_id: str = "") -> None:
        payload = {
            "mid": str(row.get("mid") or ""),
            "captured_at": row.get("captured_at") or now_iso(),
            "follower": _as_int(row.get("follower"), 0),
            "following": _as_int(row.get("following"), 0),
            "archive_count": _as_int(row.get("archive_count"), 0),
            "likes": _as_int(row.get("likes"), 0),
            "views": _as_int(row.get("views"), 0),
            "run_id": run_id,
        }
        if not payload["mid"]:
            return
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO author_snapshots(mid,captured_at,follower,following,archive_count,likes,views,run_id) "
                "VALUES(:mid,:captured_at,:follower,:following,:archive_count,:likes,:views,:run_id) "
                "ON CONFLICT(mid,captured_at) DO UPDATE SET "
                "follower=excluded.follower, following=excluded.following, "
                "archive_count=excluded.archive_count, likes=excluded.likes, views=excluded.views",
                payload,
            )

    # -- videos -------------------------------------------------------------

    def upsert_video(self, row: dict[str, Any]) -> None:
        tags = row.get("tags") or []
        if isinstance(tags, str):
            tags = [t for t in tags.split(",") if t.strip()]
        view_count = _as_int(row.get("view_count"), 0) or 0
        reply_count = _as_int(row.get("reply_count"), 0) or 0
        pubdate = _as_int(row.get("pubdate"), 0) or 0
        payload = {
            "bvid": str(row.get("bvid") or ""),
            "aid": _as_int(row.get("aid")),
            "cid": _as_int(row.get("cid")),
            "mid": str(row.get("mid") or ""),
            "author_name": row.get("author_name") or "",
            "title": row.get("title") or "",
            "description": row.get("description") or "",
            "tid": _as_int(row.get("tid")),
            "tname": row.get("tname") or "",
            "pubdate": pubdate,
            "pubdate_iso": ts_iso(pubdate),
            "duration": _as_int(row.get("duration"), 0),
            "view_count": view_count,
            "like_count": _as_int(row.get("like_count"), 0),
            "coin_count": _as_int(row.get("coin_count"), 0),
            "favorite_count": _as_int(row.get("favorite_count"), 0),
            "share_count": _as_int(row.get("share_count"), 0),
            "reply_count": reply_count,
            "danmaku_count": _as_int(row.get("danmaku_count"), 0),
            "tags_json": json.dumps(tags, ensure_ascii=False),
            "page_url": row.get("page_url") or "",
            "page_count": _as_int(row.get("page_count"), 1),
            "discovery": row.get("discovery") or "space",
            "depth": _as_int(row.get("depth"), 0),
            "parent_bvid": row.get("parent_bvid") or "",
            "seed_bvid": row.get("seed_bvid") or "",
            "pass_filter": None if row.get("pass_filter") is None else int(bool(row.get("pass_filter"))),
            "reject_reason": row.get("reject_reason") or "",
            "title_length": _text_length(row.get("title")),
            "description_length": _text_length(row.get("description")),
            "engagement_ratio": (reply_count / view_count) if view_count else 0.0,
            "run_id": row.get("run_id") or "",
            "captured_at": row.get("captured_at") or now_iso(),
        }
        if not payload["bvid"]:
            return
        columns = tuple(payload)
        with self.connect() as conn:
            conn.execute(_upsert_sql("videos", columns, ("bvid",)), payload)
            conn.execute("DELETE FROM video_tags WHERE bvid=?", (payload["bvid"],))
            conn.executemany(
                "INSERT OR REPLACE INTO video_tags(bvid,tag,position) VALUES(?,?,?)",
                [(payload["bvid"], str(tag).strip(), i) for i, tag in enumerate(tags) if str(tag).strip()],
            )
            pages = row.get("pages") or []
            conn.executemany(
                "INSERT OR REPLACE INTO video_pages(bvid,cid,page,duration,part) VALUES(?,?,?,?,?)",
                [
                    (
                        payload["bvid"],
                        _as_int(p.get("cid"), 0),
                        _as_int(p.get("page"), i + 1),
                        _as_int(p.get("duration"), 0),
                        p.get("part") or "",
                    )
                    for i, p in enumerate(pages)
                    if _as_int(p.get("cid"))
                ],
            )

    def set_video_topology(self, bvid: str, **fields: Any) -> None:
        allowed = {
            "discovery",
            "depth",
            "parent_bvid",
            "seed_bvid",
            "pass_filter",
            "reject_reason",
            "run_id",
        }
        updates = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "pass_filter" and value is not None:
                value = int(bool(value))
            updates[key] = value
        if not bvid or not updates:
            return
        assignments = ",".join(f"{key}=:{key}" for key in updates)
        updates["bvid"] = bvid
        with self.connect() as conn:
            conn.execute(f"UPDATE videos SET {assignments} WHERE bvid=:bvid", updates)

    def record_gate_decision(
        self,
        run_id: str,
        bvid: str,
        passed: bool,
        reason: str,
        checks: dict[str, Any],
        depth: int = 0,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                _upsert_sql(
                    "gate_decisions",
                    ("run_id", "bvid", "passed", "reason", "checks_json", "depth", "decided_at"),
                    ("run_id", "bvid"),
                ),
                {
                    "run_id": run_id,
                    "bvid": bvid,
                    "passed": int(bool(passed)),
                    "reason": reason,
                    "checks_json": json.dumps(checks, ensure_ascii=False),
                    "depth": depth,
                    "decided_at": now_iso(),
                },
            )

    def add_edges(self, run_id: str, src_bvid: str, dst_bvids: Sequence[str]) -> None:
        stamp = now_iso()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO snowball_edges(run_id,src_bvid,dst_bvid,rank,discovered_at) "
                "VALUES(?,?,?,?,?)",
                [(run_id, src_bvid, dst, i, stamp) for i, dst in enumerate(dst_bvids) if dst],
            )

    # -- frontier -----------------------------------------------------------

    def enqueue(self, run_id: str, items: Iterable[FrontierItem]) -> int:
        stamp = now_iso()
        rows = [
            (run_id, it.bvid, it.depth, it.parent_bvid, it.seed_bvid, "pending", "", stamp, stamp)
            for it in items
            if it.bvid
        ]
        if not rows:
            return 0
        with self.connect() as conn:
            # DO NOTHING keeps the first (shallowest) discovery of a node.
            conn.executemany(
                "INSERT INTO frontier(run_id,bvid,depth,parent_bvid,seed_bvid,state,reason,enqueued_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,bvid) DO NOTHING",
                rows,
            )
            return conn.total_changes

    def next_pending(self, run_id: str) -> FrontierItem | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT bvid, depth, parent_bvid, seed_bvid FROM frontier "
                "WHERE run_id=? AND state IN ('pending','visiting') ORDER BY depth ASC, enqueued_at ASC LIMIT 1",
                (run_id,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE frontier SET state='visiting', updated_at=? WHERE run_id=? AND bvid=?",
                (now_iso(), run_id, row["bvid"]),
            )
        return FrontierItem(
            bvid=row["bvid"],
            depth=int(row["depth"] or 0),
            parent_bvid=row["parent_bvid"] or "",
            seed_bvid=row["seed_bvid"] or "",
        )

    def mark_frontier(self, run_id: str, bvid: str, state: str, reason: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE frontier SET state=?, reason=?, updated_at=? WHERE run_id=? AND bvid=?",
                (state, reason, now_iso(), run_id, bvid),
            )

    def requeue_visiting(self, run_id: str) -> int:
        """A crash leaves rows in 'visiting'; put them back so resume is lossless."""
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE frontier SET state='pending', updated_at=? WHERE run_id=? AND state='visiting'",
                (now_iso(), run_id),
            )
            return cur.rowcount or 0

    def frontier_stats(self, run_id: str) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM frontier WHERE run_id=? GROUP BY state",
                (run_id,),
            ).fetchall()
            depth = conn.execute(
                "SELECT COALESCE(MAX(depth),0) AS d FROM frontier WHERE run_id=? AND state='done'",
                (run_id,),
            ).fetchone()
        stats = {row["state"]: int(row["n"]) for row in rows}
        stats["max_done_depth"] = int(depth["d"] if depth else 0)
        return stats

    def is_known(self, run_id: str, bvid: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM frontier WHERE run_id=? AND bvid=? LIMIT 1", (run_id, bvid)
            ).fetchone()
        return bool(row)

    # -- comments / danmaku -------------------------------------------------

    def upsert_comments(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        target_kind: str,
        target_id: str,
        bvid: str = "",
        aid: int | None = None,
        run_id: str = "",
    ) -> int:
        stamp = now_iso()
        payloads: list[dict[str, Any]] = []
        for raw in rows:
            rpid = str(raw.get("rpid") or "").strip()
            if not rpid:
                continue
            root = str(raw.get("root_rpid") or "").strip()
            parent = str(raw.get("parent_rpid") or "").strip()
            if root in {"", "0"} and parent not in {"", "0"}:
                # Legacy sidecars only carried parent; for one-level nesting the
                # parent of a reply is its root.
                root = parent
            pub_ts = _as_int(raw.get("ctime"), 0) or 0
            payloads.append(
                {
                    "rpid": rpid,
                    "target_kind": target_kind,
                    "target_id": str(target_id),
                    "oid": str(raw.get("oid") or ""),
                    "comment_type": _as_int(raw.get("type")),
                    "bvid": bvid,
                    "aid": aid,
                    "root_rpid": root or "0",
                    "parent_rpid": parent or "0",
                    "hierarchy_level": "root" if root in {"", "0"} else "reply",
                    "user_mid": str(raw.get("mid") or ""),
                    "user_name": raw.get("uname") or "",
                    "user_level": _as_int(raw.get("level")),
                    "user_sex": raw.get("sex") or "",
                    "ip_location": raw.get("ip_location") or "",
                    "content": raw.get("message") or "",
                    "text_length": _text_length(raw.get("message")),
                    "like_count": _as_int(raw.get("like"), 0),
                    "reply_count": _as_int(raw.get("rcount"), 0),
                    "pub_ts": pub_ts,
                    "pub_time_iso": ts_iso(pub_ts),
                    "run_id": run_id,
                    "captured_at": stamp,
                }
            )
        if not payloads:
            return 0
        columns = tuple(payloads[0])
        with self.connect() as conn:
            conn.executemany(_upsert_sql("comments", columns, ("rpid",)), payloads)
        return len(payloads)

    def upsert_danmaku(self, rows: Iterable[dict[str, Any]], *, run_id: str = "") -> int:
        payloads: list[dict[str, Any]] = []
        for raw in rows:
            bvid = str(raw.get("bvid") or "")
            cid = _as_int(raw.get("cid"), 0) or 0
            ident = str(raw.get("id_str") or raw.get("id") or "").strip()
            if not ident:
                ident = f"{cid}:{raw.get('progress_ms') or 0}:{abs(hash(raw.get('content') or '')) % 10**12}"
            send_ts = _as_int(raw.get("ctime"), 0) or 0
            payloads.append(
                {
                    "danmaku_id": f"{cid}:{ident}",
                    "bvid": bvid,
                    "cid": cid,
                    "part": raw.get("part") or "",
                    "progress_ms": _as_int(raw.get("progress_ms"), 0),
                    "mode": _as_int(raw.get("mode"), 0),
                    "fontsize": _as_int(raw.get("fontsize"), 0),
                    "color": _as_int(raw.get("color"), 0),
                    "pool": _as_int(raw.get("pool"), 0),
                    "sender_hash": raw.get("mid_hash") or "",
                    "content": raw.get("content") or "",
                    "text_length": _text_length(raw.get("content")),
                    "send_ts": send_ts,
                    "send_time_iso": ts_iso(send_ts),
                    "run_id": run_id,
                }
            )
        if not payloads:
            return 0
        columns = tuple(payloads[0])
        with self.connect() as conn:
            conn.executemany(_upsert_sql("danmaku", columns, ("danmaku_id",)), payloads)
        return len(payloads)

    # -- dynamics / transcripts --------------------------------------------

    def upsert_dynamic(
        self,
        dyn: dict[str, Any],
        ocr_blocks: Sequence[dict[str, Any]] | None = None,
        *,
        author_name: str = "",
        run_id: str = "",
    ) -> None:
        dyn_id = str(dyn.get("dyn_id") or "")
        if not dyn_id:
            return
        blocks = list(ocr_blocks or [])
        ocr_text = "\n".join(str(b.get("text") or "").strip() for b in blocks if b.get("text")).strip()
        pub_ts = _as_int(dyn.get("pub_ts"), 0) or 0
        pictures = dyn.get("pictures") or []
        payload = {
            "dyn_id": dyn_id,
            "mid": str(dyn.get("mid") or ""),
            "author_name": author_name,
            "dyn_type": dyn.get("dyn_type") or "",
            "pub_ts": pub_ts,
            "pub_time_iso": ts_iso(pub_ts),
            "text": dyn.get("text") or "",
            "text_length": _text_length(dyn.get("text")),
            "like_count": _as_int(dyn.get("like"), 0),
            "comment_count": _as_int(dyn.get("comment"), 0),
            "forward_count": _as_int(dyn.get("forward"), 0),
            "picture_count": len(pictures),
            "jump_url": dyn.get("jump_url") or "",
            "ocr_text": ocr_text,
            "run_id": run_id,
            "captured_at": dyn.get("captured_at") or now_iso(),
        }
        columns = tuple(payload)
        image_rows = []
        for index, block in enumerate(blocks):
            lines = block.get("lines") or []
            confidences = [float(line.get("confidence") or 0) for line in lines]
            image_rows.append(
                (
                    dyn_id,
                    index,
                    block.get("file") or "",
                    pictures[index] if index < len(pictures) else "",
                    block.get("text") or "",
                    len(lines),
                    (sum(confidences) / len(confidences)) if confidences else 0.0,
                )
            )
        if not image_rows:
            image_rows = [
                (dyn_id, index, "", url, "", 0, 0.0) for index, url in enumerate(pictures)
            ]
        with self.connect() as conn:
            conn.execute(_upsert_sql("dynamics", columns, ("dyn_id",)), payload)
            conn.executemany(
                "INSERT OR REPLACE INTO dynamic_images"
                "(dyn_id,position,file_name,url,ocr_text,ocr_line_count,ocr_confidence) "
                "VALUES(?,?,?,?,?,?,?)",
                image_rows,
            )

    def upsert_transcript(
        self,
        *,
        bvid: str,
        cid: int,
        page: int,
        payload: dict[str, Any],
        run_id: str = "",
    ) -> int:
        segments = payload.get("segments") or []
        full_text = "\n".join(str(seg.get("text") or "").strip() for seg in segments).strip()
        if not full_text:
            full_text = str(payload.get("text") or "").strip()
        row = {
            "bvid": bvid,
            "cid": int(cid),
            "page": int(page or 1),
            "source": payload.get("source") or "",
            "language": payload.get("language") or "",
            "language_probability": float(payload.get("language_probability") or 0),
            "segment_count": len(segments),
            "char_count": len(full_text),
            "full_text": full_text,
            "run_id": run_id,
            "captured_at": now_iso(),
        }
        seg_rows = []
        for index, seg in enumerate(segments):
            text = str(seg.get("text") or "").strip()
            seg_rows.append(
                (
                    bvid,
                    int(cid),
                    index,
                    int(float(seg.get("start") or 0) * 1000),
                    int(float(seg.get("end") or 0) * 1000),
                    text,
                    len(text),
                )
            )
        with self.connect() as conn:
            conn.execute(_upsert_sql("transcripts", tuple(row), ("bvid", "cid")), row)
            conn.execute("DELETE FROM transcript_segments WHERE bvid=? AND cid=?", (bvid, int(cid)))
            conn.executemany(
                "INSERT OR REPLACE INTO transcript_segments"
                "(bvid,cid,position,start_ms,end_ms,text,text_length) VALUES(?,?,?,?,?,?,?)",
                seg_rows,
            )
        return len(seg_rows)

    # -- reading ------------------------------------------------------------

    def table_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self.connect() as conn:
            for table in CORPUS_TABLES:
                try:
                    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                    counts[table] = int(row["n"])
                except sqlite3.Error:
                    counts[table] = 0
        return counts

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM collection_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["config"] = json.loads(item.pop("config_json") or "{}")
            except json.JSONDecodeError:
                item["config"] = {}
            out.append(item)
        return out

    def gate_funnel(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT passed, reason, n FROM v_gate_funnel WHERE run_id=? ORDER BY n DESC",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Read-only helper for the UI's dataset explorer."""
        cleaned = sql.strip().rstrip(";")
        lowered = cleaned.lower()
        if not lowered.startswith(("select", "with")):
            raise ValueError("只允许 SELECT 查询")
        if re.search(r"\b(attach|pragma|insert|update|delete|drop|alter|create)\b", lowered):
            raise ValueError("查询中包含不允许的关键字")
        with self.connect() as conn:
            rows = conn.execute(cleaned, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def export_bundle(self, dest: Path | None = None) -> dict[str, Any]:
        """Dump tables/views to CSV plus a copy of corpus.db for R / pandas / Stata."""
        from bili.paths import EXPORT_DIR

        stamp = now_iso().replace(":", "").replace("-", "")[:15]
        folder = Path(dest or (EXPORT_DIR / f"corpus_{stamp}"))
        folder.mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}
        names = list(CORPUS_TABLES) + list(EXPORTABLE_VIEWS)
        with self.connect() as conn:
            for name in names:
                try:
                    rows = conn.execute(f"SELECT * FROM {name}").fetchall()
                except sqlite3.Error:
                    continue
                path = folder / f"{name}.csv"
                if not rows:
                    path.write_text("", encoding="utf-8")
                    counts[name] = 0
                    continue
                fieldnames = list(rows[0].keys())
                with path.open("w", newline="", encoding="utf-8-sig") as fh:
                    writer = csv.DictWriter(fh, fieldnames=fieldnames)
                    writer.writeheader()
                    for row in rows:
                        writer.writerow({key: row[key] for key in fieldnames})
                counts[name] = len(rows)
        db_copy = folder / "corpus.db"
        shutil.copy2(self.path, db_copy)
        readme = folder / "README.txt"
        readme.write_text(
            "BiliArchiver 分析数据集\n"
            f"导出时间：{now_iso()}\n"
            "主库副本：corpus.db（可用 DB Browser / R / pandas / Stata 直接打开）\n"
            "视图：v_video_corpus、v_comment_corpus、v_snowball_network、v_gate_funnel\n",
            encoding="utf-8",
        )
        return {"dir": str(folder), "counts": counts, "db": str(db_copy)}

