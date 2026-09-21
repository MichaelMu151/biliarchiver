from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from bili.client import BiliClient
from bili.export import (
    account_dir,
    dynamic_dir,
    video_dir,
    write_account_index,
    write_dynamic_markdown,
    write_profile,
    write_video_markdown,
)
from bili.media import fetch_media
from bili.ocr import collect_images_and_ocr
from bili.paths import LIBRARY_DIR, ensure_dirs
from bili.pipeline import PipelineState
from bili.settings import AppSettings
from bili.storage import reclaim_folder, should_fetch_media
from bili.store import Store
from bili.transcribe import _write_transcript_outputs, build_transcript, fetch_official_transcript
from bili.util import cutoff_ts, now_iso, pick, safe_name, write_json, write_text

LogFn = Callable[[str, str], None]
ProgressFn = Callable[[dict[str, Any]], Awaitable[None] | None]


def _line_count(path: Path) -> int:
    with path.open("rb") as fh:
        return sum(1 for line in fh if line.strip())


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
    media_keep: str = "delete_after_text"
    ocr_enabled: bool = True
    resume: bool = True
    job_id: str = ""


@dataclass
class Crawler:
    settings: AppSettings
    store: Store
    library: Path = field(default_factory=lambda: LIBRARY_DIR)
    on_log: LogFn = field(default=lambda *_a, **_k: None)
    should_cancel: Callable[[], bool] = field(default=lambda: False)
    cancelled: bool = False

    def _is_cancelled(self) -> bool:
        if self.should_cancel():
            self.cancelled = True
        return self.cancelled

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
        client = BiliClient(self.settings, on_log=self.on_log)
        progress = {
            "uids_total": len(config.uids),
            "uids_done": 0,
            "videos_done": 0,
            "dynamics_done": 0,
            "current": "",
            "stage": "bootstrap",
        }

        async def emit() -> None:
            if on_progress:
                maybe = on_progress(dict(progress))
                if hasattr(maybe, "__await__"):
                    await maybe

        try:
            await client.bootstrap()
            for uid in config.uids:
                if self._is_cancelled():
                    break
                progress["current"] = uid
                progress["stage"] = "account"
                await emit()
                await self._crawl_uid(client, config, uid, progress, emit)
                progress["uids_done"] += 1
                await emit()
            progress["stage"] = "done" if not self.cancelled else "cancelled"
            await emit()
            return progress
        finally:
            await client.close()

    async def _crawl_uid(self, client: BiliClient, config: JobConfig, uid: str, progress: dict, emit) -> None:
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
            "sex": acc.get("sex") or acc_info.get("sex"),
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
        if config.crawl_profile:
            self.store.upsert_account(profile)
            self.store.add_snapshot(snapshot)
            write_profile(acc_dir, profile, snapshot)
            self.on_log("ok", f"{name} · 粉丝 {snapshot['follower']} · 关注 {snapshot['following']}")

        videos_meta: list[dict[str, Any]] = []
        if config.crawl_videos:
            async for item in client.iter_videos(uid, cutoff):
                if self._is_cancelled():
                    break
                bvid = item.get("bvid")
                key = f"video:{bvid}"
                progress["stage"] = f"video {bvid}"
                progress["current"] = f"{name} / {item.get('title')}"
                await emit()
                try:
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
        if config.crawl_dynamics:
            async for item in client.iter_dynamics(uid, cutoff):
                if self._is_cancelled():
                    break
                extracted = extract_dynamic(item)
                dyn_id = extracted["dyn_id"]
                key = f"dyn:{dyn_id}"
                progress["stage"] = f"dynamic {dyn_id}"
                progress["current"] = f"{name} / 动态 {dyn_id}"
                await emit()
                try:
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

    async def _crawl_video(
        self,
        client: BiliClient,
        config: JobConfig,
        uid: str,
        acc_dir: Path,
        listing: dict[str, Any],
    ) -> dict[str, Any]:
        bvid = listing["bvid"]
        detail = await client.get_view_detail(bvid)
        view = detail.get("View") or {}
        stat = view.get("stat") or {}
        pages = view.get("pages") or [{"cid": view.get("cid"), "page": 1, "part": view.get("title"), "duration": view.get("duration")}]
        folder = video_dir(acc_dir, int(view.get("pubdate") or listing.get("created") or 0), bvid, view.get("title") or listing.get("title") or bvid)
        captured = now_iso()
        meta = {
            "bvid": bvid,
            "aid": view.get("aid"),
            "cid": view.get("cid"),
            "title": view.get("title"),
            "pubdate": view.get("pubdate"),
            "duration": view.get("duration"),
            "desc": view.get("desc"),
            "tname": view.get("tname"),
            "owner": view.get("owner"),
            "stat": stat,
            "pages": [{"cid": p.get("cid"), "page": p.get("page"), "part": p.get("part"), "duration": p.get("duration")} for p in pages],
            "tags": [t.get("tag_name") for t in (detail.get("Tags") or []) if t.get("tag_name")],
            "page_url": f"https://www.bilibili.com/video/{bvid}",
            "captured_at": captured,
        }
        write_json(folder / "meta.json", meta)
        pipeline = PipelineState.load(folder)
        pipeline.mark("metadata", "v2", "done", captured_at=captured)

        comment_count = 0
        if config.crawl_comments:
            comments_path = folder / "comments.jsonl"
            signature = f"v2:pages={self.settings.comment_max_pages}:sub={int(self.settings.include_sub_replies)}"
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
                        max_pages=self.settings.comment_max_pages,
                        include_sub=self.settings.include_sub_replies,
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
            transcript_signature = (
                f"v2:{config.transcribe_mode}:model={self.settings.whisper_model}:"
                f"lang={self.settings.whisper_language}:cid={cid}"
            )
            transcript_outputs = [part_folder / "transcript.md", part_folder / "transcript.json"]
            part_transcript: dict[str, Any] = {"status": "pending", "source": "", "markdown": ""}
            if config.resume and part_pipeline.completed("transcript", transcript_signature, transcript_outputs):
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
                    whisper_model=self.settings.whisper_model,
                    whisper_language=self.settings.whisper_language,
                    whisper_device=self.settings.whisper_device,
                    whisper_compute_type=self.settings.whisper_compute_type,
                    should_cancel=self._is_cancelled,
                    on_log=self.on_log,
                )
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
                rclone_remote=self.settings.rclone_remote,
                rclone_root=self.settings.rclone_root,
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
        self.on_log("ok", f"视频完成 {bvid} · 评 {comment_count} · 弹幕 {danmaku_count}")
        return row

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
        ocr_signature = f"v2:enabled={int(config.ocr_enabled)}:min={self.settings.ocr_min_confidence}"
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
            )
            pipeline.mark("ocr", ocr_signature, "done", images=len(ocr_blocks))
        reclaim_folder(
            folder,
            policy=config.media_keep,
            page_url=str(dyn.get("jump_url") or ""),
            transcript_ready=True,
            ocr_ready=True,
            ffmpeg_path=self.settings.ffmpeg_path,
            rclone_remote=self.settings.rclone_remote,
            rclone_root=self.settings.rclone_root,
            library_root=self.library,
            on_log=self.on_log,
            include_images=True,
            include_media=False,
        )
        comment_count = 0
        if config.crawl_comments and dyn.get("comment_id"):
            path = folder / "comments.jsonl"
            signature = f"v2:pages={self.settings.comment_max_pages}:sub={int(self.settings.include_sub_replies)}"
            if config.resume and pipeline.completed("comments", signature, [path]):
                comment_count = _line_count(path)
            else:
                pipeline.mark("comments", signature, "running")
                comment_count = await self._write_async_jsonl(
                    path,
                    client.iter_comments(
                        str(dyn["comment_id"]),
                        int(dyn.get("comment_type") or 17),
                        max_pages=self.settings.comment_max_pages,
                        include_sub=self.settings.include_sub_replies,
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
        self.on_log("ok", f"动态完成 {dyn['dyn_id']}")
        return row
