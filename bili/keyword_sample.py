"""Keyword sampling: site-wide search → video list export and/or dynamics crawl.

Discovery only: does not auto-start Whisper. Paste ``bvids.txt`` into
「按视频号采集」 after screening titles. Dynamics use the same keywords and
date window, with a separate engagement gate.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from bili.academic import CIRCUIT_LIMIT, split_terms
from bili.client import RISK_CODES, BiliClient, BiliError
from bili.corpus import Corpus
from bili.crawler import Crawler, JobConfig, extract_dynamic
from bili.export import account_dir, write_keyword_catalog
from bili.paths import EXPORT_DIR, LIBRARY_DIR
from bili.settings import AppSettings
from bili.util import jitter, parse_date_boundary, pick, strip_html, ts_iso

SEARCH_ORDERS = {"totalrank", "pubdate", "click", "dm", "stow", "scores"}
DURATION_CHOICES = {0, 1, 2, 3, 4}
DEFAULT_DENY = "游戏,动画,番剧,国创,音乐,舞蹈,影视,娱乐,鬼畜,运动,汽车,时尚,美食,搞笑"


@dataclass
class KeywordSampleConfig:
    job_id: str
    keywords_text: str = ""
    keywords: list[str] = field(default_factory=list)
    title_must_terms: str = ""
    title_match_mode: str = "any"  # any | all
    date_from: str = ""
    date_to: str = ""
    order: str = "pubdate"
    duration: int = 0
    tids: int = 0
    max_pages_per_keyword: int = 30
    max_nodes: int = 200
    min_views: int = 0
    min_likes: int = 0
    min_danmaku: int = 0
    min_replies: int = 0
    min_engagement: float = 0.0
    category_allow: str = ""
    category_deny: str = DEFAULT_DENY
    export_video_list: bool = True
    crawl_dynamics: bool = True
    dyn_min_likes: int = 0
    dyn_min_comments: int = 0
    dyn_min_forwards: int = 0
    crawl_comments: bool = True  # dynamics comments when crawl_dynamics
    comment_with_replies: bool = True
    comment_max_pages: int = 0
    crawl_danmaku: bool = False
    transcribe_mode: str = "none"
    media_mode: str = "link"
    media_keep: str = "keep"
    ocr_enabled: bool = True
    resume: bool = True
    compute_backend: str = "local"
    rclone_remote: str = ""
    rclone_root: str = "BiliArchiver"


def parse_keywords(raw: str) -> list[str]:
    """Split multi-line / comma keyword lists; drop empties; keep order."""
    parts = re.split(r"[\n,，;；]+", raw or "")
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        term = part.strip()
        if not term or term in seen:
            continue
        seen.add(term)
        out.append(term)
    return out


def _as_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return default


def _as_duration_seconds(value: Any) -> int:
    """Search results use either seconds or ``mm:ss`` / ``hh:mm:ss``."""
    if isinstance(value, str) and ":" in value:
        parts = [int(part) for part in value.split(":") if part.strip().isdigit()]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return 0
    return _as_int(value)


def normalize_search_hit(row: dict[str, Any], *, keyword: str = "") -> dict[str, Any]:
    """Flatten a search-result row into gate-friendly fields."""
    title = strip_html(str(row.get("title") or ""))
    desc = strip_html(str(row.get("description") or row.get("desc") or ""))
    bvid = str(row.get("bvid") or "")
    aid = row.get("aid") or row.get("id")
    mid = str(row.get("mid") or "")
    author = str(row.get("author") or "")
    pubdate = _as_int(row.get("pubdate"))
    # Search payload field names vary across API revisions.
    views = _as_int(row.get("play") or row.get("view"))
    likes = _as_int(row.get("like") or row.get("likes"))
    danmaku = _as_int(row.get("video_review") or row.get("danmaku"))
    replies = _as_int(row.get("review") or row.get("reply") or row.get("comment"))
    favorites = _as_int(row.get("favorites") or row.get("stow"))
    duration = _as_duration_seconds(row.get("duration"))
    tname = str(row.get("typename") or row.get("tname") or "")
    tid = str(row.get("typeid") or row.get("tid") or "")
    return {
        "bvid": bvid,
        "aid": aid,
        "mid": mid,
        "author_name": author,
        "title": title,
        "description": desc,
        "pubdate": pubdate,
        "views": views,
        "likes": likes,
        "danmaku": danmaku,
        "replies": replies,
        "favorites": favorites,
        "duration": duration,
        "tname": tname,
        "tid": tid,
        "keyword": keyword,
        "raw": row,
    }


def title_terms_match(title: str, terms: list[str], mode: str = "any") -> bool:
    if not terms:
        return True
    hay = title or ""
    hits = [term for term in terms if term and term in hay]
    if mode == "all":
        return len(hits) == len([t for t in terms if t])
    return bool(hits)


def evaluate_keyword_gate(
    hit: dict[str, Any],
    config: KeywordSampleConfig,
    *,
    tags: list[str] | None = None,
    stage: str = "listing",
) -> tuple[bool, str, dict[str, Any]]:
    """AND gate for keyword-sample candidates.

    ``stage`` is ``listing`` (search row) or ``detail`` (view API). Soft listing
    stats may under-count; detail stage re-checks with authoritative numbers.
    """
    title = str(hit.get("title") or "")
    desc = str(hit.get("description") or hit.get("desc") or "")
    tname = str(hit.get("tname") or "")
    tid = str(hit.get("tid") or "")
    views = int(hit.get("views") or hit.get("view") or 0)
    likes = int(hit.get("likes") or hit.get("like") or 0)
    danmaku = int(hit.get("danmaku") or 0)
    replies = int(hit.get("replies") or hit.get("reply") or 0)
    pubdate = int(hit.get("pubdate") or 0)
    tag_list = list(tags or [])
    begin = parse_date_boundary(config.date_from, end_of_day=False)
    end = parse_date_boundary(config.date_to, end_of_day=True)
    ratio = (replies / views) if views else 0.0
    checks = {
        "stage": stage,
        "title": title,
        "tid": tid,
        "tname": tname,
        "views": views,
        "likes": likes,
        "danmaku": danmaku,
        "replies": replies,
        "engagement_ratio": round(ratio, 6),
        "pubdate": pubdate,
        "tags": tag_list,
    }

    must = split_terms(config.title_must_terms)
    if must and not title_terms_match(title, must, config.title_match_mode or "any"):
        return False, "title_terms", checks

    if begin and pubdate and pubdate < begin:
        return False, "too_old", checks
    if end and pubdate and pubdate > end:
        return False, "too_new", checks

    allow = split_terms(config.category_allow)
    deny = split_terms(config.category_deny)
    blob = f"{tname} {tid}".lower()
    if allow and not any(term.lower() in blob for term in allow):
        return False, "category", checks
    if deny and any(term.lower() in blob for term in deny):
        return False, "category", checks

    if views < int(config.min_views or 0):
        return False, "min_views", checks
    if likes < int(config.min_likes or 0):
        return False, "min_likes", checks
    if danmaku < int(config.min_danmaku or 0):
        return False, "min_danmaku", checks

    min_replies = int(config.min_replies or 0)
    min_eng = float(config.min_engagement or 0)
    if min_replies or min_eng:
        ok_replies = bool(min_replies and replies >= min_replies)
        ok_ratio = bool(min_eng and ratio >= min_eng)
        if not (ok_replies or ok_ratio):
            return False, "engagement", checks

    return True, "pass", checks


def normalize_dynamic_search_hit(row: dict[str, Any], *, keyword: str = "") -> dict[str, Any]:
    """Flatten a twitter/dynamic search row."""
    dyn_id = str(row.get("id") or row.get("twitter_id") or row.get("dynamic_id") or "").strip()
    mid = str(row.get("mid") or row.get("uid") or "")
    uname = strip_html(str(row.get("uname") or row.get("author") or ""))
    text = strip_html(str(row.get("content") or row.get("title") or row.get("description") or ""))
    pub_ts = _as_int(row.get("ctime") or row.get("pubdate") or row.get("pub_ts"))
    likes = _as_int(row.get("like") or row.get("likes"))
    comments = _as_int(row.get("comment") or row.get("review") or row.get("replies"))
    forwards = _as_int(row.get("retweet") or row.get("repost") or row.get("forward") or row.get("share"))
    pics: list[str] = []
    raw_pic = row.get("twitter_pic") or row.get("cover") or ""
    if isinstance(raw_pic, str) and raw_pic.strip():
        pics = [part.strip() for part in raw_pic.split(",") if part.strip()]
    elif isinstance(raw_pic, list):
        pics = [str(part) for part in raw_pic if part]
    return {
        "dyn_id": dyn_id,
        "mid": mid,
        "author_name": uname,
        "text": text,
        "pub_ts": pub_ts,
        "likes": likes,
        "comments": comments,
        "forwards": forwards,
        "pictures": pics,
        "keyword": keyword,
        "jump_url": f"https://t.bilibili.com/{dyn_id}" if dyn_id else "",
        "raw": row,
    }


def evaluate_dynamic_gate(
    hit: dict[str, Any],
    config: KeywordSampleConfig,
) -> tuple[bool, str, dict[str, Any]]:
    """Time window + separate dynamic engagement. No video title/category gates."""
    pub_ts = int(hit.get("pub_ts") or 0)
    likes = int(hit.get("likes") or hit.get("like") or 0)
    comments = int(hit.get("comments") or hit.get("comment") or 0)
    forwards = int(hit.get("forwards") or hit.get("forward") or 0)
    begin = parse_date_boundary(config.date_from, end_of_day=False)
    end = parse_date_boundary(config.date_to, end_of_day=True)
    checks = {
        "dyn_id": hit.get("dyn_id"),
        "pub_ts": pub_ts,
        "likes": likes,
        "comments": comments,
        "forwards": forwards,
        "text": str(hit.get("text") or "")[:80],
    }
    if begin and pub_ts and pub_ts < begin:
        return False, "too_old", checks
    if end and pub_ts and pub_ts > end:
        return False, "too_new", checks
    if likes < int(config.dyn_min_likes or 0):
        return False, "dyn_min_likes", checks
    if comments < int(config.dyn_min_comments or 0):
        return False, "dyn_min_comments", checks
    if forwards < int(config.dyn_min_forwards or 0):
        return False, "dyn_min_forwards", checks
    return True, "pass", checks


def _catalog_row(hit: dict[str, Any]) -> dict[str, Any]:
    pubdate = int(hit.get("pubdate") or 0)
    return {
        "bvid": hit.get("bvid") or "",
        "title": hit.get("title") or "",
        "author_name": hit.get("author_name") or "",
        "mid": hit.get("mid") or "",
        "pubdate": pubdate,
        "pubdate_iso": ts_iso(pubdate)[:10] if pubdate else "",
        "duration": hit.get("duration") or 0,
        "views": hit.get("views") or 0,
        "likes": hit.get("likes") or 0,
        "replies": hit.get("replies") or 0,
        "danmaku": hit.get("danmaku") or 0,
        "tname": hit.get("tname") or "",
        "keyword": hit.get("keyword") or "",
        "page_url": f"https://www.bilibili.com/video/{hit.get('bvid') or ''}",
        "description_short": str(hit.get("description") or "").replace("\n", " ")[:160],
    }


class KeywordSampler:
    def __init__(
        self,
        settings: AppSettings,
        store: Any,
        corpus: Corpus,
        crawler: Crawler,
        on_log: Callable[[str, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.corpus = corpus
        self.crawler = crawler
        self.crawler.corpus = corpus
        self.on_log = on_log or (lambda *_a, **_k: None)
        self.should_cancel = should_cancel or (lambda: False)
        self.cancelled = False

    def _stop(self) -> bool:
        if self.should_cancel():
            self.cancelled = True
        return self.cancelled or self.crawler._is_cancelled()

    def _export_dir(self, job_id: str) -> Path:
        folder = EXPORT_DIR / f"keyword_{job_id}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    async def run(
        self,
        config: KeywordSampleConfig,
        on_progress: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        if not config.export_video_list and not config.crawl_dynamics:
            raise ValueError("请至少勾选「导出视频列表」或「采集动态」")
        keywords = list(config.keywords or [])
        if not keywords:
            keywords = parse_keywords(config.keywords_text)
        if not keywords:
            raise ValueError("请至少填写一个搜索关键词")
        if config.order not in SEARCH_ORDERS:
            raise ValueError(f"排序无效：{config.order}")
        if int(config.duration) not in DURATION_CHOICES:
            raise ValueError("时长筛选项无效")
        begin = parse_date_boundary(config.date_from, end_of_day=False)
        end = parse_date_boundary(config.date_to, end_of_day=True)
        if config.date_from and begin is None:
            raise ValueError("开始日期格式应为 YYYY-MM-DD")
        if config.date_to and end is None:
            raise ValueError("结束日期格式应为 YYYY-MM-DD")
        if begin and end and begin > end:
            raise ValueError("开始日期不能晚于结束日期")

        out_dir = self._export_dir(config.job_id)
        client = BiliClient(self.settings, on_log=self.on_log)
        progress: dict[str, Any] = {
            "stage": "search",
            "kind": "keyword",
            "videos_done": 0,
            "passed": 0,
            "pruned": 0,
            "searched": 0,
            "candidates": 0,
            "dynamics_done": 0,
            "dyn_pruned": 0,
            "current": "",
            "max_nodes": config.max_nodes,
            "keywords": keywords,
            "keyword_export": str(out_dir),
        }

        async def emit() -> None:
            stats = self.corpus.frontier_stats(config.job_id)
            progress["frontier"] = stats
            progress["nodes_done"] = int(progress.get("videos_done") or 0) + int(progress.get("dynamics_done") or 0)
            if on_progress:
                maybe = on_progress(dict(progress))
                if hasattr(maybe, "__await__"):
                    await maybe

        self.corpus.start_run(
            config.job_id,
            "keyword",
            {
                "keywords": keywords,
                "title_must_terms": config.title_must_terms,
                "title_match_mode": config.title_match_mode,
                "date_from": config.date_from,
                "date_to": config.date_to,
                "order": config.order,
                "max_nodes": config.max_nodes,
                "export_video_list": config.export_video_list,
                "crawl_dynamics": config.crawl_dynamics,
                "dyn_min_likes": config.dyn_min_likes,
                "dyn_min_comments": config.dyn_min_comments,
                "dyn_min_forwards": config.dyn_min_forwards,
            },
            label="关键词采样",
        )
        catalog: list[dict[str, Any]] = []
        dyn_catalog: list[dict[str, Any]] = []
        try:
            await client.bootstrap()
            if config.export_video_list:
                catalog = self._load_video_catalog(out_dir)
                if catalog and config.resume:
                    self.on_log("info", f"续跑：复用已导出视频列表 {len(catalog)} 条")
                else:
                    hits = await self._search_candidates(client, config, keywords, begin, end, progress, emit)
                    catalog = [_catalog_row(hit) for hit in hits]
                    self._persist_video_listings(config, catalog)
                write_keyword_catalog(out_dir, config.job_id, keywords=keywords, videos=catalog, dynamics=dyn_catalog)
                progress["candidates"] = len(catalog)
                progress["passed"] = len(catalog)
                progress["videos_done"] = len(catalog)
                self.on_log("ok", f"视频列表已导出 {len(catalog)} 条 · {out_dir}")
                await emit()

            if config.crawl_dynamics and not self._stop():
                dyn_catalog = await self._crawl_keyword_dynamics(
                    client, config, keywords, begin, progress, emit, out_dir
                )
                write_keyword_catalog(out_dir, config.job_id, keywords=keywords, videos=catalog, dynamics=dyn_catalog)

            progress["stage"] = "cancelled" if self.cancelled else "done"
            await emit()
            return progress
        finally:
            status = "cancelled" if self.cancelled else "done"
            self.corpus.finish_run(config.job_id, status)
            await client.close()

    def _load_video_catalog(self, out_dir: Path) -> list[dict[str, Any]]:
        path = out_dir / "videos.json"
        if not path.is_file():
            return []
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [row for row in rows if isinstance(row, dict) and row.get("bvid")] if isinstance(rows, list) else []

    def _persist_video_listings(self, config: KeywordSampleConfig, catalog: list[dict[str, Any]]) -> None:
        for row in catalog:
            bvid = str(row.get("bvid") or "")
            if not bvid:
                continue
            try:
                self.corpus.upsert_video(
                    {
                        "bvid": bvid,
                        "aid": None,
                        "mid": row.get("mid") or "",
                        "author_name": row.get("author_name") or "",
                        "title": row.get("title") or "",
                        "description": row.get("description_short") or "",
                        "tname": row.get("tname") or "",
                        "pubdate": row.get("pubdate") or 0,
                        "duration": row.get("duration") or 0,
                        "view_count": row.get("views") or 0,
                        "like_count": row.get("likes") or 0,
                        "reply_count": row.get("replies") or 0,
                        "danmaku_count": row.get("danmaku") or 0,
                        "discovery": "keyword",
                        "depth": 0,
                        "pass_filter": True,
                        "reject_reason": "",
                        "run_id": config.job_id,
                        "page_url": row.get("page_url") or f"https://www.bilibili.com/video/{bvid}",
                    }
                )
            except Exception as exc:
                self.on_log("warn", f"数据集写入 {bvid} 失败（CSV 仍已导出）：{exc}")

    async def _search_candidates(
        self,
        client: BiliClient,
        config: KeywordSampleConfig,
        keywords: list[str],
        begin: int | None,
        end: int | None,
        progress: dict[str, Any],
        emit: Callable[[], Any],
    ) -> list[dict[str, Any]]:
        seen: set[str] = set()
        kept: list[dict[str, Any]] = []
        must = split_terms(config.title_must_terms)
        target = max(1, int(config.max_nodes or 200))

        for keyword in keywords:
            if self._stop() or len(kept) >= target:
                break
            self.on_log("info", f"搜索视频「{keyword}」…")
            progress["stage"] = f"search:{keyword}"
            progress["current"] = keyword
            await emit()
            page_hits = 0
            try:
                async for raw in client.iter_search_videos(
                    keyword,
                    order=config.order,
                    duration=int(config.duration or 0),
                    tids=int(config.tids or 0),
                    pubtime_begin_s=begin,
                    pubtime_end_s=end,
                    max_pages=int(config.max_pages_per_keyword or 30),
                    should_cancel=self._stop,
                ):
                    if self._stop() or len(kept) >= target:
                        break
                    try:
                        hit = normalize_search_hit(raw, keyword=keyword)
                    except Exception as exc:
                        self.on_log("warn", f"跳过一条无法解析的搜索结果：{exc}")
                        continue
                    bvid = hit["bvid"]
                    progress["searched"] = int(progress.get("searched") or 0) + 1
                    page_hits += 1
                    if not bvid or bvid in seen:
                        continue
                    seen.add(bvid)
                    passed, reason, checks = evaluate_keyword_gate(hit, config, stage="listing")
                    if not passed:
                        self.corpus.record_gate_decision(config.job_id, bvid, False, reason, checks, 0)
                        progress["pruned"] = int(progress.get("pruned") or 0) + 1
                        continue
                    kept.append(hit)
                    if page_hits % 20 == 0:
                        progress["candidates"] = len(kept)
                        await emit()
            except Exception as exc:
                self.on_log("error", f"搜索「{keyword}」失败，跳过该词继续：{exc}")
                await asyncio.sleep(jitter(1.2, 2.4))
                continue
            self.on_log("ok", f"「{keyword}」视频搜索结束 · 本词 {page_hits} 条 · 累计候选 {len(kept)}")
            await asyncio.sleep(jitter(1.2, 2.6))

        if must:
            self.on_log(
                "info",
                f"标题硬约束已启用（{'全部' if config.title_match_mode == 'all' else '任一'}命中）："
                + "、".join(must),
            )
        if not kept:
            self.on_log("warn", "搜索结束但没有通过标题/时间/门禁的视频候选。")
        return kept[:target]

    async def _crawl_keyword_dynamics(
        self,
        client: BiliClient,
        config: KeywordSampleConfig,
        keywords: list[str],
        begin: int | None,
        progress: dict[str, Any],
        emit: Callable[[], Any],
        out_dir: Path,
    ) -> list[dict[str, Any]]:
        seen: set[str] = set()
        catalog: list[dict[str, Any]] = []
        consecutive_risk = 0
        dyn_order = "pubdate" if config.order == "pubdate" else "totalrank"
        safety_cap = 3000

        for keyword in keywords:
            if self._stop() or len(catalog) >= safety_cap:
                break
            self.on_log("info", f"搜索动态「{keyword}」…")
            progress["stage"] = f"dyn-search:{keyword}"
            progress["current"] = keyword
            await emit()
            page_hits = 0
            try:
                async for raw in client.iter_search_dynamics(
                    keyword,
                    order=dyn_order,
                    max_pages=int(config.max_pages_per_keyword or 30),
                    should_cancel=self._stop,
                ):
                    if self._stop() or len(catalog) >= safety_cap:
                        break
                    try:
                        hit = normalize_dynamic_search_hit(raw, keyword=keyword)
                    except Exception as exc:
                        self.on_log("warn", f"跳过一条无法解析的动态搜索结果：{exc}")
                        continue
                    dyn_id = str(hit.get("dyn_id") or "")
                    page_hits += 1
                    progress["searched"] = int(progress.get("searched") or 0) + 1
                    if not dyn_id or dyn_id in seen:
                        continue
                    seen.add(dyn_id)
                    pub_ts = int(hit.get("pub_ts") or 0)
                    if begin and pub_ts and pub_ts < begin and dyn_order == "pubdate":
                        self.on_log("info", f"「{keyword}」动态已早于时间窗，停止翻页")
                        break
                    passed, reason, checks = evaluate_dynamic_gate(hit, config)
                    if not passed:
                        progress["dyn_pruned"] = int(progress.get("dyn_pruned") or 0) + 1
                        progress["pruned"] = int(progress.get("pruned") or 0) + 1
                        continue
                    if config.resume and self.corpus.has_dynamic(dyn_id):
                        self.on_log("info", f"复用动态 {dyn_id}")
                        catalog.append(self._dyn_catalog_row(hit))
                        progress["dynamics_done"] = len(catalog)
                        continue
                    progress["stage"] = f"dynamic {dyn_id}"
                    progress["current"] = dyn_id
                    await emit()
                    try:
                        row = await self._collect_one_dynamic(client, config, hit)
                        consecutive_risk = 0
                        if row:
                            catalog.append(row)
                            progress["dynamics_done"] = len(catalog)
                    except Exception as exc:
                        risk = isinstance(exc, BiliError) and (exc.retryable or exc.code in RISK_CODES)
                        if risk:
                            consecutive_risk += 1
                            self.on_log(
                                "error",
                                f"动态 {dyn_id} 风控：{exc}（连续 {consecutive_risk}/{CIRCUIT_LIMIT}）",
                            )
                            if consecutive_risk >= CIRCUIT_LIMIT:
                                self.on_log("error", "连续风控，停止动态采集。稍后用同一任务续跑。")
                                self.cancelled = True
                                break
                        else:
                            consecutive_risk = 0
                            self.on_log("error", f"动态 {dyn_id} 失败：{exc}")
                    await asyncio.sleep(jitter(0.45, 1.05))
            except Exception as exc:
                self.on_log("error", f"动态搜索「{keyword}」失败，跳过该词继续：{exc}")
                await asyncio.sleep(jitter(1.2, 2.4))
                continue
            self.on_log("ok", f"「{keyword}」动态搜索结束 · 本词 {page_hits} 条 · 已采集 {len(catalog)}")
            await asyncio.sleep(jitter(1.2, 2.6))
        self.on_log("ok", f"动态采集 {len(catalog)} 条 · {out_dir}")
        return catalog

    def _dyn_catalog_row(self, hit: dict[str, Any], extracted: dict[str, Any] | None = None) -> dict[str, Any]:
        src = extracted or hit
        pub_ts = int(src.get("pub_ts") or hit.get("pub_ts") or 0)
        text = str(src.get("text") or hit.get("text") or "").replace("\n", " ")
        return {
            "dyn_id": src.get("dyn_id") or hit.get("dyn_id") or "",
            "mid": src.get("mid") or hit.get("mid") or "",
            "author_name": src.get("author_name") or hit.get("author_name") or "",
            "pub_time_iso": ts_iso(pub_ts) if pub_ts else "",
            "text": text[:500],
            "like": src.get("like") or hit.get("likes") or 0,
            "comment": src.get("comment") or hit.get("comments") or 0,
            "forward": src.get("forward") or hit.get("forwards") or 0,
            "picture_count": len(src.get("pictures") or hit.get("pictures") or []),
            "keyword": hit.get("keyword") or "",
            "jump_url": src.get("jump_url") or hit.get("jump_url") or "",
        }

    async def _collect_one_dynamic(
        self,
        client: BiliClient,
        config: KeywordSampleConfig,
        hit: dict[str, Any],
    ) -> dict[str, Any] | None:
        dyn_id = str(hit.get("dyn_id") or "")
        item = await client.get_dynamic_detail(dyn_id)
        if not item:
            self.on_log("warn", f"动态详情为空 {dyn_id}，按搜索卡片写入")
            extracted = {
                "dyn_id": dyn_id,
                "mid": hit.get("mid") or "",
                "author_name": hit.get("author_name") or "",
                "dyn_type": "search",
                "pub_ts": hit.get("pub_ts") or 0,
                "text": hit.get("text") or "",
                "pictures": hit.get("pictures") or [],
                "like": hit.get("likes") or 0,
                "comment": hit.get("comments") or 0,
                "forward": hit.get("forwards") or 0,
                "comment_id": dyn_id,
                "comment_type": 17,
                "jump_url": hit.get("jump_url") or f"https://t.bilibili.com/{dyn_id}",
            }
        else:
            extracted = extract_dynamic(item)
            if not extracted.get("dyn_id"):
                extracted["dyn_id"] = dyn_id
            extracted["mid"] = extracted.get("mid") or hit.get("mid") or str(pick(item, "modules", "module_author", "mid") or "")
            extracted["author_name"] = (
                extracted.get("author_name")
                or hit.get("author_name")
                or str(pick(item, "modules", "module_author", "name") or "")
            )
        mid = str(extracted.get("mid") or hit.get("mid") or "unknown")
        name = str(extracted.get("author_name") or hit.get("author_name") or mid)
        acc_dir = account_dir(LIBRARY_DIR, mid, name)
        job = JobConfig(
            uids=[mid] if mid else [],
            time_range="all",
            crawl_profile=False,
            crawl_videos=False,
            crawl_dynamics=True,
            crawl_comments=bool(config.crawl_comments),
            crawl_danmaku=False,
            media_mode="link",
            transcribe_mode="none",
            media_keep=config.media_keep or "keep",
            ocr_enabled=bool(config.ocr_enabled),
            resume=config.resume,
            job_id=config.job_id,
            rclone_remote=config.rclone_remote or self.settings.rclone_remote,
            rclone_root=config.rclone_root or self.settings.rclone_root,
            compute_backend=config.compute_backend,
            discovery="keyword",
            comment_max_pages=int(config.comment_max_pages or 0),
            include_sub_replies=bool(config.comment_with_replies),
        )
        await self.crawler._crawl_dynamic(client, job, mid, acc_dir, extracted)
        return self._dyn_catalog_row(hit, extracted)

