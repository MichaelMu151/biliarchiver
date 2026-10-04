from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator

from bili.client import BiliClient
from bili.corpus import Corpus
from bili.export import (
    account_dir,
    dynamic_dir,
    video_dir,
    write_account_index,
    write_dynamic_markdown,
    write_profile,
    write_scout_catalog,
    write_video_markdown,
)
from bili.gpu_remote import whisper_model_for_backend
from bili.labor_lexicon import match_labor
from bili.media import fetch_media
from bili.ocr import OCR_FRAME_INTERVAL, collect_images_and_ocr, transcribe_video_ocr
from bili.paths import EXPORT_DIR, LIBRARY_DIR, ensure_dirs
from bili.pipeline import PipelineState
from bili.settings import AppSettings
from bili.storage import reclaim_folder, should_fetch_media
from bili.store import Store
from bili.transcribe import (
    _write_transcript_outputs,
    build_transcript,
    fetch_official_transcript,
    folder_has_spoken_transcript,
    folder_whisper_ran_empty,
    transcript_has_text,
)
from bili.util import cutoff_ts, now_iso, pick, safe_name, ts_iso, write_json, write_text

LogFn = Callable[[str, str], None]
ProgressFn = Callable[[dict[str, Any]], Awaitable[None] | None]

# Scout: UPs at/above this archive count use list-title prefilter (skip detail for non-hits).
SCOUT_LARGE_ARCHIVE_THRESHOLD = 400


def _scout_listing_text(item: dict[str, Any]) -> str:
    """Title-focused text from space archive listing (available before detail API)."""
    return " ".join(
        [
            str(item.get("title") or ""),
            str(item.get("description") or item.get("desc") or ""),
        ]
    )


def _line_count(path: Path) -> int:
    with path.open("rb") as fh:
        return sum(1 for line in fh if line.strip())


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Read a sidecar back row by row; corrupt lines are skipped, not fatal."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def extract_dynamic(item: dict[str, Any]) -> dict[str, Any]:
    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    dynamic = modules.get("module_dynamic") or {}
    stat = modules.get("module_stat") or {}
    desc = dynamic.get("desc") or {}
    major = dynamic.get("major") or {}
    text = desc.get("text") or ""
    opus = major.get("opus") or {}
    if opus.get("summary", {}).get("text"):
        text = text or opus["summary"]["text"]
    pictures: list[str] = []
    for pic in opus.get("pics") or []:
        url = pic.get("url") or pic.get("src")
        if url:
            pictures.append(url)
    draw = major.get("draw") or {}
    for pic in draw.get("items") or []:
        if isinstance(pic, dict) and pic.get("src"):
            pictures.append(pic["src"])
    archive = major.get("archive") or {}
    if archive.get("title") and archive.get("title") not in text:
        text = (text + "\n" + archive.get("title", "")).strip()
    basic = item.get("basic") or {}
    jump = ""
    dyn_id = str(item.get("id_str") or "")
    if dyn_id:
        jump = f"https://t.bilibili.com/{dyn_id}"
    return {
        "dyn_id": dyn_id,
        "dyn_type": item.get("type") or "",
        "pub_ts": int(author.get("pub_ts") or 0),
        "text": text.strip(),
        "pictures": pictures,
        "like": pick(stat, "like", "count") or 0,
        "comment": pick(stat, "comment", "count") or 0,
        "forward": pick(stat, "forward", "count") or 0,
        "comment_id": str(basic.get("comment_id_str") or dyn_id),
        "comment_type": int(basic.get("comment_type") or 17),
        "jump_url": jump,
        "raw_type": item.get("type"),
    }


@dataclass
class JobConfig:
    uids: list[str]
    time_range: str = "1y"
    crawl_profile: bool = True
    crawl_videos: bool = True
    crawl_dynamics: bool = True
    crawl_comments: bool = True
    crawl_danmaku: bool = True
    media_mode: str = "link"
    transcribe_mode: str = "official"
    media_keep: str = "upload_then_delete"
    ocr_enabled: bool = True
    resume: bool = True
    job_id: str = ""
    rclone_remote: str = ""
    rclone_root: str = "BiliArchiver"
    compute_backend: str = "local"
    discovery: str = "space"
    # archive = full UP crawl; scout = titles/tags/dynamics only for screening
    mode: str = "archive"
    # Optional per-job comment controls (None → fall back to AppSettings).
    comment_max_pages: int | None = None
    include_sub_replies: bool | None = None


