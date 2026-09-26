from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from bili.paths import DB_PATH, ensure_dirs
from bili.util import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  mid TEXT PRIMARY KEY,
  name TEXT,
  sign TEXT,
  face TEXT,
  level INTEGER,
  official TEXT,
  space_url TEXT,
  sex TEXT,
  school TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS account_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mid TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  follower INTEGER,
  following INTEGER,
  archive_count INTEGER,
  likes INTEGER,
  views INTEGER,
  raw_json TEXT
);
CREATE TABLE IF NOT EXISTS videos (
  bvid TEXT PRIMARY KEY,
  mid TEXT,
  aid TEXT,
  cid TEXT,
  title TEXT,
  pubdate INTEGER,
  duration INTEGER,
  view INTEGER,
  like_n INTEGER,
  coin INTEGER,
  favorite INTEGER,
  share INTEGER,
  reply INTEGER,
  danmaku INTEGER,
  tname TEXT,
  description TEXT,
  page_url TEXT,
  local_audio TEXT,
  local_video TEXT,
  transcript_path TEXT,
  markdown_path TEXT,
  status TEXT,
  error TEXT,
  captured_at TEXT
);
CREATE TABLE IF NOT EXISTS dynamics (
  dyn_id TEXT PRIMARY KEY,
  mid TEXT,
  dyn_type TEXT,
  pub_ts INTEGER,
  text TEXT,
  like_n INTEGER,
  comment_n INTEGER,
  forward_n INTEGER,
  markdown_path TEXT,
  status TEXT,
  error TEXT,
  captured_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  created_at TEXT,
  config_json TEXT,
  status TEXT,
  progress_json TEXT,
  error TEXT,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS job_items (
  job_id TEXT,
  item_key TEXT,
  status TEXT,
  detail TEXT,
  PRIMARY KEY (job_id, item_key)
);
"""


class Store:
    def __init__(self, path: Path | None = None):
        ensure_dirs()
        self.path = Path(path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._add_missing_columns(conn)

    @staticmethod
    def _add_missing_columns(conn: sqlite3.Connection) -> None:
        """Existing databases predate some columns; CREATE TABLE IF NOT EXISTS won't add them."""
        wanted = {"accounts": {"sex": "TEXT", "school": "TEXT"}}
        for table, columns in wanted.items():
            have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, ddl in columns.items():
                if column not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert_account(self, row: dict[str, Any]) -> None:
        payload = dict(row)
        if isinstance(payload.get("official"), (dict, list)):
            payload["official"] = json.dumps(payload["official"], ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO accounts(mid,name,sign,face,level,official,space_url,sex,school,updated_at)
                VALUES(:mid,:name,:sign,:face,:level,:official,:space_url,:sex,:school,:updated_at)
                ON CONFLICT(mid) DO UPDATE SET
                  name=excluded.name, sign=excluded.sign, face=excluded.face,
                  level=excluded.level, official=excluded.official,
                  space_url=excluded.space_url, sex=excluded.sex,
                  school=excluded.school, updated_at=excluded.updated_at
                """,
                payload,
            )

    def add_snapshot(self, row: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO account_snapshots(mid,captured_at,follower,following,archive_count,likes,views,raw_json)
                VALUES(:mid,:captured_at,:follower,:following,:archive_count,:likes,:views,:raw_json)
                """,
                row,
            )

    def upsert_video(self, row: dict[str, Any]) -> None:
        keys = [
            "bvid", "mid", "aid", "cid", "title", "pubdate", "duration", "view",
            "like_n", "coin", "favorite", "share", "reply", "danmaku", "tname",
            "description", "page_url", "local_audio", "local_video",
            "transcript_path", "markdown_path", "status", "error", "captured_at",
        ]
        payload = {k: row.get(k) for k in keys}
        assignments = ",".join(f"{k}=excluded.{k}" for k in keys if k != "bvid")
        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO videos({','.join(keys)})
                VALUES({','.join(':'+k for k in keys)})
                ON CONFLICT(bvid) DO UPDATE SET {assignments}
                """,
                payload,
            )

    def upsert_dynamic(self, row: dict[str, Any]) -> None:
        keys = [
            "dyn_id", "mid", "dyn_type", "pub_ts", "text", "like_n", "comment_n",
            "forward_n", "markdown_path", "status", "error", "captured_at",
        ]
        payload = {k: row.get(k) for k in keys}
        assignments = ",".join(f"{k}=excluded.{k}" for k in keys if k != "dyn_id")
        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO dynamics({','.join(keys)})
                VALUES({','.join(':'+k for k in keys)})
                ON CONFLICT(dyn_id) DO UPDATE SET {assignments}
                """,
                payload,
            )

    def save_job(self, job_id: str, config: dict[str, Any], status: str, progress: dict[str, Any], error: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs(id,created_at,config_json,status,progress_json,error,finished_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  status=excluded.status, progress_json=excluded.progress_json,
                  error=excluded.error, finished_at=excluded.finished_at
                """,
                (
                    job_id,
                    now_iso(),
                    json.dumps(config, ensure_ascii=False),
                    status,
                    json.dumps(progress, ensure_ascii=False),
                    error,
                    now_iso() if status in {"done", "error", "cancelled"} else None,
                ),
            )

    def mark_item(self, job_id: str, item_key: str, status: str, detail: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO job_items(job_id,item_key,status,detail)
                VALUES(?,?,?,?)
                ON CONFLICT(job_id,item_key) DO UPDATE SET status=excluded.status, detail=excluded.detail
                """,
                (job_id, item_key, status, detail),
            )

    def item_done(self, job_id: str, item_key: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status FROM job_items WHERE job_id=? AND item_key=?",
                (job_id, item_key),
            ).fetchone()
        return bool(row and row["status"] == "done")

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.*, s.follower, s.following, s.archive_count, s.likes, s.captured_at AS snapshot_at
                FROM accounts a
                LEFT JOIN (
                  SELECT * FROM account_snapshots WHERE id IN (
                    SELECT MAX(id) FROM account_snapshots GROUP BY mid
                  )
                ) s ON a.mid = s.mid
                ORDER BY a.updated_at DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def account_detail(self, mid: str) -> dict[str, Any]:
        with self.connect() as conn:
            acc = conn.execute("SELECT * FROM accounts WHERE mid=?", (mid,)).fetchone()
            snaps = conn.execute(
                "SELECT * FROM account_snapshots WHERE mid=? ORDER BY id DESC LIMIT 50",
                (mid,),
            ).fetchall()
            videos = conn.execute(
                "SELECT * FROM videos WHERE mid=? ORDER BY pubdate DESC",
                (mid,),
            ).fetchall()
            dyns = conn.execute(
                "SELECT * FROM dynamics WHERE mid=? ORDER BY pub_ts DESC",
                (mid,),
            ).fetchall()
        return {
            "account": dict(acc) if acc else None,
            "snapshots": [dict(x) for x in snaps],
            "videos": [dict(x) for x in videos],
            "dynamics": [dict(x) for x in dyns],
        }

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["config"] = json.loads(item.pop("config_json") or "{}")
            item["progress"] = json.loads(item.pop("progress_json") or "{}")
        except json.JSONDecodeError:
            item["config"] = {}
            item["progress"] = {}
        return item

    def list_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["config"] = json.loads(item.pop("config_json") or "{}")
                item["progress"] = json.loads(item.pop("progress_json") or "{}")
            except json.JSONDecodeError:
                item["config"] = {}
                item["progress"] = {}
            out.append(item)
        return out

    def mark_interrupted_jobs(self) -> list[dict[str, Any]]:
        """A process restart cannot keep in-memory tasks alive; make that explicit."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, progress_json FROM jobs
                WHERE status IN ('queued','running','cancelling')
                """
            ).fetchall()
            conn.execute(
                """
                UPDATE jobs
                SET status='interrupted',
                    error=CASE WHEN error='' THEN '应用曾退出；重新创建同样任务即可从磁盘检查点续跑' ELSE error END,
                    finished_at=?
                WHERE status IN ('queued','running','cancelling')
                """,
                (now_iso(),),
            )
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["progress"] = json.loads(item.pop("progress_json") or "{}")
            except json.JSONDecodeError:
                item["progress"] = {}
            out.append(item)
        return out
