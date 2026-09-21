from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlparse

import httpx


def normalize_bark_key(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        path = urlparse(text).path.strip("/")
        return path.split("/")[0] if path else ""
    return text.strip("/")


def bark_endpoint(server: str, key: str) -> str:
    base = (server or "https://api.day.app").rstrip("/")
    return f"{base}/{normalize_bark_key(key)}"


async def send_bark(
    *,
    key: str,
    body: str,
    title: str = "b站爬虫",
    sound: str = "bell",
    server: str = "https://api.day.app",
    group: str = "biliarchiver",
) -> dict[str, Any]:
    token = normalize_bark_key(key)
    if not token:
        return {"ok": False, "skipped": True, "error": "未配置 Bark Key"}
    url = bark_endpoint(server, token)
    payload = {
        "title": title or "b站爬虫",
        "body": (body or "")[:500],
        "sound": sound or "bell",
        "group": group,
        "level": "active",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            response = await client.post(url, json=payload)
            if response.status_code >= 400:
                path = f"{url}/{quote(payload['title'], safe='')}/{quote(payload['body'], safe='')}"
                response = await client.get(path, params={"sound": payload["sound"]})
            ok = response.status_code < 400
            detail: Any
            try:
                detail = response.json()
            except Exception:
                detail = (response.text or "")[-240:]
            return {"ok": ok, "status": response.status_code, "data": detail}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def progress_messages(event: str, payload: dict[str, Any] | None = None) -> str:
    data = payload or {}
    current = str(data.get("current") or "").strip()
    videos = int(data.get("videos_done") or 0)
    dyns = int(data.get("dynamics_done") or 0)
    uids_done = int(data.get("uids_done") or 0)
    uids_total = int(data.get("uids_total") or 0)
    if event == "start":
        return f"任务开始，共 {uids_total} 个账号"
    if event == "video":
        return f"视频完成 {videos} 条\n{current}" if current else f"视频完成 {videos} 条"
    if event == "uid":
        return f"账号完成 {uids_done}/{uids_total}\n视频 {videos} · 动态 {dyns}"
    if event == "done":
        return f"采集完成。账号 {uids_done}/{uids_total}，视频 {videos}，动态 {dyns}"
    if event == "cancelled":
        return f"任务已取消。已完成视频 {videos}，动态 {dyns}"
    if event == "error":
        return f"任务失败：{data.get('error') or '未知错误'}"
    return current or event
