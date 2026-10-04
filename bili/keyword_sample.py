"""Keyword sampling: search → gate → collect (transcript / comments / danmaku).

Unlike academic snowball (recommendation graph) or archive-by-UP (full space),
this lane discovers videos via Bilibili typed search, applies hard title / date
filters plus engagement gates, then reuses the normal video crawl pipeline.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from bili.academic import CIRCUIT_LIMIT, extract_tags, split_terms
from bili.client import RISK_CODES, BiliClient, BiliError
from bili.corpus import Corpus, FrontierItem
from bili.crawler import Crawler, JobConfig
from bili.export import account_dir
from bili.paths import LIBRARY_DIR
from bili.settings import AppSettings
from bili.util import parse_date_boundary, pick, strip_html

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
    crawl_comments: bool = True
    comment_with_replies: bool = True  # False = top-level list only
    comment_max_pages: int = 0  # 0 = all pages (subject to settings)
    crawl_danmaku: bool = False
    transcribe_mode: str = "official_then_whisper"
    media_mode: str = "audio"
    media_keep: str = "delete_after_text"
    ocr_enabled: bool = False
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


def normalize_search_hit(row: dict[str, Any], *, keyword: str = "") -> dict[str, Any]:
    """Flatten a search-result row into gate-friendly fields."""
    title = strip_html(str(row.get("title") or ""))
    desc = strip_html(str(row.get("description") or row.get("desc") or ""))
    bvid = str(row.get("bvid") or "")
    aid = row.get("aid") or row.get("id")
    mid = str(row.get("mid") or "")
    author = str(row.get("author") or "")
    pubdate = int(row.get("pubdate") or 0)
    # Search payload field names vary across API revisions.
    views = int(row.get("play") or row.get("view") or 0)
    likes = int(row.get("like") or row.get("likes") or 0)
    danmaku = int(row.get("video_review") or row.get("danmaku") or 0)
    replies = int(row.get("review") or row.get("reply") or row.get("comment") or 0)
    favorites = int(row.get("favorites") or row.get("stow") or 0)
    duration = int(row.get("duration") or 0)
    if isinstance(row.get("duration"), str) and ":" in str(row.get("duration")):
        # Some payloads return "mm:ss" / "hh:mm:ss".
        parts = [int(p) for p in str(row["duration"]).split(":") if str(p).isdigit()]
        if len(parts) == 2:
            duration = parts[0] * 60 + parts[1]
        elif len(parts) == 3:
            duration = parts[0] * 3600 + parts[1] * 60 + parts[2]
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

    async def run(
        self,
        config: KeywordSampleConfig,
        on_progress: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
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

        client = BiliClient(self.settings, on_log=self.on_log)
        progress: dict[str, Any] = {
            "stage": "search",
            "kind": "keyword",
            "videos_done": 0,
            "passed": 0,
            "pruned": 0,
            "searched": 0,
            "candidates": 0,
            "current": "",
            "max_nodes": config.max_nodes,
            "keywords": keywords,
        }

        async def emit() -> None:
            stats = self.corpus.frontier_stats(config.job_id)
            progress["frontier"] = stats
            progress["nodes_done"] = int(stats.get("done") or 0)
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
                "min_views": config.min_views,
                "min_likes": config.min_likes,
                "min_danmaku": config.min_danmaku,
                "min_replies": config.min_replies,
                "min_engagement": config.min_engagement,
                "category_allow": config.category_allow,
                "category_deny": config.category_deny,
                "crawl_comments": config.crawl_comments,
                "comment_with_replies": config.comment_with_replies,
                "crawl_danmaku": config.crawl_danmaku,
            },
            label="关键词采样",
        )
        if config.resume:
            restored = self.corpus.requeue_visiting(config.job_id)
            if restored:
                self.on_log("info", f"已把中断的 {restored} 个节点放回队列")

        consecutive_risk = 0
        try:
            await client.bootstrap()
            stats0 = self.corpus.frontier_stats(config.job_id)
            if not stats0.get("pending") and not stats0.get("done") and not stats0.get("pruned"):
                candidates = await self._search_candidates(client, config, keywords, begin, end, progress, emit)
                progress["candidates"] = len(candidates)
                if not candidates:
                    self.on_log("warn", "无候选可入队，本轮结束。")
                    progress["stage"] = "done"
                    await emit()
                    return progress
                items = [
                    FrontierItem(bvid=row["bvid"], depth=0, seed_bvid=row["bvid"])
                    for row in candidates
                ]
                added = self.corpus.enqueue(config.job_id, items)
                self.on_log("ok", f"搜索去重后候选 {len(candidates)} · 新入队 {added}")
                # Persist listing-side prune reasons already recorded during search.
                await emit()
            else:
                self.on_log("info", "续跑：跳过搜索阶段，继续处理队列")

            while not self._stop():
                stats = self.corpus.frontier_stats(config.job_id)
                done = int(stats.get("done") or 0)
                if done >= int(config.max_nodes):
                    self.on_log("warn", f"已达采集上限 {config.max_nodes}，停止")
                    break
                item = self.corpus.next_pending(config.job_id)
                if item is None:
                    break
                progress["stage"] = f"video {item.bvid}"
                progress["current"] = item.bvid
                await emit()
                try:
                    collected = await self._visit(client, config, item)
                    consecutive_risk = 0
                    if collected:
                        progress["videos_done"] = int(progress.get("videos_done") or 0) + 1
                        progress["passed"] = int(progress.get("passed") or 0) + 1
                    else:
                        progress["pruned"] = int(progress.get("pruned") or 0) + 1
                except Exception as exc:
                    self.corpus.mark_frontier(config.job_id, item.bvid, "error", str(exc))
                    risk = isinstance(exc, BiliError) and (exc.retryable or exc.code in RISK_CODES)
                    if risk:
                        consecutive_risk += 1
                        self.on_log(
                            "error",
                            f"{item.bvid} 风控：{exc}（连续 {consecutive_risk}/{CIRCUIT_LIMIT}）",
                        )
                        if consecutive_risk >= CIRCUIT_LIMIT:
                            self.on_log("error", "连续风控，停止本轮。队列已保存，稍后用同一任务续跑。")
                            self.cancelled = True
                            break
                    else:
                        consecutive_risk = 0
                        self.on_log("error", f"{item.bvid} 失败：{exc}")
                await emit()

            progress["stage"] = "cancelled" if self.cancelled else "done"
            await emit()
            return progress
        finally:
            status = "cancelled" if self.cancelled else "done"
            self.corpus.finish_run(config.job_id, status)
            await client.close()

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
        # Hard stop search early once we have enough strong candidates.
        target = max(int(config.max_nodes) * 3, int(config.max_nodes))

        for keyword in keywords:
            if self._stop() or len(kept) >= target:
                break
            self.on_log("info", f"搜索「{keyword}」…")
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
                    hit = normalize_search_hit(raw, keyword=keyword)
                    bvid = hit["bvid"]
                    progress["searched"] = int(progress.get("searched") or 0) + 1
                    page_hits += 1
                    if not bvid or bvid in seen:
                        continue
                    seen.add(bvid)
                    # Listing-stage soft gate: title + date + cheap stats.
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
                # One keyword failing must not abort the whole multi-keyword job.
                self.on_log("error", f"搜索「{keyword}」失败，跳过该词继续：{exc}")
                await asyncio.sleep(1.2)
                continue
            self.on_log("ok", f"「{keyword}」搜索结束 · 本词处理 {page_hits} 条 · 累计候选 {len(kept)} / 已见 {len(seen)}")
            await asyncio.sleep(0.4)

        if must:
            self.on_log(
                "info",
                f"标题硬约束已启用（{'全部' if config.title_match_mode == 'all' else '任一'}命中）："
                + "、".join(must),
            )
        if not kept:
            self.on_log("warn", "搜索结束但没有通过标题/时间/门禁的候选。请放宽标题词、日期或互动门槛后重试。")
        return kept[: max(0, target)]

    async def _visit(self, client: BiliClient, config: KeywordSampleConfig, item: FrontierItem) -> bool:
        detail = await client.get_view_detail(item.bvid)
        view = detail.get("View") if isinstance(detail.get("View"), dict) else {}
        if not view.get("bvid") and not view.get("aid"):
            self.corpus.record_gate_decision(config.job_id, item.bvid, False, "no_view", {}, 0)
            self.corpus.mark_frontier(config.job_id, item.bvid, "error", "no_view")
            return False
        view["bvid"] = view.get("bvid") or item.bvid
        tags = extract_tags(detail)
        stat = view.get("stat") if isinstance(view.get("stat"), dict) else {}
        hit = {
            "bvid": item.bvid,
            "title": view.get("title") or "",
            "description": view.get("desc") or "",
            "tname": view.get("tname") or "",
            "tid": view.get("tid") or "",
            "pubdate": view.get("pubdate") or 0,
            "views": pick(view, "stat", "view") or 0,
            "likes": pick(view, "stat", "like") or 0,
            "danmaku": pick(view, "stat", "danmaku") or 0,
            "replies": pick(view, "stat", "reply") or 0,
        }
        passed, reason, checks = evaluate_keyword_gate(hit, config, tags=tags, stage="detail")
        self.corpus.record_gate_decision(config.job_id, item.bvid, passed, reason, checks, 0)
        owner = view.get("owner") if isinstance(view.get("owner"), dict) else {}
        mid = str(owner.get("mid") or "")
        name = owner.get("name") or mid or "unknown"
        if not passed:
            self.corpus.upsert_video(
                {
                    "bvid": item.bvid,
                    "aid": view.get("aid"),
                    "mid": mid,
                    "author_name": name,
                    "title": view.get("title"),
                    "description": view.get("desc"),
                    "tid": view.get("tid"),
                    "tname": view.get("tname"),
                    "pubdate": view.get("pubdate"),
                    "duration": view.get("duration"),
                    "view_count": stat.get("view"),
                    "like_count": stat.get("like"),
                    "reply_count": stat.get("reply"),
                    "danmaku_count": stat.get("danmaku"),
                    "tags": tags,
                    "discovery": "keyword",
                    "depth": 0,
                    "parent_bvid": None,
                    "seed_bvid": item.seed_bvid,
                    "pass_filter": False,
                    "reject_reason": reason,
                    "run_id": config.job_id,
                    "page_url": f"https://www.bilibili.com/video/{item.bvid}",
                }
            )
            self.corpus.mark_frontier(config.job_id, item.bvid, "pruned", reason)
            self.on_log("warn", f"剪枝 {item.bvid} · {reason}")
            return False

        # Temporarily override global comment settings for this crawl.
        prev_pages = self.settings.comment_max_pages
        prev_sub = self.settings.include_sub_replies
        self.settings.comment_max_pages = int(config.comment_max_pages or 0)
        self.settings.include_sub_replies = bool(config.comment_with_replies)
        try:
            acc_dir = account_dir(LIBRARY_DIR, mid or "unknown", name)
            job = JobConfig(
                uids=[mid] if mid else [],
                time_range="all",
                crawl_profile=False,
                crawl_videos=True,
                crawl_dynamics=False,
                crawl_comments=bool(config.crawl_comments),
                crawl_danmaku=bool(config.crawl_danmaku),
                media_mode=config.media_mode or "audio",
                transcribe_mode=config.transcribe_mode or "official_then_whisper",
                media_keep=config.media_keep,
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
            await self.crawler._crawl_video(
                client,
                job,
                mid,
                acc_dir,
                {"bvid": item.bvid, "title": view.get("title"), "_detail": detail},
            )
        finally:
            self.settings.comment_max_pages = prev_pages
            self.settings.include_sub_replies = prev_sub

        self.corpus.set_video_topology(
            item.bvid,
            discovery="keyword",
            depth=0,
            parent_bvid=None,
            seed_bvid=item.seed_bvid,
            pass_filter=True,
            reject_reason="",
            run_id=config.job_id,
        )
        self.corpus.mark_frontier(config.job_id, item.bvid, "done")
        self.on_log("ok", f"采集完成 {item.bvid} · {view.get('title') or ''}")
        return True
