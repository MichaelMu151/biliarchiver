"""Snowball sampling branch: seed videos → related graph → gated corpus."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from bili.client import RISK_CODES, BiliClient, BiliError
from bili.corpus import Corpus, FrontierItem
from bili.crawler import Crawler, JobConfig
from bili.export import account_dir
from bili.paths import LIBRARY_DIR
from bili.settings import AppSettings
from bili.store import Store
from bili.util import cutoff_ts, parse_bvids, parse_uids, pick

BV_STRIP = re.compile(r"BV[0-9A-Za-z]{10}")
DEFAULT_DENY = "游戏,动画,番剧,国创,音乐,舞蹈,影视,娱乐,鬼畜,运动,汽车,时尚,美食"
DEFAULT_TAGS = "就业,学历,失业,薪资,找工作,文凭,体制内,内卷,大厂,职场"
CIRCUIT_LIMIT = 5


@dataclass
class AcademicConfig:
    job_id: str
    seeds_text: str = ""
    seed_bvids: list[str] = field(default_factory=list)
    seed_uids: list[str] = field(default_factory=list)
    max_depth: int = 2
    max_nodes: int = 80
    related_limit: int = 15
    min_views: int = 10000
    min_replies: int = 150
    min_engagement: float = 0.003
    category_allow: str = ""
    category_deny: str = DEFAULT_DENY
    tag_terms: str = DEFAULT_TAGS
    keyword: str = ""
    time_range: str = "1y"
    seeds_per_uid: int = 8
    crawl_comments: bool = True
    crawl_danmaku: bool = False
    transcribe_mode: str = "official_then_whisper"
    media_mode: str = "audio"
    media_keep: str = "delete_after_text"
    ocr_enabled: bool = False
    resume: bool = True
    compute_backend: str = "local"
    rclone_remote: str = ""
    rclone_root: str = "BiliArchiver"
    skip_gate: bool = False


def parse_academic_seeds(raw: str) -> tuple[list[str], list[str]]:
    bvids = parse_bvids(raw)
    remainder = BV_STRIP.sub(" ", raw or "")
    return bvids, parse_uids(remainder)


def split_terms(raw: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,，;；\n]+", raw or "") if part.strip()]


def extract_tags(detail: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for item in detail.get("Tags") or []:
        if isinstance(item, dict):
            name = str(item.get("tag_name") or item.get("name") or "").strip()
        else:
            name = str(item).strip()
        if name:
            tags.append(name)
    return tags


def evaluate_gate(
    view: dict[str, Any],
    config: AcademicConfig,
    depth: int,
    tags: list[str] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """AND of depth, category, views, engagement, tag co-occurrence, optional keyword, recency."""
    stat = view.get("stat") if isinstance(view.get("stat"), dict) else {}
    title = str(view.get("title") or "")
    desc = str(view.get("desc") or "")
    tname = str(view.get("tname") or "")
    tid = str(view.get("tid") or "")
    views = int(stat.get("view") or 0)
    replies = int(stat.get("reply") or 0)
    pubdate = int(view.get("pubdate") or 0)
    tag_list = list(tags or [])
    cutoff = cutoff_ts(config.time_range)
    ratio = (replies / views) if views else 0.0
    checks = {
        "depth": depth,
        "tid": tid,
        "tname": tname,
        "views": views,
        "replies": replies,
        "engagement_ratio": round(ratio, 6),
        "tags": tag_list,
        "pubdate": pubdate,
        "title": title,
    }
    if depth > config.max_depth:
        return False, "max_depth", checks

    allow = split_terms(config.category_allow)
    deny = split_terms(config.category_deny)
    blob = f"{tname} {tid}".lower()
    if allow and not any(term.lower() in blob for term in allow):
        return False, "category", checks
    if deny and any(term.lower() in blob for term in deny):
        return False, "category", checks

    if views < int(config.min_views or 0):
        return False, "min_views", checks

    min_replies = int(config.min_replies or 0)
    min_eng = float(config.min_engagement or 0)
    if min_replies or min_eng:
        ok_replies = min_replies and replies >= min_replies
        ok_ratio = min_eng and ratio >= min_eng
        if not (ok_replies or ok_ratio):
            return False, "engagement", checks

    needles = split_terms(config.tag_terms)
    if needles:
        haystack = " ".join(tag_list + [title, desc]).lower()
        if not any(term.lower() in haystack for term in needles):
            return False, "tags", checks

    needle = (config.keyword or "").strip()
    if needle and needle.lower() not in title.lower() and needle.lower() not in desc.lower():
        return False, "keyword", checks
    if cutoff and pubdate and pubdate < cutoff:
        return False, "too_old", checks
    return True, "pass", checks


class AcademicCrawler:
    def __init__(
        self,
        settings: AppSettings,
        store: Store,
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

    async def run(self, config: AcademicConfig, on_progress: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
        bvids = list(config.seed_bvids or [])
        uids = list(config.seed_uids or [])
        if config.seeds_text and not bvids and not uids:
            bvids, uids = parse_academic_seeds(config.seeds_text)
        client = BiliClient(self.settings, on_log=self.on_log)
        progress = {
            "stage": "bootstrap",
            "videos_done": 0,
            "passed": 0,
            "pruned": 0,
            "current": "",
            "max_nodes": config.max_nodes,
            "kind": "transcribe" if config.skip_gate else "academic",
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
            "transcribe" if config.skip_gate else "academic",
            {
                "seed_bvids": bvids,
                "seed_uids": uids,
                "max_depth": 0 if config.skip_gate else config.max_depth,
                "max_nodes": config.max_nodes,
                "min_views": config.min_views,
                "min_replies": config.min_replies,
                "min_engagement": config.min_engagement,
                "category_allow": config.category_allow,
                "category_deny": config.category_deny,
                "tag_terms": config.tag_terms,
                "keyword": config.keyword,
                "time_range": config.time_range,
                "skip_gate": config.skip_gate,
            },
            label="按视频号采集" if config.skip_gate else "学术滚雪球",
        )
        if config.resume:
            restored = self.corpus.requeue_visiting(config.job_id)
            if restored:
                self.on_log("info", f"已把中断的 {restored} 个节点放回队列")
        consecutive_risk = 0
        try:
            await client.bootstrap()
            if not self.corpus.frontier_stats(config.job_id).get("pending") and not self.corpus.frontier_stats(config.job_id).get("done"):
                seed_items = await self._expand_seeds(client, bvids, uids, config)
                self.corpus.enqueue(config.job_id, seed_items)
                self.on_log("ok", f"种子 {len(seed_items)} 条已入队")
            await emit()
            while not self._stop():
                stats = self.corpus.frontier_stats(config.job_id)
                done = int(stats.get("done") or 0)
                if done >= config.max_nodes:
                    self.on_log("warn", f"已达节点上限 {config.max_nodes}，停止扩展")
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
                        progress["videos_done"] += 1
                        progress["passed"] += 1
                    else:
                        progress["pruned"] += 1
                except Exception as exc:
                    self.corpus.mark_frontier(config.job_id, item.bvid, "error", str(exc))
                    risk = isinstance(exc, BiliError) and (exc.retryable or exc.code in RISK_CODES)
                    if risk:
                        consecutive_risk += 1
                        self.on_log("error", f"{item.bvid} 风控：{exc}（连续 {consecutive_risk}/{CIRCUIT_LIMIT}）")
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

    async def _expand_seeds(
        self,
        client: BiliClient,
        bvids: list[str],
        uids: list[str],
        config: AcademicConfig,
    ) -> list[FrontierItem]:
        items = [FrontierItem(bvid=bvid, depth=0, seed_bvid=bvid) for bvid in bvids]
        cutoff = cutoff_ts(config.time_range)
        for uid in uids:
            count = 0
            async for listing in client.iter_videos(uid, cutoff):
                bvid = str(listing.get("bvid") or "")
                if not bvid:
                    continue
                items.append(FrontierItem(bvid=bvid, depth=0, seed_bvid=bvid))
                count += 1
                if count >= config.seeds_per_uid:
                    break
            self.on_log("info", f"账号 {uid} 取了 {count} 条种子视频")
        # Deduplicate while keeping first (shallowest) occurrence.
        seen: set[str] = set()
        unique: list[FrontierItem] = []
        for item in items:
            if item.bvid in seen:
                continue
            seen.add(item.bvid)
            unique.append(item)
        return unique

    async def _visit(self, client: BiliClient, config: AcademicConfig, item: FrontierItem) -> bool:
        detail = await client.get_view_detail(item.bvid)
        view = detail.get("View") if isinstance(detail.get("View"), dict) else {}
        if not view.get("bvid") and not view.get("aid"):
            self.corpus.record_gate_decision(config.job_id, item.bvid, False, "no_view", {}, item.depth)
            self.corpus.mark_frontier(config.job_id, item.bvid, "error", "no_view")
            return False
        view["bvid"] = view.get("bvid") or item.bvid
        tags = extract_tags(detail)
        if config.skip_gate:
            passed, reason, checks = True, "forced_transcribe", {"forced": True}
        else:
            passed, reason, checks = evaluate_gate(view, config, item.depth, tags=tags)
        self.corpus.record_gate_decision(config.job_id, item.bvid, passed, reason, checks, item.depth)
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
                    "view_count": pick(view, "stat", "view"),
                    "like_count": pick(view, "stat", "like"),
                    "reply_count": pick(view, "stat", "reply"),
                    "tags": tags,
                    "discovery": "seed" if item.depth == 0 else "snowball",
                    "depth": item.depth,
                    "parent_bvid": item.parent_bvid,
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

        acc_dir = account_dir(LIBRARY_DIR, mid or "unknown", name)
        job = JobConfig(
            uids=[mid] if mid else [],
            time_range=config.time_range,
            crawl_profile=False,
            crawl_videos=True,
            crawl_dynamics=False,
            crawl_comments=config.crawl_comments,
            crawl_danmaku=config.crawl_danmaku,
            media_mode=config.media_mode or "audio",
            transcribe_mode=config.transcribe_mode or "official_then_whisper",
            media_keep=config.media_keep,
            ocr_enabled=bool(config.ocr_enabled) or config.skip_gate,
            resume=config.resume,
            job_id=config.job_id,
            rclone_remote=config.rclone_remote or self.settings.rclone_remote,
            rclone_root=config.rclone_root or self.settings.rclone_root,
            compute_backend=config.compute_backend,
            discovery="seed" if item.depth == 0 else "snowball",
        )
        await self.crawler._crawl_video(
            client,
            job,
            mid,
            acc_dir,
            {"bvid": item.bvid, "title": view.get("title"), "_detail": detail},
        )
        self.corpus.set_video_topology(
            item.bvid,
            discovery="seed" if item.depth == 0 else "snowball",
            depth=item.depth,
            parent_bvid=item.parent_bvid,
            seed_bvid=item.seed_bvid,
            pass_filter=True,
            reject_reason="",
            run_id=config.job_id,
        )
        neighbours: list[str] = []
        if (not config.skip_gate) and item.depth < config.max_depth:
            try:
                related = await client.get_related(item.bvid, limit=config.related_limit)
                neighbours = [
                    str(row.get("bvid") or "")
                    for row in related
                    if isinstance(row, dict) and row.get("bvid")
                ]
                self.corpus.add_edges(config.job_id, item.bvid, neighbours)
                children = [
                    FrontierItem(
                        bvid=bvid,
                        depth=item.depth + 1,
                        parent_bvid=item.bvid,
                        seed_bvid=item.seed_bvid or item.bvid,
                    )
                    for bvid in neighbours
                ]
                added = self.corpus.enqueue(config.job_id, children)
                self.on_log("info", f"{item.bvid} 相关 {len(neighbours)} · 新入队 {added}")
            except Exception as exc:
                self.on_log("warn", f"{item.bvid} 相关推荐失败，节点仍计完成：{exc}")
        self.corpus.mark_frontier(config.job_id, item.bvid, "done")
        return True
