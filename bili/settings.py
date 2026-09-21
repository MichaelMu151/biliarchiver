from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any

from bili.paths import SETTINGS_PATH, ensure_dirs


DEFAULTS: dict[str, Any] = {
    "cookie": "",
    "min_interval": 0.8,
    "max_interval": 1.8,
    "max_retries": 5,
    "comment_max_pages": 0,
    "include_sub_replies": True,
    "danmaku_mode": "full",
    "media_mode": "link",
    "transcribe_mode": "official",
    "ocr_enabled": True,
    "whisper_model": "small",
    "whisper_language": "auto",
    "ocr_min_confidence": 0.55,
    "video_quality": 64,
    "ffmpeg_path": "ffmpeg",
    "output_dir": "",
    "media_keep": "delete_after_text",
    "rclone_remote": "gdrive",
    "rclone_root": "BiliArchiver",
}


@dataclass
class AppSettings:
    cookie: str = ""
    min_interval: float = 0.8
    max_interval: float = 1.8
    max_retries: int = 5
    comment_max_pages: int = 0
    include_sub_replies: bool = True
    danmaku_mode: str = "full"
    media_mode: str = "link"
    transcribe_mode: str = "official"
    ocr_enabled: bool = True
    whisper_model: str = "small"
    whisper_language: str = "auto"
    ocr_min_confidence: float = 0.55
    video_quality: int = 64
    ffmpeg_path: str = "ffmpeg"
    output_dir: str = ""
    media_keep: str = "delete_after_text"
    rclone_remote: str = "gdrive"
    rclone_root: str = "BiliArchiver"

    def to_public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        cookie = data.pop("cookie") or ""
        data["has_cookie"] = bool(cookie)
        data["cookie_preview"] = _mask_cookie(cookie)
        return data


def _mask_cookie(cookie: str) -> str:
    if not cookie:
        return ""
    sess = ""
    for part in cookie.split(";"):
        if "SESSDATA=" in part:
            sess = part.split("=", 1)[-1].strip()
            break
    if not sess:
        return "已保存（无 SESSDATA 字段）"
    if len(sess) < 8:
        return "SESSDATA=****"
    return f"SESSDATA={sess[:4]}****{sess[-4:]}"


def load_settings() -> AppSettings:
    ensure_dirs()
    if not SETTINGS_PATH.exists():
        return AppSettings()
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppSettings()
    merged = deepcopy(DEFAULTS)
    merged.update({k: v for k, v in raw.items() if k in DEFAULTS})
    return AppSettings(**merged)


def save_settings(settings: AppSettings) -> None:
    ensure_dirs()
    SETTINGS_PATH.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