@dataclass
class Crawler:
    settings: AppSettings
    store: Store
    library: Path = field(default_factory=lambda: LIBRARY_DIR)
    on_log: LogFn = field(default=lambda *_a, **_k: None)
    should_cancel: Callable[[], bool] = field(default=lambda: False)
    cancelled: bool = False
    corpus: Corpus | None = None

    def _is_cancelled(self) -> bool:
        if self.should_cancel():
            self.cancelled = True
        return self.cancelled

    def _corpus_guard(self, label: str, action: Callable[[Corpus], Any]) -> None:
        """Dataset writes must never be able to break the archive pipeline."""
        if self.corpus is None:
            return
        try:
            action(self.corpus)
        except Exception as exc:
            self.on_log("warn", f"数据集写入{label}失败（归档不受影响）：{exc}")

    async def _write_async_jsonl(
        self,
        path: Path,
        rows: AsyncIterator[dict[str, Any]],
    ) -> int:
        """Stream into a sidecar and only replace the last good dataset on success."""
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        part.unlink(missing_ok=True)
        count = 0
        try:
            with part.open("w", encoding="utf-8") as fh:
                async for row in rows:
                    if self._is_cancelled():
                        raise asyncio.CancelledError()
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    count += 1
                    if count % 100 == 0:
                        fh.flush()
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(part, path)
            return count
        except BaseException:
            part.unlink(missing_ok=True)
            raise

    async def _optional(self, label: str, awaitable) -> dict[str, Any]:
        try:
            return await awaitable
        except Exception as exc:
            self.on_log("warn", f"{label}暂不可用，已降级继续：{exc}")
            return {}

    async def run(self, config: JobConfig, on_progress: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
        ensure_dirs()
        self.library.mkdir(parents=True, exist_ok=True)
        if config.mode == "scout":
            # Force a light footprint; UI also sets these, but never trust a mixed config.
            config.crawl_comments = False
            config.crawl_danmaku = False
            config.ocr_enabled = False
            config.media_mode = "none"
            config.transcribe_mode = "none"
            config.media_keep = "keep"
            config.crawl_videos = True
            config.crawl_dynamics = True
            config.crawl_profile = True
        client = BiliClient(self.settings, on_log=self.on_log)
        progress = {
            "uids_total": len(config.uids),
            "uids_done": 0,
            "videos_done": 0,
            "dynamics_done": 0,
            "labor_videos_kept": 0,
            "labor_dynamics_kept": 0,
            "videos_skipped": 0,
            "dynamics_skipped": 0,
            "current": "",
            "last_up_name": "",
            "stage": "bootstrap",
            "mode": config.mode,
            "kind": "scout" if config.mode == "scout" else "archive",
        }
        scout_accounts: list[dict[str, Any]] = []
        scout_videos: list[dict[str, Any]] = []
        scout_dynamics: list[dict[str, Any]] = []

        async def emit() -> None:
            if on_progress:
                maybe = on_progress(dict(progress))
                if hasattr(maybe, "__await__"):
                    await maybe

        if config.job_id:
            self._corpus_guard(
                "任务登记",
                lambda c: c.start_run(
                    config.job_id,
                    "scout" if config.mode == "scout" else "archive",
                    {
                        "uids": config.uids,
                        "time_range": config.time_range,
                        "mode": config.mode,
                        "transcribe_mode": config.transcribe_mode,
                        "compute_backend": config.compute_backend,
                        "labor_filter": config.mode == "scout",
                    },
                    label=(
                        f"选题预览 · {len(config.uids)} 个账号 · 劳工词表筛选"
                        if config.mode == "scout"
                        else f"UP 主归档 · {len(config.uids)} 个账号"
                    ),
                ),
            )

        try:
            await client.bootstrap()
            for uid in config.uids:
                if self._is_cancelled():
                    break
                scout_key = f"uid:{uid}:scout_done"
                if config.mode == "scout" and config.resume and config.job_id and self.store.item_done(config.job_id, scout_key):
                    progress["uids_done"] += 1
                    reloaded = self._reload_scout_labor_from_disk(uid)
                    if reloaded:
                        scout_accounts.append(reloaded.get("account") or {})
                        scout_videos.extend(reloaded.get("videos") or [])
                        scout_dynamics.extend(reloaded.get("dynamics") or [])
                        progress["labor_videos_kept"] = len(scout_videos)
                        progress["labor_dynamics_kept"] = len(scout_dynamics)
                        progress["last_up_name"] = (reloaded.get("account") or {}).get("name") or uid
                    progress["current"] = f"{progress.get('last_up_name') or uid}（已完成，跳过）"
                    progress["notify_uid"] = False
                    self.on_log("info", f"跳过已完成选题预览账号 {uid}")
                    await emit()
                    continue
                progress["current"] = uid
                progress["stage"] = "account"
                await emit()
                result = await self._crawl_uid(client, config, uid, progress, emit)
                if config.mode == "scout" and result:
                    inline = bool(result.pop("_inline_prefiltered", False))
                    # Large UPs: list-title gate already applied. Others: post-filter with tags.
                    filtered = self._filter_scout_labor(config, result, already_filtered=inline)
                    scout_accounts.append(filtered.get("account") or {})
                    scout_videos.extend(filtered.get("videos") or [])
                    scout_dynamics.extend(filtered.get("dynamics") or [])
                    progress["labor_videos_kept"] = len(scout_videos)
                    progress["labor_dynamics_kept"] = len(scout_dynamics)
                    progress["last_up_name"] = (filtered.get("account") or {}).get("name") or uid
                    progress["current"] = progress["last_up_name"]
                    progress["notify_uid"] = True
                    if config.job_id:
                        self.store.mark_item(config.job_id, scout_key, "done")
                    # Incremental export so cancelled runs still leave a usable CSV.
                    if config.job_id:
                        out = EXPORT_DIR / f"scout_{config.job_id}"
                        write_scout_catalog(
                            out,
                            job_id=config.job_id,
                            accounts=[row for row in scout_accounts if row],
                            videos=scout_videos,
                            dynamics=scout_dynamics,
                        )
                        progress["scout_export"] = str(out)
                progress["uids_done"] += 1
                await emit()
            if config.mode == "scout" and config.job_id and not self.cancelled:
                out = EXPORT_DIR / f"scout_{config.job_id}"
                write_scout_catalog(
                    out,
                    job_id=config.job_id,
                    accounts=[row for row in scout_accounts if row],
                    videos=scout_videos,
                    dynamics=scout_dynamics,
                )
                progress["scout_export"] = str(out)
                self.on_log(
                    "ok",
                    f"选题预览已导出：{out} · 账号 {len(scout_accounts)} · "
                    f"劳工视频 {len(scout_videos)} · 劳工动态 {len(scout_dynamics)}",
                )
            progress["stage"] = "done" if not self.cancelled else "cancelled"
            await emit()
            return progress
        finally:
            if config.job_id:
                status = "cancelled" if self.cancelled else "done"
                self._corpus_guard("任务收尾", lambda c: c.finish_run(config.job_id, status))
            await client.close()

    def _filter_scout_labor(
        self,
        config: JobConfig,
        result: dict[str, Any],
        *,
        already_filtered: bool = False,
    ) -> dict[str, Any]:
        """Keep only labor-lexicon hits.

        When ``already_filtered`` is True (inline scout path), only annotate tier/matched
        and do not delete — the list-stage gate already skipped non-matches.
        """
        account = dict(result.get("account") or {})
        mid = str(account.get("mid") or "")
        name = str(account.get("name") or mid)
        kept_videos: list[dict[str, Any]] = []
        kept_dynamics: list[dict[str, Any]] = []
        dropped_v = dropped_d = 0

        for row in result.get("videos") or []:
            text = " ".join(
                [
                    str(row.get("title") or ""),
                    str(row.get("tags") or "").replace(" · ", " "),
                    str(row.get("description_short") or ""),
                    str(row.get("tname") or ""),
                ]
            )
            hit = match_labor(text)
            bvid = str(row.get("bvid") or "")
            folder = Path(str(row.get("folder") or ""))
            if not folder.exists() and bvid:
                for cand in self.library.glob(f"{mid}_*/videos/*_{bvid}_*"):
                    folder = cand
                    break
            if hit.hit or (already_filtered and row.get("tier")):
                enriched = dict(row)
                if hit.hit:
                    enriched["tier"] = hit.tier
                    enriched["matched"] = " · ".join(hit.terms)
                kept_videos.append(enriched)
            elif already_filtered:
                # Inline path already gated; keep annotated row even if rematch is soft.
                kept_videos.append(dict(row))
            else:
                if folder.exists():
                    shutil.rmtree(folder, ignore_errors=True)
                if bvid:
                    try:
                        with self.store.connect() as conn:
                            conn.execute("DELETE FROM videos WHERE bvid=?", (bvid,))
                    except Exception:
                        pass
                    if config.job_id:
                        self.store.mark_item(config.job_id, f"video:{bvid}", "dropped")
                dropped_v += 1

        for row in result.get("dynamics") or []:
            text = str(row.get("text") or "")
            hit = match_labor(text)
            dyn_id = str(row.get("dyn_id") or "")
            folder = Path(str(row.get("folder") or ""))
            if not folder.exists() and dyn_id:
                for cand in self.library.glob(f"{mid}_*/dynamics/*_{dyn_id}"):
                    folder = cand
                    break
            if hit.hit or (already_filtered and row.get("tier")):
                enriched = dict(row)
                if hit.hit:
                    enriched["tier"] = hit.tier
                    enriched["matched"] = " · ".join(hit.terms)
                kept_dynamics.append(enriched)
            elif already_filtered:
                kept_dynamics.append(dict(row))
            else:
                if folder.exists():
                    shutil.rmtree(folder, ignore_errors=True)
                if dyn_id:
                    try:
                        with self.store.connect() as conn:
                            conn.execute("DELETE FROM dynamics WHERE dyn_id=?", (dyn_id,))
                    except Exception:
                        pass
                    if config.job_id:
                        self.store.mark_item(config.job_id, f"dyn:{dyn_id}", "dropped")
                dropped_d += 1

        account["video_n"] = len(kept_videos)
        account["dynamic_n"] = len(kept_dynamics)
        account["videos_dropped"] = dropped_v
        account["dynamics_dropped"] = dropped_d

        for acc_dir in self.library.glob(f"{mid}_*"):
            detail = self.store.account_detail(mid)
            profile_path = acc_dir / "profile.json"
            profile = {}
            if profile_path.exists():
                try:
                    profile = json.loads(profile_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    profile = {"mid": mid, "name": name}
            else:
                profile = {"mid": mid, "name": name, "space_url": account.get("space_url")}
            write_account_index(
                acc_dir,
                profile,
                detail.get("videos") or [],
                detail.get("dynamics") or [],
            )
            break

        if dropped_v or dropped_d:
            self.on_log(
                "ok",
                f"{name} · 劳工筛选：视频保留 {len(kept_videos)}/{len(kept_videos)+dropped_v} · "
                f"动态保留 {len(kept_dynamics)}/{len(kept_dynamics)+dropped_d}",
            )
        return {"account": account, "videos": kept_videos, "dynamics": kept_dynamics}

    async def _crawl_uid(self, client: BiliClient, config: JobConfig, uid: str, progress: dict, emit) -> dict[str, Any] | None:
        cutoff = cutoff_ts(config.time_range)
        card = await client.get_card(uid)
        acc = card.get("card") or {}
        acc_info = await self._optional("详细资料", client.get_acc_info(uid))
        relation = await self._optional("粉丝/关注", client.get_relation_stat(uid))
        upstat = await self._optional("播放/获赞", client.get_upstat(uid))
        navnum = await self._optional("投稿统计", client.get_navnum(uid))
        name = acc.get("name") or acc_info.get("name") or uid
        profile = {
            "mid": str(acc.get("mid") or uid),
            "name": name,
            "sign": acc.get("sign") or acc_info.get("sign") or "",
            "face": acc.get("face") or acc_info.get("face") or "",
            "level": (acc.get("level_info") or {}).get("current_level") or acc_info.get("level"),
            "official": acc.get("official") or acc_info.get("official") or {},
            "sex": acc.get("sex") or acc_info.get("sex") or "",
            "school": (acc_info.get("school") or {}).get("name") or "",
            "birthday": acc_info.get("birthday") or "",
            "space_url": f"https://space.bilibili.com/{uid}",
            "updated_at": now_iso(),
        }
        snapshot = {
            "mid": uid,
            "captured_at": now_iso(),
            "follower": relation.get("follower") or acc.get("fans") or 0,
            "following": relation.get("following") or acc.get("friend") or 0,
            "archive_count": (navnum.get("video") if isinstance(navnum, dict) else None) or acc.get("archive_count") or 0,
            "likes": pick(upstat, "likes") or pick(upstat, "archive", "likes") or 0,
            "views": pick(upstat, "archive", "view") or 0,
            "raw_json": json.dumps({"card": card, "relation": relation, "upstat": upstat, "navnum": navnum}, ensure_ascii=False),
        }
        acc_dir = account_dir(self.library, uid, name)
        self._corpus_guard("账号资料", lambda c: c.upsert_author(profile))
        self._corpus_guard(
            "账号快照",
            lambda c: c.add_author_snapshot(snapshot, run_id=config.job_id),
        )
        if config.crawl_profile:
            self.store.upsert_account(profile)
            self.store.add_snapshot(snapshot)
            write_profile(acc_dir, profile, snapshot)
            self.on_log("ok", f"{name} · 粉丝 {snapshot['follower']} · 关注 {snapshot['following']}")

        archive_count = int(snapshot.get("archive_count") or 0)
        inline_prefilter = config.mode == "scout" and archive_count >= SCOUT_LARGE_ARCHIVE_THRESHOLD
        if inline_prefilter:
            self.on_log(
                "info",
                f"{name} · 投稿约 {archive_count} ≥ {SCOUT_LARGE_ARCHIVE_THRESHOLD}，"
                f"启用列表标题预筛（超大号：标题命中才拉详情）",
            )

        videos_meta: list[dict[str, Any]] = []
        scout_video_rows: list[dict[str, Any]] = []
        scout_skipped_videos = 0
        scout_skipped_dynamics = 0
        if config.crawl_videos:
            async for item in client.iter_videos(uid, cutoff):
                if self._is_cancelled():
                    break
                bvid = item.get("bvid")
                key = f"video:{bvid}"
                prior = self.store.item_status(config.job_id, key) if config.job_id else ""
                if config.resume and prior == "done":
                    if config.mode == "scout":
                        existing = self._scout_row_from_disk(uid, name, bvid)
                        if existing:
                            scout_video_rows.append(existing)
                            videos_meta.append({"bvid": bvid, "title": existing.get("title")})
                    progress["videos_done"] += 1
                    continue
                if config.resume and prior == "skipped" and inline_prefilter:
                    scout_skipped_videos += 1
                    continue
                # Large-UP scout: filter on listing title before detail API.
                if inline_prefilter:
                    listing_hit = match_labor(_scout_listing_text(item))
                    if not listing_hit.hit:
                        scout_skipped_videos += 1
                        if config.job_id:
                            self.store.mark_item(config.job_id, key, "skipped", "labor_lexicon_listing_title")
                        if scout_skipped_videos % 50 == 1:
                            progress["stage"] = "scout_skip"
                            progress["current"] = f"{name} · 标题未命中，跳过（本号已跳 {scout_skipped_videos}）"
                            progress["videos_skipped"] = int(progress.get("videos_skipped") or 0)
                            await emit()
                        continue
                progress["stage"] = f"video {bvid}"
                progress["current"] = f"{name} / {item.get('title')}"
                await emit()
                try:
                    if config.mode == "scout":
                        meta_row, scout_row = await self._scout_video(client, config, uid, name, acc_dir, item)
                        if inline_prefilter:
                            detail_hit = match_labor(
                                " ".join(
                                    [
                                        str(scout_row.get("title") or ""),
                                        str(scout_row.get("tags") or "").replace(" · ", " "),
                                        str(scout_row.get("description_short") or ""),
                                        str(scout_row.get("tname") or ""),
                                    ]
                                )
                            )
                            scout_row["tier"] = detail_hit.tier if detail_hit.hit else "listing"
                            scout_row["matched"] = " · ".join(detail_hit.terms) if detail_hit.hit else "标题预筛"
                        videos_meta.append(meta_row)
                        scout_video_rows.append(scout_row)
                    else:
                        meta_row = await self._crawl_video(client, config, uid, acc_dir, item)
                        videos_meta.append(meta_row)
                    if config.job_id:
                        self.store.mark_item(config.job_id, key, "done")
                    progress["videos_done"] += 1
                except Exception as exc:
                    self.on_log("error", f"视频 {bvid} 失败：{exc}")
                    if config.job_id:
                        self.store.mark_item(config.job_id, key, "error", str(exc))
                await emit()

        dynamics_meta: list[dict[str, Any]] = []
        scout_dyn_rows: list[dict[str, Any]] = []
        if config.crawl_dynamics:
            async for item in client.iter_dynamics(uid, cutoff):
                if self._is_cancelled():
                    break
                extracted = extract_dynamic(item)
                dyn_id = extracted["dyn_id"]
                key = f"dyn:{dyn_id}"
                prior = self.store.item_status(config.job_id, key) if config.job_id else ""
                if config.resume and prior == "done":
                    if config.mode == "scout":
                        existing = self._scout_dyn_from_disk(uid, name, dyn_id)
                        if existing:
                            scout_dyn_rows.append(existing)
                            dynamics_meta.append({"dyn_id": dyn_id, "text": existing.get("text")})
                    progress["dynamics_done"] += 1
                    continue
                if config.resume and prior == "skipped" and inline_prefilter:
                    scout_skipped_dynamics += 1
                    continue
                # Large-UP scout: filter dynamic text before write / extra requests.
                if inline_prefilter:
                    dyn_hit = match_labor(str(extracted.get("text") or ""))
                    if not dyn_hit.hit:
                        scout_skipped_dynamics += 1
                        if config.job_id:
                            self.store.mark_item(config.job_id, key, "skipped", "labor_lexicon")
                        if scout_skipped_dynamics % 50 == 1:
                            progress["stage"] = "scout_skip"
                            progress["current"] = f"{name} · 动态未命中，跳过（本号已跳 {scout_skipped_dynamics}）"
                            await emit()
                        continue
                progress["stage"] = f"dynamic {dyn_id}"
                progress["current"] = f"{name} / 动态 {dyn_id}"
                await emit()
                try:
                    if config.mode == "scout":
                        row, scout_row = await self._scout_dynamic(client, config, uid, name, acc_dir, extracted)
                        if inline_prefilter:
                            dyn_hit = match_labor(str(scout_row.get("text") or ""))
                            scout_row["tier"] = dyn_hit.tier
                            scout_row["matched"] = " · ".join(dyn_hit.terms)
                        dynamics_meta.append(row)
                        scout_dyn_rows.append(scout_row)
                    else:
                        row = await self._crawl_dynamic(client, config, uid, acc_dir, extracted)
                        dynamics_meta.append(row)
                    if config.job_id:
                        self.store.mark_item(config.job_id, key, "done")
                    progress["dynamics_done"] += 1
                except Exception as exc:
                    self.on_log("error", f"动态 {dyn_id} 失败：{exc}")
                    if config.job_id:
                        self.store.mark_item(config.job_id, key, "error", str(exc))
                await emit()

        if inline_prefilter:
            progress["videos_skipped"] = int(progress.get("videos_skipped") or 0) + scout_skipped_videos
            progress["dynamics_skipped"] = int(progress.get("dynamics_skipped") or 0) + scout_skipped_dynamics
            self.on_log(
                "ok",
                f"{name} · 超大号标题预筛：保留视频 {len(scout_video_rows)} / 跳过 {scout_skipped_videos} · "
                f"保留动态 {len(scout_dyn_rows)} / 跳过 {scout_skipped_dynamics}",
            )

        # Rebuild durable indexes from SQLite, not only this run's in-memory slice.
        # This keeps prior items visible after cancellation, a narrower time range,
        # or a resumed run.
        account_data = self.store.account_detail(uid)
        indexed_videos = account_data.get("videos") or videos_meta
        indexed_dynamics = account_data.get("dynamics") or dynamics_meta
        write_account_index(acc_dir, profile, indexed_videos, indexed_dynamics)
        write_json(
            acc_dir / "manifests" / "cloud_transcribe.json",
            {
                "mid": uid,
                "name": name,
                "hint": "把下列仍缺 transcript 的视频交给云 GPU。优先使用本地 audio；若只有 page_url，请在云端重新请求 playurl。",
                "videos": [
                    {
                        "bvid": v.get("bvid"),
                        "title": v.get("title"),
                        "page_url": v.get("page_url"),
                        "local_audio": v.get("local_audio"),
                        "markdown_path": v.get("markdown_path"),
                    }
                    for v in indexed_videos
                    if not v.get("transcript_path")
                ],
            },
        )
        if config.mode == "scout":
            return {
                "account": {
                    "mid": uid,
                    "name": name,
                    "space_url": profile.get("space_url"),
                    "video_n": len(scout_video_rows),
                    "dynamic_n": len(scout_dyn_rows),
                    "follower": snapshot.get("follower") or 0,
                    "sign": (profile.get("sign") or "")[:120],
                },
                "videos": scout_video_rows,
                "dynamics": scout_dyn_rows,
                "_inline_prefiltered": inline_prefilter,
            }
        return None

    async def _scout_video(
        self,
        client: BiliClient,
        config: JobConfig,
        uid: str,
        up_name: str,
        acc_dir: Path,
        listing: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Titles + tags + basic stats only — no comments/media/transcript."""
        bvid = listing["bvid"]
        detail = await client.get_view_detail(bvid)
        view = detail.get("View") if isinstance(detail.get("View"), dict) else {}
        owner = view.get("owner") if isinstance(view.get("owner"), dict) else {}
        stat = view.get("stat") if isinstance(view.get("stat"), dict) else {}
        folder = video_dir(
            acc_dir,
            int(view.get("pubdate") or listing.get("created") or 0),
            bvid,
            view.get("title") or listing.get("title") or bvid,
        )
        captured = now_iso()
        tags: list[str] = []
        for tag in detail.get("Tags") or []:
            if isinstance(tag, dict):
                name = str(tag.get("tag_name") or tag.get("name") or "").strip()
                if name:
                    tags.append(name)
        title = view.get("title") or listing.get("title") or bvid
        desc = str(view.get("desc") or "")
        meta = {
            "bvid": bvid,
            "aid": view.get("aid"),
            "cid": view.get("cid"),
            "title": title,
            "pubdate": view.get("pubdate") or listing.get("created"),
            "duration": view.get("duration") or listing.get("length"),
            "desc": desc,
            "tname": view.get("tname") or "",
            "owner": owner,
            "stat": stat,
            "pages": [],
            "tags": tags,
            "page_url": f"https://www.bilibili.com/video/{bvid}",
            "captured_at": captured,
            "scout": True,
        }
        write_json(folder / "meta.json", meta)
        pipeline = PipelineState.load(folder)
        pipeline.mark("metadata", "v2-scout", "done", captured_at=captured)
        md = (
            f"# {title}\n\n"
            f"- BV：{bvid}\n"
            f"- 分区：{meta.get('tname') or '—'}\n"
            f"- 标签：{' · '.join(tags) if tags else '（无）'}\n"
            f"- 播放 {stat.get('view') or 0} · 点赞 {stat.get('like') or 0} · 评论 {stat.get('reply') or 0}\n"
            f"- 链接：{meta['page_url']}\n\n"
            f"## 简介\n\n{desc or '（无）'}\n\n"
            f"> 选题预览模式：未采集评论 / 弹幕 / 媒体 / 转写。\n"
        )
        md_path = folder / "video.md"
        write_text(md_path, md)
        row = {
            "bvid": bvid,
            "mid": uid,
            "aid": str(view.get("aid") or ""),
            "cid": str(view.get("cid") or ""),
            "title": title,
            "pubdate": meta.get("pubdate"),
            "duration": meta.get("duration"),
            "view": stat.get("view"),
            "like_n": stat.get("like"),
            "coin": stat.get("coin"),
            "favorite": stat.get("favorite"),
            "share": stat.get("share"),
            "reply": stat.get("reply"),
            "danmaku": stat.get("danmaku"),
            "tname": meta.get("tname"),
            "description": desc,
            "page_url": meta["page_url"],
            "local_audio": "",
            "local_video": "",
            "transcript_path": "",
            "markdown_path": str(md_path),
            "status": "done",
            "error": "",
            "captured_at": captured,
        }
        self.store.upsert_video(row)
        self._corpus_guard(
            f"视频 {bvid}",
            lambda c: c.upsert_video(
                {
                    "bvid": bvid,
                    "aid": meta.get("aid"),
                    "cid": meta.get("cid"),
                    "mid": uid,
                    "author_name": owner.get("name") or up_name,
                    "title": title,
                    "description": desc,
                    "tname": meta.get("tname"),
                    "pubdate": meta.get("pubdate"),
                    "duration": meta.get("duration"),
                    "view_count": stat.get("view"),
                    "like_count": stat.get("like"),
                    "coin_count": stat.get("coin"),
                    "favorite_count": stat.get("favorite"),
                    "share_count": stat.get("share"),
                    "reply_count": stat.get("reply"),
                    "danmaku_count": stat.get("danmaku"),
                    "tags": tags,
                    "pages": [],
                    "page_count": 1,
                    "page_url": meta["page_url"],
                    "discovery": "space",
                    "run_id": config.job_id,
                    "captured_at": captured,
                }
            ),
        )
        scout_row = {
            "mid": uid,
            "name": up_name,
            "bvid": bvid,
            "title": title,
            "tags": " · ".join(tags),
            "tname": meta.get("tname") or "",
            "pubdate_iso": ts_iso(int(meta.get("pubdate") or 0))[:10],
            "view": stat.get("view") or 0,
            "like": stat.get("like") or 0,
            "reply": stat.get("reply") or 0,
            "duration": meta.get("duration") or 0,
            "page_url": meta["page_url"],
            "description_short": desc.replace("\n", " ")[:160],
            "folder": str(folder),
        }
        self.on_log("ok", f"预览视频 {bvid} · 标签 {len(tags)}")
        return row, scout_row

    async def _scout_dynamic(
        self,
        client: BiliClient,
        config: JobConfig,
        uid: str,
        up_name: str,
        acc_dir: Path,
        dyn: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Dynamic text only — no OCR, no comments, no image download."""
        folder = dynamic_dir(acc_dir, dyn["pub_ts"], dyn["dyn_id"])
        captured = now_iso()
        dyn = dict(dyn)
        dyn["captured_at"] = captured
        dyn["scout"] = True
        write_json(folder / "meta.json", dyn)
        pipeline = PipelineState.load(folder)
        pipeline.mark("metadata", "v2-scout", "done", captured_at=captured)
        text = str(dyn.get("text") or "")
        pics = dyn.get("pictures") or []
        md = (
            f"# 动态 {dyn['dyn_id']}\n\n"
            f"- 时间：{ts_iso(int(dyn.get('pub_ts') or 0))}\n"
            f"- 赞 {dyn.get('like') or 0} · 评 {dyn.get('comment') or 0} · 转发 {dyn.get('forward') or 0}\n"
            f"- 图片数：{len(pics)}\n"
            f"- 链接：{dyn.get('jump_url') or '—'}\n\n"
            f"## 正文\n\n{text or '（无文字）'}\n\n"
            f"> 选题预览模式：未下载图片 / OCR / 评论。\n"
        )
        md_path = folder / "dynamic.md"
        write_text(md_path, md)
        row = {
            "dyn_id": dyn["dyn_id"],
            "mid": uid,
            "dyn_type": dyn.get("dyn_type"),
            "pub_ts": dyn.get("pub_ts"),
            "text": text,
            "like_n": dyn.get("like"),
            "comment_n": dyn.get("comment"),
            "forward_n": dyn.get("forward"),
            "markdown_path": str(md_path),
            "status": "done",
            "error": "",
            "captured_at": captured,
        }
        self.store.upsert_dynamic(row)
        dyn_payload = dict(dyn)
        dyn_payload["mid"] = uid
        self._corpus_guard(
            f"动态 {dyn['dyn_id']}",
            lambda c: c.upsert_dynamic(dyn_payload, [], run_id=config.job_id),
        )
        scout_row = {
            "mid": uid,
            "name": up_name,
            "dyn_id": dyn["dyn_id"],
            "pub_time_iso": ts_iso(int(dyn.get("pub_ts") or 0)),
            "text": text.replace("\n", " ")[:500],
            "like": dyn.get("like") or 0,
            "comment": dyn.get("comment") or 0,
            "forward": dyn.get("forward") or 0,
            "picture_count": len(pics),
            "jump_url": dyn.get("jump_url") or "",
            "folder": str(folder),
        }
        self.on_log("ok", f"预览动态 {dyn['dyn_id']}")
        return row, scout_row

    def _scout_row_from_disk(self, uid: str, up_name: str, bvid: str) -> dict[str, Any] | None:
        for folder in self.library.glob(f"{uid}_*/videos/*_{bvid}_*"):
            meta_path = folder / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            tags = meta.get("tags") or []
            if isinstance(tags, str):
                tags = [tags]
            return {
                "mid": uid,
                "name": up_name,
                "bvid": bvid,
                "title": meta.get("title") or "",
                "tags": " · ".join(tags),
                "tname": meta.get("tname") or "",
                "pubdate_iso": ts_iso(int(meta.get("pubdate") or 0))[:10],
                "view": (meta.get("stat") or {}).get("view") or 0,
                "like": (meta.get("stat") or {}).get("like") or 0,
                "reply": (meta.get("stat") or {}).get("reply") or 0,
                "duration": meta.get("duration") or 0,
                "page_url": meta.get("page_url") or f"https://www.bilibili.com/video/{bvid}",
                "description_short": str(meta.get("desc") or "").replace("\n", " ")[:160],
                "folder": str(folder),
            }
        return None

    def _scout_dyn_from_disk(self, uid: str, up_name: str, dyn_id: str) -> dict[str, Any] | None:
        for folder in self.library.glob(f"{uid}_*/dynamics/*_{dyn_id}"):
            meta_path = folder / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            text = str(meta.get("text") or "")
            return {
                "mid": uid,
                "name": up_name,
                "dyn_id": dyn_id,
                "pub_time_iso": ts_iso(int(meta.get("pub_ts") or 0)),
                "text": text.replace("\n", " ")[:500],
                "like": meta.get("like") or 0,
                "comment": meta.get("comment") or 0,
                "forward": meta.get("forward") or 0,
                "picture_count": len(meta.get("pictures") or []),
                "jump_url": meta.get("jump_url") or "",
                "folder": str(folder),
            }
        return None

    def _reload_scout_labor_from_disk(self, uid: str) -> dict[str, Any] | None:
        acc_dirs = list(self.library.glob(f"{uid}_*"))
        if not acc_dirs:
            return None
        acc_dir = acc_dirs[0]
        name = acc_dir.name.split("_", 1)[-1] if "_" in acc_dir.name else uid
        profile_path = acc_dir / "profile.json"
        if profile_path.exists():
            try:
                name = json.loads(profile_path.read_text(encoding="utf-8")).get("name") or name
            except (OSError, json.JSONDecodeError):
                pass
        videos: list[dict[str, Any]] = []
        dynamics: list[dict[str, Any]] = []
        for folder in sorted((acc_dir / "videos").glob("*")) if (acc_dir / "videos").exists() else []:
            meta_path = folder / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            bvid = str(meta.get("bvid") or "")
            row = self._scout_row_from_disk(uid, name, bvid)
            if row:
                videos.append(row)
        for folder in sorted((acc_dir / "dynamics").glob("*")) if (acc_dir / "dynamics").exists() else []:
            meta_path = folder / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            dyn_id = str(meta.get("dyn_id") or "")
            row = self._scout_dyn_from_disk(uid, name, dyn_id)
            if row:
                dynamics.append(row)
        return {
            "account": {
                "mid": uid,
                "name": name,
                "space_url": f"https://space.bilibili.com/{uid}",
                "video_n": len(videos),
                "dynamic_n": len(dynamics),
            },
            "videos": videos,
            "dynamics": dynamics,
        }

    async def _crawl_video(
        self,
        client: BiliClient,
        config: JobConfig,
        uid: str,
        acc_dir: Path,
        listing: dict[str, Any],
    ) -> dict[str, Any]:
        bvid = listing["bvid"]
        detail = listing.get("_detail") if isinstance(listing.get("_detail"), dict) else None
        if not detail:
            detail = await client.get_view_detail(bvid)
        view = detail.get("View") if isinstance(detail.get("View"), dict) else {}
        owner = view.get("owner") if isinstance(view.get("owner"), dict) else {}
        stat = view.get("stat") if isinstance(view.get("stat"), dict) else {}
        pages = view.get("pages") or [{"cid": view.get("cid"), "page": 1, "part": view.get("title"), "duration": view.get("duration")}]
        if not isinstance(pages, list):
            pages = [{"cid": view.get("cid"), "page": 1, "part": view.get("title"), "duration": view.get("duration")}]
        folder = video_dir(acc_dir, int(view.get("pubdate") or listing.get("created") or 0), bvid, view.get("title") or listing.get("title") or bvid)
        captured = now_iso()
        tags = []
        for tag in detail.get("Tags") or []:
            if isinstance(tag, dict):
                name = str(tag.get("tag_name") or tag.get("name") or "").strip()
                if name:
                    tags.append(name)
        meta = {
            "bvid": bvid,
            "aid": view.get("aid"),
            "cid": view.get("cid"),
            "title": view.get("title"),
            "pubdate": view.get("pubdate"),
            "duration": view.get("duration"),
            "desc": view.get("desc"),
            "tname": view.get("tname"),
            "owner": owner,
            "stat": stat,
            "pages": [
                {"cid": p.get("cid"), "page": p.get("page"), "part": p.get("part"), "duration": p.get("duration")}
                for p in pages
                if isinstance(p, dict)
            ],
            "tags": tags,
            "page_url": f"https://www.bilibili.com/video/{bvid}",
            "captured_at": captured,
        }
        write_json(folder / "meta.json", meta)
        pipeline = PipelineState.load(folder)
        pipeline.mark("metadata", "v2", "done", captured_at=captured)

        comment_count = 0
        if config.crawl_comments:
            comments_path = folder / "comments.jsonl"
            max_pages = (
                int(config.comment_max_pages)
                if config.comment_max_pages is not None
                else int(self.settings.comment_max_pages)
            )
            include_sub = (
                bool(config.include_sub_replies)
                if config.include_sub_replies is not None
                else bool(self.settings.include_sub_replies)
            )
            signature = f"v2:pages={max_pages}:sub={int(include_sub)}"
            if config.resume and pipeline.completed("comments", signature, [comments_path]):
                comment_count = _line_count(comments_path)
                self.on_log("info", f"复用评论 {bvid} · {comment_count} 条")
            else:
                pipeline.mark("comments", signature, "running")
                comment_count = await self._write_async_jsonl(
                    comments_path,
                    client.iter_comments(
                        str(view.get("aid") or listing.get("aid")),
                        1,
                        max_pages=max_pages,
                        include_sub=include_sub,
                    ),
                )
                pipeline.mark("comments", signature, "done", count=comment_count)

        danmaku_count = 0
        if config.crawl_danmaku:
            danmaku_path = folder / "danmaku.jsonl"
            signature = "v2:full-segments"
            if config.resume and pipeline.completed("danmaku", signature, [danmaku_path]):
                danmaku_count = _line_count(danmaku_path)
                self.on_log("info", f"复用弹幕 {bvid} · {danmaku_count} 条")
            else:
                pipeline.mark("danmaku", signature, "running")
                part_path = danmaku_path.with_name(danmaku_path.name + ".part")
                part_path.unlink(missing_ok=True)
                try:
                    with part_path.open("w", encoding="utf-8") as fh:
                        for page in pages:
                            if self._is_cancelled():
                                raise asyncio.CancelledError()
                            cid = int(page.get("cid") or 0)
                            if not cid:
                                continue
                            dms = await client.get_danmaku(
                                cid,
                                int(page.get("duration") or view.get("duration") or 0),
                                int(view.get("aid") or 0) or None,
                            )
                            for dm in dms:
                                dm["cid"] = cid
                                dm["bvid"] = bvid
                                dm["part"] = page.get("part")
                                fh.write(json.dumps(dm, ensure_ascii=False) + "\n")
                                danmaku_count += 1
                            fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(part_path, danmaku_path)
                except BaseException:
                    part_path.unlink(missing_ok=True)
                    raise
                pipeline.mark("danmaku", signature, "done", count=danmaku_count)

        part_results: list[dict[str, Any]] = []
        multipart = len(pages) > 1
        for index, page in enumerate(pages, 1):
            cid = int(page.get("cid") or 0)
            if not cid:
                continue
            page_no = int(page.get("page") or index)
            part_name = str(page.get("part") or f"P{page_no}")
            part_folder = (
                folder / "parts" / f"P{page_no:02d}_{safe_name(part_name)}"
                if multipart
                else folder
            )
            part_folder.mkdir(parents=True, exist_ok=True)
            part_pipeline = PipelineState.load(part_folder)
            whisper_model = whisper_model_for_backend(self.settings.whisper_model, config.compute_backend)
            transcript_signature = (
                f"v2:{config.transcribe_mode}:backend={config.compute_backend}:"
                f"model={whisper_model}:"
                f"lang={self.settings.whisper_language}:cid={cid}"
            )
            transcript_outputs = [part_folder / "transcript.md", part_folder / "transcript.json"]
            part_transcript: dict[str, Any] = {"status": "pending", "source": "", "markdown": ""}
            if (
                config.resume
                and part_pipeline.completed("transcript", transcript_signature, transcript_outputs)
                and folder_has_spoken_transcript(part_folder)
            ):
                part_transcript = {
                    "status": "done",
                    "markdown": (part_folder / "transcript.md").read_text(encoding="utf-8"),
                    "source": "existing",
                }
                self.on_log("info", f"复用转录 {bvid} P{page_no}")
            elif config.transcribe_mode in {"official", "official_then_whisper"}:
                try:
                    official = await fetch_official_transcript(
                        client, bvid, cid, int(view.get("aid") or 0) or None, uid
                    )
                except Exception as exc:
                    official = {}
                    self.on_log("warn", f"官方字幕失败 {bvid}：{exc}")
                if official.get("markdown"):
                    _write_transcript_outputs(part_folder, official)
                    part_transcript = {
                        "status": "done",
                        "markdown": official.get("markdown") or "",
                        "source": official.get("source"),
                    }
                    part_pipeline.mark("transcript", transcript_signature, "done", source=part_transcript["source"])
                    self.on_log("info", f"已取得官方/AI 字幕 {bvid} P{page_no}，可跳过体积更大的媒体")

            fetch_mode = should_fetch_media(
                config.media_mode,
                config.media_keep,
                config.transcribe_mode,
                part_transcript.get("status") == "done",
            )
            media_signature = f"v2:{fetch_mode}:qn={self.settings.video_quality}:cid={cid}:keep={config.media_keep}"
            media_info = await fetch_media(
                client,
                bvid=bvid,
                cid=cid,
                folder=part_folder,
                mode=fetch_mode,
                qn=self.settings.video_quality,
                ffmpeg_path=self.settings.ffmpeg_path,
                on_log=self.on_log,
                should_cancel=self._is_cancelled,
            )
            part_pipeline.mark(
                "media",
                media_signature,
                "done" if media_info.get("status") not in {"error"} else "error",
                mode=fetch_mode,
                media_status=media_info.get("status"),
                local_audio=media_info.get("local_audio"),
                local_video=media_info.get("local_video"),
            )
            if part_transcript.get("status") != "done":
                part_pipeline.mark("transcript", transcript_signature, "running")
                skip_whisper = folder_whisper_ran_empty(part_folder)
                if not skip_whisper:
                    audio_path = media_info.get("local_audio") or media_info.get("local_video") or ""
                    part_transcript = await build_transcript(
                        client,
                        bvid=bvid,
                        cid=cid,
                        aid=int(view.get("aid") or 0) or None,
                        up_mid=uid,
                        folder=part_folder,
                        mode=config.transcribe_mode,
                        audio_path=audio_path,
                        whisper_model=whisper_model,
                        whisper_language=self.settings.whisper_language,
                        whisper_device=self.settings.whisper_device,
                        whisper_compute_type=self.settings.whisper_compute_type,
                        gpu_worker_url=self.settings.gpu_worker_url,
                        gpu_worker_token=self.settings.gpu_worker_token,
                        compute_backend=config.compute_backend,
                        should_cancel=self._is_cancelled,
                        on_log=self.on_log,
                    )
                if (
                    not transcript_has_text(part_transcript)
                    and config.transcribe_mode in {"whisper", "official_then_whisper"}
                ):
                    video_path = media_info.get("local_video") or ""
                    if not video_path or not Path(video_path).is_file():
                        video_info = await fetch_media(
                            client,
                            bvid=bvid,
                            cid=cid,
                            folder=part_folder,
                            mode="video",
                            qn=self.settings.video_quality,
                            ffmpeg_path=self.settings.ffmpeg_path,
                            on_log=self.on_log,
                            should_cancel=self._is_cancelled,
                        )
                        video_path = video_info.get("local_video") or ""
                        if video_info.get("local_audio"):
                            media_info["local_audio"] = video_info["local_audio"]
                        if video_path:
                            media_info["local_video"] = video_path
                    if video_path:
                        ocr_transcript = await transcribe_video_ocr(
                            video_path=video_path,
                            folder=part_folder,
                            ffmpeg_path=self.settings.ffmpeg_path,
                            interval=OCR_FRAME_INTERVAL,
                            min_confidence=self.settings.ocr_min_confidence,
                            compute_backend=config.compute_backend,
                            gpu_worker_url=self.settings.gpu_worker_url,
                            gpu_worker_token=self.settings.gpu_worker_token,
                            should_cancel=self._is_cancelled,
                            on_log=self.on_log,
                        )
                        if transcript_has_text(ocr_transcript):
                            _write_transcript_outputs(part_folder, ocr_transcript)
                            part_transcript = ocr_transcript
                part_pipeline.mark(
                    "transcript",
                    transcript_signature,
                    "done" if part_transcript.get("status") == "done" else "pending",
                    source=part_transcript.get("source"),
                )
            storage_info = reclaim_folder(
                part_folder,
                policy=config.media_keep,
                page_url=f"https://www.bilibili.com/video/{bvid}",
                transcript_ready=part_transcript.get("status") == "done"
                or config.transcribe_mode in {"none", "url_only"},
                ocr_ready=True,
                ffmpeg_path=self.settings.ffmpeg_path,
                rclone_remote=config.rclone_remote or self.settings.rclone_remote,
                rclone_root=config.rclone_root or self.settings.rclone_root,
                library_root=self.library,
                on_log=self.on_log,
                include_images=False,
                include_media=True,
            )
            if storage_info.get("released"):
                media_info["local_audio"] = ""
                media_info["local_video"] = ""
                media_info["status"] = "released"
            media_info["storage"] = storage_info
            part_results.append(
                {
                    "page": page_no,
                    "cid": cid,
                    "title": part_name,
                    "folder": str(part_folder),
                    "media": media_info,
                    "transcript": part_transcript,
                }
            )

        media_info = (part_results[0].get("media") if part_results else {}) or {}
        completed_transcripts = [
            part for part in part_results if (part.get("transcript") or {}).get("status") == "done"
        ]
        if multipart and completed_transcripts:
            chunks = []
            manifest_parts = []
            for part in completed_transcripts:
                info = part["transcript"]
                chunks.append(f"## P{part['page']} · {part['title']}\n\n{info.get('markdown') or ''}")
                part_path = Path(part["folder"]) / "transcript.json"
                manifest_parts.append(
                    {
                        "page": part["page"],
                        "cid": part["cid"],
                        "title": part["title"],
                        "transcript_json": str(part_path.relative_to(folder)),
                    }
                )
            aggregate_markdown = "\n\n".join(chunks).strip() + "\n"
            write_text(folder / "transcript.md", aggregate_markdown)
            write_json(
                folder / "transcript.json",
                {"schema_version": 1, "multipart": True, "parts": manifest_parts},
            )
            transcript_info = {
                "status": "done",
                "source": "multipart",
                "markdown": aggregate_markdown,
            }
        elif completed_transcripts:
            transcript_info = completed_transcripts[0]["transcript"]
        else:
            transcript_info = {"status": "pending", "source": "", "markdown": ""}

        meta["media_parts"] = [
            {
                "page": part["page"],
                "cid": part["cid"],
                "title": part["title"],
                "folder": part["folder"],
                "local_audio": (part["media"] or {}).get("local_audio"),
                "local_video": (part["media"] or {}).get("local_video"),
                "media_status": (part["media"] or {}).get("status"),
                "transcript_status": (part["transcript"] or {}).get("status"),
                "transcript_source": (part["transcript"] or {}).get("source"),
                "storage_policy": ((part["media"] or {}).get("storage") or {}).get("policy"),
                "storage_note": ((part["media"] or {}).get("storage") or {}).get("note"),
                "drive_uris": ((part["media"] or {}).get("storage") or {}).get("drive_uris") or [],
            }
            for part in part_results
        ]
        meta["local_audio"] = media_info.get("local_audio")
        meta["local_video"] = media_info.get("local_video")
        meta["media_status"] = media_info.get("status")
        meta["storage"] = media_info.get("storage") or {}
        meta["transcript_status"] = transcript_info.get("status")
        meta["captured_at"] = captured
        write_json(folder / "meta.json", meta)
        md_path = write_video_markdown(
            folder,
            meta,
            transcript_info.get("markdown") or "",
            comment_count,
            danmaku_count,
        )
        row = {
            "bvid": bvid,
            "mid": uid,
            "aid": str(view.get("aid") or ""),
            "cid": str(view.get("cid") or ""),
            "title": view.get("title"),
            "pubdate": view.get("pubdate"),
            "duration": view.get("duration"),
            "view": stat.get("view"),
            "like_n": stat.get("like"),
            "coin": stat.get("coin"),
            "favorite": stat.get("favorite"),
            "share": stat.get("share"),
            "reply": stat.get("reply"),
            "danmaku": stat.get("danmaku"),
            "tname": view.get("tname"),
            "description": view.get("desc"),
            "page_url": meta["page_url"],
            "local_audio": media_info.get("local_audio"),
            "local_video": media_info.get("local_video"),
            "transcript_path": str(folder / "transcript.md") if transcript_info.get("status") == "done" else "",
            "markdown_path": str(md_path),
            "status": "done",
            "error": "",
            "captured_at": captured,
        }
        self.store.upsert_video(row)
        self._ingest_video_corpus(config, uid, folder, meta, part_results)
        self.on_log("ok", f"视频完成 {bvid} · 评 {comment_count} · 弹幕 {danmaku_count}")
        return row

    def _ingest_video_corpus(
        self,
        config: JobConfig,
        uid: str,
        folder: Path,
        meta: dict[str, Any],
        part_results: list[dict[str, Any]],
    ) -> None:
        """Mirror one finished video into the relational dataset.

        Reads the JSONL sidecars back rather than the in-memory slice, so a resumed
        run that reused comments/danmaku still lands them in the corpus.
        """
        if self.corpus is None:
            return
        bvid = str(meta.get("bvid") or "")
        if not bvid:
            return
        owner = meta.get("owner") if isinstance(meta.get("owner"), dict) else {}
        stat = meta.get("stat") if isinstance(meta.get("stat"), dict) else {}
        aid = None
        try:
            aid = int(meta.get("aid") or 0) or None
        except (TypeError, ValueError):
            aid = None

        self._corpus_guard(
            f"视频 {bvid}",
            lambda c: c.upsert_video(
                {
                    "bvid": bvid,
                    "aid": meta.get("aid"),
                    "cid": meta.get("cid"),
                    "mid": uid,
                    "author_name": owner.get("name") or "",
                    "title": meta.get("title"),
                    "description": meta.get("desc"),
                    "tid": meta.get("tid"),
                    "tname": meta.get("tname"),
                    "pubdate": meta.get("pubdate"),
                    "duration": meta.get("duration"),
                    "view_count": stat.get("view"),
                    "like_count": stat.get("like"),
                    "coin_count": stat.get("coin"),
                    "favorite_count": stat.get("favorite"),
                    "share_count": stat.get("share"),
                    "reply_count": stat.get("reply"),
                    "danmaku_count": stat.get("danmaku"),
                    "tags": meta.get("tags") or [],
                    "pages": meta.get("pages") or [],
                    "page_count": len(meta.get("pages") or []) or 1,
                    "page_url": meta.get("page_url"),
                    "discovery": config.discovery or "space",
                    "run_id": config.job_id,
                    "captured_at": meta.get("captured_at"),
                }
            ),
        )

        comments_path = folder / "comments.jsonl"
        if comments_path.exists():
            self._corpus_guard(
                f"评论 {bvid}",
                lambda c: c.upsert_comments(
                    iter_jsonl(comments_path),
                    target_kind="video",
                    target_id=bvid,
                    bvid=bvid,
                    aid=aid,
                    run_id=config.job_id,
                ),
            )

        danmaku_path = folder / "danmaku.jsonl"
        if danmaku_path.exists():
            self._corpus_guard(
                f"弹幕 {bvid}",
                lambda c: c.upsert_danmaku(iter_jsonl(danmaku_path), run_id=config.job_id),
            )

        for part in part_results:
            part_folder = Path(part.get("folder") or folder)
            transcript_json = part_folder / "transcript.json"
            if not transcript_json.exists():
                continue
            try:
                payload = json.loads(transcript_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("multipart"):
                continue
            self._corpus_guard(
                f"转写 {bvid}",
                lambda c, p=payload, part=part: c.upsert_transcript(
                    bvid=bvid,
                    cid=int(part.get("cid") or 0),
                    page=int(part.get("page") or 1),
                    payload=p,
                    run_id=config.job_id,
                ),
            )

    async def _crawl_dynamic(
        self,
        client: BiliClient,
        config: JobConfig,
        uid: str,
        acc_dir: Path,
        dyn: dict[str, Any],
    ) -> dict[str, Any]:
        folder = dynamic_dir(acc_dir, dyn["pub_ts"], dyn["dyn_id"])
        captured = now_iso()
        dyn["captured_at"] = captured
        write_json(folder / "meta.json", dyn)
        pipeline = PipelineState.load(folder)
        pipeline.mark("metadata", "v2", "done", captured_at=captured)
        ocr_signature = (
            f"v2:enabled={int(config.ocr_enabled)}:min={self.settings.ocr_min_confidence}"
            f":backend={config.compute_backend}"
        )
        ocr_outputs = [folder / "ocr.json"] if dyn.get("pictures") else []
        if config.resume and pipeline.completed("ocr", ocr_signature, ocr_outputs):
            try:
                ocr_blocks = json.loads((folder / "ocr.json").read_text(encoding="utf-8")).get("images") or []
            except (OSError, json.JSONDecodeError):
                ocr_blocks = []
            self.on_log("info", f"复用 OCR {dyn['dyn_id']} · {len(ocr_blocks)} 张图")
        else:
            pipeline.mark("ocr", ocr_signature, "running")
            ocr_blocks = await collect_images_and_ocr(
                client,
                dyn.get("pictures") or [],
                folder,
                enabled=config.ocr_enabled,
                on_log=self.on_log,
                min_confidence=self.settings.ocr_min_confidence,
                compute_backend=config.compute_backend,
                gpu_worker_url=self.settings.gpu_worker_url,
                gpu_worker_token=self.settings.gpu_worker_token,
            )
            pipeline.mark("ocr", ocr_signature, "done", images=len(ocr_blocks))
        reclaim_folder(
            folder,
            policy=config.media_keep,
            page_url=str(dyn.get("jump_url") or ""),
            transcript_ready=True,
            ocr_ready=True,
            ffmpeg_path=self.settings.ffmpeg_path,
            rclone_remote=config.rclone_remote or self.settings.rclone_remote,
            rclone_root=config.rclone_root or self.settings.rclone_root,
            library_root=self.library,
            on_log=self.on_log,
            include_images=True,
            include_media=False,
        )
        comment_count = 0
        if config.crawl_comments and dyn.get("comment_id"):
            path = folder / "comments.jsonl"
            max_pages = (
                int(config.comment_max_pages)
                if config.comment_max_pages is not None
                else int(self.settings.comment_max_pages)
            )
            include_sub = (
                bool(config.include_sub_replies)
                if config.include_sub_replies is not None
                else bool(self.settings.include_sub_replies)
            )
            signature = f"v2:pages={max_pages}:sub={int(include_sub)}"
            if config.resume and pipeline.completed("comments", signature, [path]):
                comment_count = _line_count(path)
            else:
                pipeline.mark("comments", signature, "running")
                comment_count = await self._write_async_jsonl(
                    path,
                    client.iter_comments(
                        str(dyn["comment_id"]),
                        int(dyn.get("comment_type") or 17),
                        max_pages=max_pages,
                        include_sub=include_sub,
                    ),
                )
                pipeline.mark("comments", signature, "done", count=comment_count)
        md_path = write_dynamic_markdown(folder, dyn, ocr_blocks, comment_count)
        row = {
            "dyn_id": dyn["dyn_id"],
            "mid": uid,
            "dyn_type": dyn.get("dyn_type"),
            "pub_ts": dyn.get("pub_ts"),
            "text": dyn.get("text"),
            "like_n": dyn.get("like"),
            "comment_n": dyn.get("comment"),
            "forward_n": dyn.get("forward"),
            "markdown_path": str(md_path),
            "status": "done",
            "error": "",
            "captured_at": captured,
        }
        self.store.upsert_dynamic(row)
        dyn_payload = dict(dyn)
        dyn_payload["mid"] = uid
        self._corpus_guard(
            f"动态 {dyn['dyn_id']}",
            lambda c: c.upsert_dynamic(dyn_payload, ocr_blocks, run_id=config.job_id),
        )
        comments_path = folder / "comments.jsonl"
        if comments_path.exists():
            self._corpus_guard(
                f"动态评论 {dyn['dyn_id']}",
                lambda c: c.upsert_comments(
                    iter_jsonl(comments_path),
                    target_kind="dynamic",
                    target_id=str(dyn["dyn_id"]),
                    run_id=config.job_id,
                ),
            )
        self.on_log("ok", f"动态完成 {dyn['dyn_id']}")
        return row
