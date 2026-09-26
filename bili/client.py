from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import random
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx

from bili import wbi
from bili.danmaku import parse_seg_protobuf, parse_xml_danmaku
from bili.settings import AppSettings
from bili.util import abs_url, jitter, parse_cookie_string, pick

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

RISK_CODES = {-352, -412, -799, -509, 412, -403, -401}


def has_risk_voucher(payload: dict[str, Any] | None) -> bool:
    """Related-video APIs return ``data`` as a list; only dict payloads have v_voucher."""
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    return isinstance(data, dict) and bool(data.get("v_voucher"))

# Browser-like fingerprint fields required by some space / search WBI APIs.
DM_IMG = {
    "dm_img_list": "[]",
    "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
    "dm_cover_img_str": "QU5HTEUgKEludGVsLCBJbnRlbChSKSBVSEQgR3JhcGhpY3MgNjIwKCB4ODYpIEdvb2dsZSBJbmMuLCBXaW5kb3dzKQ",
    "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
}


class BiliError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class BiliClient:
    def __init__(self, settings: AppSettings, on_log=None):
        self.settings = settings
        self.on_log = on_log or (lambda *_a, **_k: None)
        self.cookies = parse_cookie_string(settings.cookie)
        self._img_key = ""
        self._sub_key = ""
        self._wbi_expire = 0.0
        self._last_request = 0.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True,
            trust_env=False,
            headers={
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Origin": "https://www.bilibili.com",
                "Referer": "https://www.bilibili.com/",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def bootstrap(self) -> dict[str, Any]:
        await self._ensure_buvid()
        await self._ensure_bili_ticket()
        nav = await self.get_nav()
        login = bool(pick(nav, "data", "isLogin"))
        uname = pick(nav, "data", "uname") or ""
        self.on_log("info", f"会话就绪 · 登录状态={'已登录 '+uname if login else '匿名'}")
        return {"is_login": login, "uname": uname, "mid": pick(nav, "data", "mid")}

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items() if v)

    async def _sleep_gate(self) -> None:
        gap = jitter(self.settings.min_interval, self.settings.max_interval)
        wait = self._last_request + gap - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request = time.monotonic()

    async def _ensure_buvid(self) -> None:
        if self.cookies.get("buvid3"):
            return
        data = await self.get_json("https://api.bilibili.com/x/frontend/finger/spi", wbi=False, signed=False)
        payload = data.get("data") or {}
        if payload.get("b_3"):
            self.cookies["buvid3"] = payload["b_3"]
        if payload.get("b_4"):
            self.cookies["buvid4"] = payload["b_4"]
        self.cookies.setdefault("b_nut", str(int(time.time())))

    async def _ensure_bili_ticket(self) -> None:
        if self.cookies.get("bili_ticket"):
            return
        ts = int(time.time())
        hexsign = hmac.new(b"XgwSnGZ1p", f"ts{ts}".encode(), hashlib.sha256).hexdigest()
        try:
            data = await self.request_json(
                "POST",
                "https://api.bilibili.com/bili_ticket/v1/getTicket",
                data={"hexsign": hexsign, "csrf": self.cookies.get("bili_jct", ""), "ts": ts},
            )
            ticket = pick(data, "data", "ticket")
            ttl = pick(data, "data", "ttl") or 259200
            if ticket:
                self.cookies["bili_ticket"] = ticket
                self.cookies["bili_ticket_expires"] = str(ts + int(ttl))
        except Exception as exc:
            if "404" not in str(exc):
                self.on_log("warn", f"bili_ticket 获取失败（可继续）：{exc}")

    async def _refresh_wbi(self, force: bool = False) -> None:
        if not force and self._img_key and time.monotonic() < self._wbi_expire:
            return
        nav = await self.get_json("https://api.bilibili.com/x/web-interface/nav", wbi=False, signed=False)
        self._img_key, self._sub_key = wbi.keys_from_nav(nav)
        self._wbi_expire = time.monotonic() + 50 * 60

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        wbi_sign: bool = False,
        referer: str | None = None,
        binary: bool = False,
    ) -> httpx.Response:
        retries = max(1, int(self.settings.max_retries))
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            await self._sleep_gate()
            req_params = dict(params or {})
            if wbi_sign:
                await self._refresh_wbi()
                req_params = wbi.enc_wbi(req_params, self._img_key, self._sub_key)
            headers = {}
            if referer:
                headers["Referer"] = referer
            try:
                resp = await self._client.request(
                    method,
                    url,
                    params=req_params,
                    data=data,
                    headers=headers,
                    cookies=self.cookies,
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                await asyncio.sleep(min(60, 2 ** attempt) + jitter(0.2, 1.2))
                continue

            if resp.status_code in (412, 429, 503):
                backoff = min(24, 3 * attempt) + jitter(0.4, 1.2)
                self.on_log("warn", f"HTTP {resp.status_code} · {url} · {backoff:.0f}s 后重试 ({attempt}/{retries})")
                await asyncio.sleep(backoff)
                last_exc = BiliError(f"HTTP {resp.status_code}", resp.status_code, True)
                continue

            if not binary:
                try:
                    payload = resp.json()
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    code = payload.get("code")
                    if code in RISK_CODES or has_risk_voucher(payload):
                        if wbi_sign:
                            await self._refresh_wbi(force=True)
                        backoff = min(30, 4 * attempt + random.choice([1, 2, 4]))
                        self.on_log(
                            "warn",
                            f"风控 {code} {payload.get('message','')} · {backoff:.0f}s 后重试 ({attempt}/{retries})",
                        )
                        await asyncio.sleep(backoff)
                        last_exc = BiliError(str(payload.get("message") or code), code, True)
                        continue
            return resp

        raise last_exc or BiliError("请求失败")

    async def request_json(self, method: str, url: str, **kwargs) -> dict[str, Any]:
        resp = await self.request(method, url, **kwargs)
        try:
            payload = resp.json()
        except Exception as exc:
            raise BiliError(f"非 JSON 响应 HTTP {resp.status_code}") from exc
        if not isinstance(payload, dict):
            raise BiliError("响应格式异常")
        return payload

    async def get_json(self, url: str, params: dict[str, Any] | None = None, wbi: bool = True, signed: bool = True, **kwargs) -> dict[str, Any]:
        return await self.request_json("GET", url, params=params, wbi_sign=wbi and signed, **kwargs)

    async def get_bytes(self, url: str, params: dict[str, Any] | None = None, wbi_sign: bool = False, **kwargs) -> bytes:
        resp = await self.request("GET", url, params=params, wbi_sign=wbi_sign, binary=True, **kwargs)
        return resp.content

    async def get_nav(self) -> dict[str, Any]:
        return await self.get_json("https://api.bilibili.com/x/web-interface/nav", wbi=False)

    async def qr_generate(self) -> dict[str, Any]:
        data = await self.get_json(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
            wbi=False,
        )
        if data.get("code") != 0:
            raise BiliError(data.get("message") or "二维码生成失败", data.get("code"))
        return data["data"]

    async def qr_poll(self, qrcode_key: str) -> dict[str, Any]:
        resp = await self.request(
            "GET",
            "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": qrcode_key},
            binary=True,
        )
        payload = resp.json()
        data = payload.get("data") or {}
        if data.get("code") == 0:
            for cookie in resp.cookies.jar:
                self.cookies[cookie.name] = cookie.value
            # httpx also puts set-cookie in resp.cookies
            for name, value in resp.cookies.items():
                self.cookies[name] = value
        return payload

    async def get_card(self, mid: str) -> dict[str, Any]:
        data = await self.get_json(
            "https://api.bilibili.com/x/web-interface/card",
            params={"mid": mid, "photo": "1"},
            wbi=False,
            referer=f"https://space.bilibili.com/{mid}",
        )
        self._raise_api(data, "用户卡片")
        return data.get("data") or {}

    async def get_acc_info(self, mid: str) -> dict[str, Any]:
        data = await self.get_json(
            "https://api.bilibili.com/x/space/wbi/acc/info",
            params={"mid": mid, "web_location": 1550101, **DM_IMG},
            wbi=True,
            referer=f"https://space.bilibili.com/{mid}",
        )
        if data.get("code") != 0:
            return {}
        return data.get("data") or {}

    async def get_relation_stat(self, mid: str) -> dict[str, Any]:
        data = await self.get_json(
            "https://api.bilibili.com/x/relation/stat",
            params={"vmid": mid},
            wbi=False,
        )
        self._raise_api(data, "关系统计")
        return data.get("data") or {}

    async def get_upstat(self, mid: str) -> dict[str, Any]:
        data = await self.get_json(
            "https://api.bilibili.com/x/space/upstat",
            params={"mid": mid},
            wbi=False,
            referer=f"https://space.bilibili.com/{mid}",
        )
        if data.get("code") != 0:
            return {}
        return data.get("data") or {}

    async def get_navnum(self, mid: str) -> dict[str, Any]:
        data = await self.get_json(
            "https://api.bilibili.com/x/space/navnum",
            params={"mid": mid},
            wbi=False,
        )
        if data.get("code") != 0:
            return {}
        return data.get("data") or {}

    async def iter_videos(self, mid: str, cutoff: int | None) -> AsyncIterator[dict[str, Any]]:
        pn = 1
        ps = 30
        seen: set[str] = set()
        while True:
            data = await self.get_json(
                "https://api.bilibili.com/x/space/wbi/arc/search",
                params={
                    "mid": mid,
                    "pn": pn,
                    "ps": ps,
                    "order": "pubdate",
                    "tid": 0,
                    "keyword": "",
                    "web_location": 1550101,
                    **DM_IMG,
                },
                wbi=True,
                referer=f"https://space.bilibili.com/{mid}/video",
            )
            self._raise_api(data, "投稿列表")
            vlist = pick(data, "data", "list", "vlist") or []
            if not vlist:
                break
            stop = False
            for item in vlist:
                bvid = item.get("bvid")
                created = int(item.get("created") or 0)
                if cutoff and created and created < cutoff:
                    stop = True
                    break
                if bvid and bvid not in seen:
                    seen.add(bvid)
                    yield item
            page = pick(data, "data", "page") or {}
            count = int(page.get("count") or 0)
            if stop or pn * ps >= count or len(vlist) < ps:
                break
            pn += 1

    async def get_view_detail(self, bvid: str) -> dict[str, Any]:
        """Prefer view/detail so Tags travel with View; fall back to view."""
        referer = f"https://www.bilibili.com/video/{bvid}"
        data = await self.get_json(
            "https://api.bilibili.com/x/web-interface/wbi/view/detail",
            params={"bvid": bvid},
            wbi=True,
            referer=referer,
        )
        if data.get("code") != 0:
            data = await self.get_json(
                "https://api.bilibili.com/x/web-interface/view/detail",
                params={"bvid": bvid},
                wbi=False,
                referer=referer,
            )
        if data.get("code") != 0:
            data = await self.get_json(
                "https://api.bilibili.com/x/web-interface/wbi/view",
                params={"bvid": bvid},
                wbi=True,
                referer=referer,
            )
        if data.get("code") != 0:
            data = await self.get_json(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid},
                wbi=False,
                referer=referer,
            )
        self._raise_api(data, "视频详情")
        payload = data.get("data") or {}
        if "View" in payload:
            return payload
        return {"View": payload, "Tags": payload.get("tag") or payload.get("Tags") or []}

    async def iter_comments(
        self,
        oid: str,
        ctype: int,
        *,
        max_pages: int = 0,
        include_sub: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        offset = ""
        page = 0
        seen: set[str] = set()
        while True:
            page += 1
            if max_pages and page > max_pages:
                break
            params = {
                "oid": oid,
                "type": ctype,
                "mode": 3,
                "plat": 1,
                "pagination_str": json.dumps({"offset": offset}, separators=(",", ":")),
                "seek_rpid": "",
                "web_location": "1315875",
            }
            data = await self.get_json(
                "https://api.bilibili.com/x/v2/reply/wbi/main",
                params=params,
                wbi=True,
            )
            if data.get("code") != 0:
                data = await self.get_json(
                    "https://api.bilibili.com/x/v2/reply/main",
                    params={"oid": oid, "type": ctype, "mode": 3, "next": page - 1, "ps": 20},
                    wbi=False,
                )
            if data.get("code") != 0:
                self.on_log("warn", f"评论接口 {oid} 失败：{data.get('message')}")
                break
            payload = data.get("data") or {}
            replies = (payload.get("replies") or []) + (payload.get("top_replies") or [])
            if payload.get("top") and payload["top"] not in replies:
                replies = [payload["top"]] + replies
            if not replies:
                break
            for reply in replies:
                async for row in self._walk_reply(oid, ctype, reply, include_sub, seen):
                    yield row
            cursor = payload.get("cursor") or {}
            if cursor.get("is_end"):
                break
            pagination = cursor.get("pagination_reply") or {}
            nxt = pagination.get("next_offset") or ""
            if not nxt or nxt == offset:
                break
            offset = nxt

    async def _walk_reply(
        self,
        oid: str,
        ctype: int,
        reply: dict[str, Any],
        include_sub: bool,
        seen: set[str],
    ) -> AsyncIterator[dict[str, Any]]:
        row = self._comment_row(reply, oid, ctype, parent="")
        rpid = row.get("rpid")
        if rpid and rpid not in seen:
            seen.add(rpid)
            yield row
        children = reply.get("replies") or []
        for child in children:
            crow = self._comment_row(child, oid, ctype, parent=rpid or "", root=rpid or "")
            if crow.get("rpid") and crow["rpid"] not in seen:
                seen.add(crow["rpid"])
                yield crow
        rcount = int(reply.get("rcount") or 0)
        if include_sub and rcount > len(children) and rpid:
            pn = 1
            while True:
                data = await self.get_json(
                    "https://api.bilibili.com/x/v2/reply/reply",
                    params={"oid": oid, "type": ctype, "root": rpid, "ps": 20, "pn": pn},
                    wbi=False,
                )
                if data.get("code") != 0:
                    break
                batch = pick(data, "data", "replies") or []
                if not batch:
                    break
                for child in batch:
                    crow = self._comment_row(child, oid, ctype, parent=rpid, root=rpid)
                    if crow.get("rpid") and crow["rpid"] not in seen:
                        seen.add(crow["rpid"])
                        yield crow
                page = pick(data, "data", "page") or {}
                count = int(page.get("count") or 0)
                if pn * 20 >= count:
                    break
                pn += 1

    def _comment_row(
        self,
        reply: dict[str, Any],
        oid: str,
        ctype: int,
        parent: str,
        root: str = "",
    ) -> dict[str, Any]:
        member = reply.get("member") or {}
        content = reply.get("content") or {}
        api_root = str(reply.get("root") or "")
        api_parent = str(reply.get("parent") or "")
        resolved_root = root or (api_root if api_root not in {"", "0"} else "")
        resolved_parent = api_parent if api_parent not in {"", "0"} else parent
        return {
            "oid": str(oid),
            "type": ctype,
            "rpid": str(reply.get("rpid") or ""),
            "root_rpid": resolved_root or "0",
            "parent_rpid": str(resolved_parent or "0"),
            "hierarchy_level": "root" if not resolved_root else "reply",
            "mid": str(member.get("mid") or ""),
            "uname": member.get("uname") or "",
            "level": pick(member, "level_info", "current_level"),
            "sex": member.get("sex") or "",
            "message": content.get("message") or "",
            "like": reply.get("like") or 0,
            "rcount": reply.get("rcount") or 0,
            "ctime": reply.get("ctime") or 0,
            "ip_location": reply.get("reply_control", {}).get("location") or "",
        }

    async def get_related(self, bvid: str, limit: int = 20) -> list[dict[str, Any]]:
        """Algorithmic recommendation neighbours; the edges of the snowball graph."""
        data = await self.get_json(
            "https://api.bilibili.com/x/web-interface/archive/related",
            params={"bvid": bvid},
            wbi=False,
            referer=f"https://www.bilibili.com/video/{bvid}",
        )
        if data.get("code") != 0:
            self.on_log("warn", f"相关推荐 {bvid} 失败：{data.get('message')}")
            return []
        raw = data.get("data")
        if isinstance(raw, dict):
            items = raw.get("related") or raw.get("archives") or raw.get("list") or []
        else:
            items = raw or []
        if not isinstance(items, list):
            items = []
        out: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("bvid"):
                continue
            out.append(item)
            if limit and len(out) >= limit:
                break
        return out

    async def get_danmaku(self, cid: int, duration: int, aid: int | None = None) -> list[dict[str, Any]]:
        segs = max(1, (int(duration) + 359) // 360)
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for idx in range(1, segs + 1):
            params = {"type": 1, "oid": cid, "segment_index": idx}
            if aid:
                params["pid"] = aid
            try:
                raw = await self.get_bytes(
                    "https://api.bilibili.com/x/v2/dm/wbi/web/seg.so",
                    params=params,
                    wbi_sign=True,
                    referer="https://www.bilibili.com",
                )
                batch = parse_seg_protobuf(raw)
            except Exception:
                batch = []
            if not batch and idx == 1:
                try:
                    raw = await self.get_bytes(f"https://comment.bilibili.com/{cid}.xml")
                    batch = parse_xml_danmaku(raw)
                except Exception:
                    batch = []
            if not batch:
                if idx == 1:
                    continue
                break
            for item in batch:
                key = str(item.get("id_str") or item.get("id") or f"{item.get('progress_ms')}-{item.get('content')}")
                if key in seen:
                    continue
                seen.add(key)
                collected.append(item)
        return collected

    async def iter_dynamics(self, mid: str, cutoff: int | None) -> AsyncIterator[dict[str, Any]]:
        offset = ""
        seen: set[str] = set()
        while True:
            params = {
                "host_mid": mid,
                "offset": offset,
                "timezone_offset": -480,
                "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,forwardListHidden,decorationCard,commentsNum,onlyfansAssetsV2,combination,htmlNewStyle,uturnLastComment",
                "platform": "web",
                "web_location": 333.999,
                **DM_IMG,
            }
            data = await self.get_json(
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
                params=params,
                wbi=True,
                referer=f"https://space.bilibili.com/{mid}/dynamic",
            )
            if data.get("code") != 0:
                raise BiliError(data.get("message") or "动态列表失败", data.get("code"), True)
            payload = data.get("data") or {}
            items = payload.get("items") or []
            if not items:
                break
            stop = False
            for item in items:
                dyn_id = str(item.get("id_str") or "")
                pub_ts = int(pick(item, "modules", "module_author", "pub_ts") or 0)
                if cutoff and pub_ts and pub_ts < cutoff:
                    stop = True
                    break
                if dyn_id and dyn_id not in seen:
                    seen.add(dyn_id)
                    yield item
            if stop or not payload.get("has_more"):
                break
            offset = payload.get("offset") or ""
            if not offset:
                break

    async def get_playurl(self, bvid: str, cid: int, qn: int = 64) -> dict[str, Any]:
        data = await self.get_json(
            "https://api.bilibili.com/x/player/wbi/playurl",
            params={
                "bvid": bvid,
                "cid": cid,
                "qn": qn,
                "fnval": 4048,
                "fnver": 0,
                "fourk": 1,
                "try_look": 1,
                "platform": "pc",
            },
            wbi=True,
            referer=f"https://www.bilibili.com/video/{bvid}",
        )
        if data.get("code") != 0:
            raise BiliError(data.get("message") or "playurl 失败", data.get("code"), True)
        return data.get("data") or {}

    async def get_player(self, bvid: str, cid: int, aid: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"bvid": bvid, "cid": cid, "isGaiaAvoided": "false", "web_location": 1315873}
        if aid:
            params["aid"] = aid
        data = await self.get_json(
            "https://api.bilibili.com/x/player/wbi/v2",
            params=params,
            wbi=True,
            referer=f"https://www.bilibili.com/video/{bvid}",
        )
        if data.get("code") != 0:
            return {}
        return data.get("data") or {}

    async def get_ai_conclusion(self, bvid: str, cid: int, up_mid: str, aid: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"bvid": bvid, "cid": cid, "up_mid": up_mid}
        if aid:
            params["aid"] = aid
        data = await self.get_json(
            "https://api.bilibili.com/x/web-interface/view/conclusion/get",
            params=params,
            wbi=True,
            referer=f"https://www.bilibili.com/video/{bvid}",
        )
        if data.get("code") != 0:
            return {}
        return data.get("data") or {}

    async def download_file(
        self,
        url: str,
        dest: str | Path,
        referer: str = "https://www.bilibili.com",
        *,
        should_cancel: Callable[[], bool] | None = None,
        on_progress: Callable[[int, int | None], None] | None = None,
    ) -> int:
        """Download to a .part file, resume with Range, then atomically publish."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size > 0:
            return dest.stat().st_size
        part = dest.with_name(dest.name + ".part")
        last_error: Exception | None = None
        for attempt in range(1, max(2, int(self.settings.max_retries)) + 1):
            offset = part.stat().st_size if part.exists() else 0
            headers = {
                "Referer": referer,
                "User-Agent": UA,
                "Origin": "https://www.bilibili.com",
            }
            if offset:
                headers["Range"] = f"bytes={offset}-"
            try:
                async with self._client.stream("GET", url, headers=headers, cookies=self.cookies) as resp:
                    if resp.status_code == 416 and offset:
                        os.replace(part, dest)
                        return dest.stat().st_size
                    resp.raise_for_status()
                    append = offset > 0 and resp.status_code == 206
                    if offset and not append:
                        offset = 0
                    length = int(resp.headers.get("Content-Length") or 0)
                    expected = offset + length if length else None
                    mode = "ab" if append else "wb"
                    downloaded = offset
                    with part.open(mode) as fh:
                        async for chunk in resp.aiter_bytes(1024 * 512):
                            if should_cancel and should_cancel():
                                raise asyncio.CancelledError()
                            fh.write(chunk)
                            downloaded += len(chunk)
                            if on_progress:
                                on_progress(downloaded, expected)
                        fh.flush()
                        os.fsync(fh.fileno())
                    if expected and downloaded < expected:
                        raise BiliError(f"下载不完整：{downloaded}/{expected}", retryable=True)
                    os.replace(part, dest)
                    return downloaded
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, OSError, BiliError) as exc:
                last_error = exc
                if attempt < max(2, int(self.settings.max_retries)):
                    await asyncio.sleep(min(10, 2**attempt) + jitter(0.2, 0.8))
        raise BiliError(f"下载失败（已保留断点）：{last_error}", retryable=True)

    def _raise_api(self, data: dict[str, Any], what: str) -> None:
        code = data.get("code")
        if code != 0:
            raise BiliError(f"{what}失败：{data.get('message') or code}", code, code in RISK_CODES)
