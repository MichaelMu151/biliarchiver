from __future__ import annotations

import json
import os
import random
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

TZ_SHANGHAI = timezone(timedelta(hours=8))
UID_RE = re.compile(r"(?:space\.bilibili\.com/)?(\d{1,20})")
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")

TIME_RANGES: dict[str, int | None] = {
    "all": None,
    "3y": 365 * 3,
    "2y": 365 * 2,
    "1y": 365,
    "9m": 273,
    "6m": 182,
    "3m": 90,
}

RANGE_LABELS = {
    "all": "全部",
    "3y": "近 3 年",
    "2y": "近 2 年",
    "1y": "近 1 年",
    "9m": "近 9 个月",
    "6m": "近 6 个月",
    "3m": "近 3 个月",
}


def now_iso() -> str:
    return datetime.now(TZ_SHANGHAI).isoformat(timespec="seconds")


def ts_iso(ts: int | float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), TZ_SHANGHAI).isoformat(timespec="seconds")


def cutoff_ts(range_key: str) -> int | None:
    days = TIME_RANGES.get(range_key, 365)
    if days is None:
        return None
    return int(time.time()) - days * 86400


def parse_uids(raw: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[\s,;，；]+", raw.strip()):
        if not part:
            continue
        match = UID_RE.search(part)
        if not match:
            continue
        uid = match.group(1)
        if uid not in seen:
            seen.add(uid)
            found.append(uid)
    return found


def parse_bvids(raw: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in BV_RE.finditer(raw or ""):
        bvid = match.group(0)
        if bvid not in seen:
            seen.add(bvid)
            found.append(bvid)
    return found


def safe_name(text: str, limit: int = 60) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\n\r\t]+', "_", text).strip(" ._")
    return (cleaned or "untitled")[:limit]


def parse_cookie_string(raw: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for piece in (raw or "").split(";"):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        key = key.strip()
        if key:
            cookies[key] = value.strip()
    return cookies


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def replace_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    """Write a complete JSONL dataset and atomically publish it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                count += 1
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return count


def write_text(path: Path, text: str) -> None:
    atomic_write_text(path, text)


def atomic_write_text(path: Path, text: str) -> None:
    """Prevent interrupted writes from leaving corrupt manifests or Markdown."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def jitter(low: float, high: float) -> float:
    return random.uniform(low, high)


def pick(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = d
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def abs_url(url: str | None) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return url


def host_ok(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host.endswith(x) for x in ("bilibili.com", "hdslb.com", "bilivideo.com", "akamaized.net"))
