"""Write crawl observations into data/merge/research.db.

The crawler still keeps its resume queue in corpus.db. Entity rows and
discovery logs for new work go here, in the research schema.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from bili.paths import DATA_DIR
from bili.util import now_iso

RESEARCH_DB_PATH = DATA_DIR / "merge" / "research.db"
SCHEMA_PATH = DATA_DIR / "merge" / "schema.sql"

KIND_TO_METHOD = {
    "academic": "snowball",
    "snowball": "snowball",
    "seed": "snowball",
    "keyword": "keyword",
    "scout": "creator_search",
    "archive": "creator_search",
    "space": "creator_search",
    "transcribe": "transcribe",
}

VIDEO_COLUMNS = (
    "bvid", "aid", "cid", "mid", "author_name", "title", "description", "tid", "tname",
    "pubdate", "pubdate_iso", "duration", "view_count", "like_count", "coin_count",
    "favorite_count", "share_count", "reply_count", "danmaku_count", "tags_json",
    "page_url", "page_count", "title_length", "description_length", "engagement_ratio",
    "captured_at",
)


def _blank(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def _method_for(kind: str) -> str:
    key = (kind or "").strip()
    return KIND_TO_METHOD.get(key, key or "unknown")


class ResearchSink:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or RESEARCH_DB_PATH)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        self._ensure_schema(conn)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_batches'"
        ).fetchone()
        if row is None:
            if not SCHEMA_PATH.is_file():
                raise RuntimeError(f"research.db 没有表，也找不到 schema：{SCHEMA_PATH}")
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transcripts (
              bvid TEXT NOT NULL,
              cid INTEGER NOT NULL,
              page INTEGER,
              source TEXT,
              language TEXT,
              language_probability REAL,
              segment_count INTEGER,
              char_count INTEGER,
              full_text TEXT,
              run_id TEXT,
              captured_at TEXT,
              PRIMARY KEY (bvid, cid)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transcript_segments (
              bvid TEXT NOT NULL,
              cid INTEGER NOT NULL,
              position INTEGER NOT NULL,
              start_ms INTEGER,
              end_ms INTEGER,
              text TEXT,
              text_length INTEGER,
              PRIMARY KEY (bvid, cid, position)
            )
            """
        )

    def _ensure_batch(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        kind: str = "",
        label: str = "",
        keywords: str = "",
    ) -> None:
        if not run_id:
            return
        conn.execute(
            """
            INSERT INTO collection_batches (
              batch_id, method, batch_label, search_keyword, started_at, status
            ) VALUES (?, ?, ?, ?, ?, 'running')
            ON CONFLICT(batch_id) DO UPDATE SET
              method = CASE
                WHEN collection_batches.method IN ('', 'unknown') THEN excluded.method
                ELSE collection_batches.method
              END,
              batch_label = COALESCE(NULLIF(excluded.batch_label, ''), collection_batches.batch_label),
              search_keyword = COALESCE(NULLIF(excluded.search_keyword, ''), collection_batches.search_keyword)
            """,
            (run_id, _method_for(kind), label or None, keywords or None, now_iso()),
        )

    def ensure_batch(self, run_id: str, kind: str = "", label: str = "", keywords: str = "") -> None:
        if not run_id:
            return
        with self.connect() as conn:
            self._ensure_batch(conn, run_id, kind, label, keywords)

    def finish_batch(self, run_id: str, status: str) -> None:
        if not run_id:
            return
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE collection_batches
                SET status = ?, finished_at = ?
                WHERE batch_id = ?
                """,
                (status, now_iso(), run_id),
            )

    def upsert_creator(self, payload: dict[str, Any]) -> None:
        mid = _blank(payload.get("mid"))
        if not mid:
            return
        row = {
            "mid": str(mid),
            "name": payload.get("name") or None,
            "sex": payload.get("sex") or None,
            "sign": payload.get("sign") or None,
            "level": payload.get("level"),
            "school": payload.get("school") or None,
            "official_role": payload.get("official_role"),
            "official_title": payload.get("official_title") or None,
            "birthday": payload.get("birthday") or None,
            "face": payload.get("face") or None,
            "space_url": payload.get("space_url") or None,
            "last_seen": payload.get("last_seen") or now_iso(),
        }
        columns = list(row) + ["first_seen"]
        row["first_seen"] = row["last_seen"]
        assignments = ", ".join(
            f"{column}=COALESCE(excluded.{column}, creators.{column})"
            for column in columns
            if column not in {"mid", "first_seen"}
        )
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO creators ({', '.join(columns)}) VALUES ({', '.join(':' + c for c in columns)}) "
                f"ON CONFLICT(mid) DO UPDATE SET {assignments}",
                row,
            )

    def upsert_video(self, payload: dict[str, Any], pages: Iterable[dict[str, Any]] | None = None) -> None:
        bvid = str(payload.get("bvid") or "").strip()
        if not bvid:
            return
        mid = _blank(payload.get("mid"))
        if mid:
            self.upsert_creator({"mid": str(mid), "name": payload.get("author_name") or ""})
        row = {column: payload.get(column) for column in VIDEO_COLUMNS}
        row["bvid"] = bvid
        row["mid"] = str(mid) if mid else None
        assignments = ", ".join(f"{column}=excluded.{column}" for column in VIDEO_COLUMNS if column != "bvid")
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO videos ({', '.join(VIDEO_COLUMNS)}) "
                f"VALUES ({', '.join(':' + c for c in VIDEO_COLUMNS)}) "
                f"ON CONFLICT(bvid) DO UPDATE SET {assignments}",
                row,
            )
            for index, page in enumerate(pages or []):
                if not isinstance(page, dict):
                    continue
                cid = page.get("cid")
                if cid in (None, ""):
                    continue
                conn.execute(
                    """
                    INSERT INTO video_parts (bvid, cid, page, part, duration)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(bvid, cid) DO UPDATE SET
                      page=excluded.page, part=excluded.part, duration=excluded.duration
                    """,
                    (bvid, cid, page.get("page") or index + 1, page.get("part") or None, page.get("duration")),
                )
        self._video_discovery(payload)

    def _remember_keyword(self, conn: sqlite3.Connection, keyword: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO keyword_dictionary (keyword, keyword_category, added_reason) VALUES (?, NULL, NULL)",
            (keyword,),
        )

    def _video_discovery(self, payload: dict[str, Any]) -> None:
        run_id = str(payload.get("run_id") or "").strip()
        bvid = str(payload.get("bvid") or "").strip()
        if not run_id or not bvid:
            return
        self.ensure_batch(run_id, str(payload.get("discovery") or ""))
        passed = payload.get("pass_filter")
        if passed is None:
            gate = payload.get("reject_reason") or None
        elif int(bool(passed)):
            gate = payload.get("reject_reason") or "pass"
        else:
            gate = payload.get("reject_reason") or "reject"
        keyword = _blank(payload.get("keyword"))
        creator_mid = None
        with self.connect() as conn:
            if keyword:
                self._remember_keyword(conn, str(keyword))
            method = conn.execute(
                "SELECT method FROM collection_batches WHERE batch_id = ?",
                (run_id,),
            ).fetchone()
            if method and method["method"] == "creator_search":
                creator_mid = _blank(payload.get("mid"))
            conn.execute(
                """
                INSERT INTO video_discovery_log (
                  bvid, batch_id, discovery_depth, gate_result,
                  matched_via_keyword, matched_via_creator_mid, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bvid, batch_id) DO UPDATE SET
                  discovery_depth = COALESCE(excluded.discovery_depth, video_discovery_log.discovery_depth),
                  gate_result = COALESCE(excluded.gate_result, video_discovery_log.gate_result),
                  matched_via_keyword = COALESCE(excluded.matched_via_keyword, video_discovery_log.matched_via_keyword),
                  matched_via_creator_mid = COALESCE(excluded.matched_via_creator_mid, video_discovery_log.matched_via_creator_mid),
                  discovered_at = excluded.discovered_at
                """,
                (
                    bvid,
                    run_id,
                    payload.get("depth"),
                    gate,
                    str(keyword) if keyword else None,
                    str(creator_mid) if creator_mid else None,
                    payload.get("captured_at") or now_iso(),
                ),
            )

    def note_gate(self, run_id: str, bvid: str, passed: bool, reason: str, depth: int) -> None:
        if not run_id or not bvid:
            return
        self.ensure_batch(run_id)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO video_discovery_log (bvid, batch_id, discovery_depth, gate_result, discovered_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(bvid, batch_id) DO UPDATE SET
                  discovery_depth = excluded.discovery_depth,
                  gate_result = excluded.gate_result,
                  discovered_at = excluded.discovered_at
                """,
                (bvid, run_id, depth, reason or ("pass" if passed else "reject"), now_iso()),
            )

    def upsert_comments(self, payloads: list[dict[str, Any]]) -> None:
        if not payloads:
            return
        columns = (
            "rpid", "target_kind", "target_id", "oid", "comment_type", "bvid", "aid",
            "root_rpid", "parent_rpid", "hierarchy_level", "user_mid", "user_name",
            "user_level", "user_sex", "ip_location", "content", "text_length",
            "like_count", "reply_count", "pub_ts", "pub_time_iso", "captured_at",
        )
        rows = []
        for payload in payloads:
            row = {column: payload.get(column) for column in columns}
            row["bvid"] = _blank(row.get("bvid"))
            rows.append(row)
        assignments = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "rpid")
        with self.connect() as conn:
            conn.executemany(
                f"INSERT INTO comments ({', '.join(columns)}) VALUES ({', '.join(':' + c for c in columns)}) "
                f"ON CONFLICT(rpid) DO UPDATE SET {assignments}",
                rows,
            )

    def upsert_danmaku(self, payloads: list[dict[str, Any]]) -> None:
        if not payloads:
            return
        columns = (
            "danmaku_id", "bvid", "cid", "part", "progress_ms", "mode", "fontsize",
            "color", "pool", "sender_hash", "content", "text_length", "send_ts", "send_time_iso",
        )
        rows = [{column: payload.get(column) for column in columns} for payload in payloads]
        assignments = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "danmaku_id")
        with self.connect() as conn:
            conn.executemany(
                f"INSERT INTO danmaku ({', '.join(columns)}) VALUES ({', '.join(':' + c for c in columns)}) "
                f"ON CONFLICT(danmaku_id) DO UPDATE SET {assignments}",
                rows,
            )

    def upsert_dynamic(self, payload: dict[str, Any]) -> None:
        dyn_id = str(payload.get("dyn_id") or "").strip()
        if not dyn_id:
            return
        mid = _blank(payload.get("mid"))
        if mid:
            self.upsert_creator({"mid": str(mid), "name": payload.get("author_name") or ""})
        columns = (
            "dyn_id", "mid", "author_name", "dyn_type", "pub_ts", "pub_time_iso", "text",
            "text_length", "like_count", "comment_count", "forward_count", "picture_count",
            "jump_url", "ocr_text", "captured_at",
        )
        row = {column: payload.get(column) for column in columns}
        row["dyn_id"] = dyn_id
        row["mid"] = str(mid) if mid else None
        assignments = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "dyn_id")
        keyword = _blank(payload.get("keyword"))
        run_id = str(payload.get("run_id") or "").strip()
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO dynamics ({', '.join(columns)}) VALUES ({', '.join(':' + c for c in columns)}) "
                f"ON CONFLICT(dyn_id) DO UPDATE SET {assignments}",
                row,
            )
            if run_id:
                self._ensure_batch(conn, run_id, "keyword" if keyword else "")
                if keyword:
                    self._remember_keyword(conn, str(keyword))
                conn.execute(
                    """
                    INSERT INTO dynamic_discovery_log (dyn_id, batch_id, matched_via_keyword, discovered_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(dyn_id, batch_id) DO UPDATE SET
                      matched_via_keyword = COALESCE(excluded.matched_via_keyword, dynamic_discovery_log.matched_via_keyword),
                      discovered_at = excluded.discovered_at
                    """,
                    (dyn_id, run_id, str(keyword) if keyword else None, payload.get("captured_at") or now_iso()),
                )

    def upsert_transcript(self, row: dict[str, Any], segments: list[tuple]) -> None:
        columns = (
            "bvid", "cid", "page", "source", "language", "language_probability",
            "segment_count", "char_count", "full_text", "run_id", "captured_at",
        )
        payload = {column: row.get(column) for column in columns}
        assignments = ", ".join(f"{column}=excluded.{column}" for column in columns if column not in {"bvid", "cid"})
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO transcripts ({', '.join(columns)}) VALUES ({', '.join(':' + c for c in columns)}) "
                f"ON CONFLICT(bvid, cid) DO UPDATE SET {assignments}",
                payload,
            )
            conn.execute(
                "DELETE FROM transcript_segments WHERE bvid = ? AND cid = ?",
                (payload["bvid"], payload["cid"]),
            )
            conn.executemany(
                """
                INSERT INTO transcript_segments (bvid, cid, position, start_ms, end_ms, text, text_length)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                segments,
            )
