from __future__ import annotations

import time
import urllib.parse
from functools import reduce
from hashlib import md5
from typing import Any

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return reduce(lambda s, i: s + raw[i], MIXIN_KEY_ENC_TAB, "")[:32]


def enc_wbi(params: dict[str, Any], img_key: str, sub_key: str) -> dict[str, Any]:
    signed = {str(k): v for k, v in params.items() if v is not None}
    signed["wts"] = int(time.time())
    signed = dict(sorted(signed.items()))
    signed = {
        k: "".join(ch for ch in str(v) if ch not in "!'()*")
        for k, v in signed.items()
    }
    query = urllib.parse.urlencode(signed, quote_via=urllib.parse.quote)
    signed["w_rid"] = md5((query + mixin_key(img_key, sub_key)).encode()).hexdigest()
    return signed


def keys_from_nav(nav: dict[str, Any]) -> tuple[str, str]:
    wbi = (nav.get("data") or {}).get("wbi_img") or {}
    img = str(wbi.get("img_url") or "")
    sub = str(wbi.get("sub_url") or "")
    img_key = img.rsplit("/", 1)[-1].split(".")[0]
    sub_key = sub.rsplit("/", 1)[-1].split(".")[0]
    if not img_key or not sub_key:
        raise RuntimeError("未能从 nav 接口取得 WBI 密钥")
    return img_key, sub_key
