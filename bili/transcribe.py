from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Callable

from bili.client import BiliClient
from bili.runtime import prepare_process, resolve_whisper_backend
from bili.util import abs_url, write_json, write_text

_MODEL = None
_MODEL_KEY = ""
_MODEL_LOCK = threading.Lock()
prepare_process()


def _fmt_ts(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _normalise_segments(body: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments = []
    for index, item in enumerate(body, 1):
        start = float(item.get("from") if "from" in item else item.get("start") or 0)
        end = float(item.get("to") if "to" in item else item.get("end") or start)
        text = (item.get("content") or item.get("text") or "").strip()
        if text:
            segments.append({"id": index, "start": start, "end": max(start, end), "text": text})
    return segments


def segments_to_markdown(segments: list[dict[str, Any]], source: str) -> str:
    lines = [f"> 来源：{source}", ""]
    for item in segments:
        lines.append(f"- `{_fmt_ts(float(item.get('start') or 0))}` {item.get('text')}")
    return "\n".join(lines).strip() + "\n"


async def fetch_official_transcript(
    client: BiliClient, bvid: str, cid: int, aid: int | None, up_mid: str
) -> dict[str, Any]:
    player = await client.get_player(bvid, cid, aid)
    subs = ((player.get("subtitle") or {}).get("subtitles") or [])
    chosen = None
    for pref in ("ai-zh", "zh-CN", "zh-Hans", "zh"):
        for sub in subs:
            lan = (sub.get("lan") or "") + " " + (sub.get("lan_doc") or "")
            if pref in lan:
                chosen = sub
                break
        if chosen:
            break
    if not chosen and subs:
        chosen = subs[0]
    if chosen and chosen.get("subtitle_url"):
        url = abs_url(chosen["subtitle_url"])
        raw = await client.get_json(url, wbi=False)
        body = raw.get("body") or []
        if body:
            source = f"B站字幕 {chosen.get('lan_doc') or chosen.get('lan')}"
            segments = _normalise_segments(body)
            return {"source": source, "segments": segments, "markdown": segments_to_markdown(segments, source)}
    conclusion = await client.get_ai_conclusion(bvid, cid, up_mid, aid)
    model = conclusion.get("model_result") or {}
    summary = model.get("summary") or ""
    outline = model.get("outline") or []
    subtitle = model.get("subtitle") or []
    chunks = []
    if summary:
        chunks.append("## AI 摘要\n\n" + summary)
    if outline:
        lines = ["## 章节"]
        for part in outline:
            title = part.get("title") or ""
            lines.append(f"- {title}")
            for stamp in part.get("part_outline") or []:
                lines.append(f"  - `{_fmt_ts(stamp.get('timestamp') or 0)}` {stamp.get('content')}")
        chunks.append("\n".join(lines))
    if subtitle:
        segments = _normalise_segments(subtitle)
        chunks.append(segments_to_markdown(segments, "B站 AI 字幕"))
    else:
        segments = []
    markdown = "\n\n".join(chunks).strip()
    return {"source": "B站 AI 总结", "segments": segments, "markdown": markdown}


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _get_whisper_model(model_size: str, device: str = "auto", compute_type: str = "auto"):
    global _MODEL, _MODEL_KEY
    from faster_whisper import WhisperModel

    resolved_device, resolved_compute, note = resolve_whisper_backend(device, compute_type)
    cache_key = f"{model_size}:{resolved_device}:{resolved_compute}"
    with _MODEL_LOCK:
        if _MODEL is None or _MODEL_KEY != cache_key:
            try:
                _MODEL = WhisperModel(model_size, device=resolved_device, compute_type=resolved_compute)
            except Exception:
                if resolved_device == "cpu":
                    raise
                resolved_device, resolved_compute, note = "cpu", "int8", "GPU 加载失败，已回退 CPU"
                cache_key = f"{model_size}:cpu:int8"
                _MODEL = WhisperModel(model_size, device="cpu", compute_type="int8")
            _MODEL_KEY = cache_key
            _MODEL._bili_backend = (resolved_device, resolved_compute, note)
        return _MODEL


def transcribe_local(
    audio_path: str,
    model_size: str = "small",
    language: str = "auto",
    should_cancel: Callable[[], bool] | None = None,
    device: str = "auto",
    compute_type: str = "auto",
) -> dict[str, Any]:
    model = _get_whisper_model(model_size, device, compute_type)
    backend = getattr(model, "_bili_backend", ("auto", "auto", ""))
    raw_segments, info = model.transcribe(
        audio_path,
        language=None if language == "auto" else language,
        beam_size=5,
        temperature=0,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 250},
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
    )
    segments = []
    for index, seg in enumerate(raw_segments, 1):
        if should_cancel and should_cancel():
            raise RuntimeError("转写已取消")
        if seg.text.strip():
            segments.append(
                {
                    "id": index,
                    "start": float(seg.start),
                    "end": float(seg.end),
                    "text": seg.text.strip(),
                }
            )
    probability = float(getattr(info, "language_probability", 0) or 0)
    source = (
        f"faster-whisper {model_size} · {backend[0]}/{backend[1]} · "
        f"{info.language} · p={probability:.2f}"
    )
    return {
        "source": source,
        "language": info.language,
        "language_probability": probability,
        "segments": segments,
        "markdown": segments_to_markdown(segments, source),
    }


def _srt_time(seconds: float, decimal: str = ",") -> str:
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{decimal}{millis:03d}"


def _write_transcript_outputs(folder: Path, result: dict[str, Any]) -> None:
    segments = result.get("segments") or []
    write_text(folder / "transcript.md", result.get("markdown") or "")
    write_json(
        folder / "transcript.json",
        {
            "schema_version": 1,
            "source": result.get("source"),
            "language": result.get("language"),
            "language_probability": result.get("language_probability"),
            "segments": segments,
        },
    )
    srt = "\n\n".join(
        f"{i}\n{_srt_time(float(seg['start']))} --> {_srt_time(float(seg['end']))}\n{seg['text']}"
        for i, seg in enumerate(segments, 1)
    )
    vtt = "WEBVTT\n\n" + "\n\n".join(
        f"{_srt_time(float(seg['start']), '.')} --> {_srt_time(float(seg['end']), '.')}\n{seg['text']}"
        for seg in segments
    )
    write_text(folder / "transcript.srt", srt + ("\n" if srt else ""))
    write_text(folder / "transcript.vtt", vtt + "\n")


async def build_transcript(
    client: BiliClient,
    *,
    bvid: str,
    cid: int,
    aid: int | None,
    up_mid: str,
    folder: Path,
    mode: str,
    audio_path: str,
    whisper_model: str,
    whisper_language: str = "auto",
    whisper_device: str = "auto",
    whisper_compute_type: str = "auto",
    should_cancel: Callable[[], bool] | None = None,
    on_log,
) -> dict[str, Any]:
    if mode in {"none", "url_only"}:
        return {"status": "skipped", "source": "", "segments": [], "markdown": ""}
    result: dict[str, Any] = {}
    if mode in {"official", "official_then_whisper"}:
        try:
            result = await fetch_official_transcript(client, bvid, cid, aid, up_mid)
            if result.get("markdown"):
                on_log("info", f"已取得官方/AI 字幕 {bvid}")
        except Exception as exc:
            on_log("warn", f"官方字幕失败 {bvid}：{exc}")
    if not result.get("markdown") and mode in {"whisper", "official_then_whisper"}:
        if audio_path and Path(audio_path).exists() and whisper_available():
            on_log("info", f"本地 Whisper 转录 {bvid}，可能较慢")
            try:
                result = await asyncio.to_thread(
                    transcribe_local,
                    audio_path,
                    whisper_model,
                    whisper_language,
                    should_cancel,
                    whisper_device,
                    whisper_compute_type,
                )
            except Exception as exc:
                on_log("warn", f"Whisper 失败 {bvid}：{exc}")
        elif mode == "whisper" or mode == "official_then_whisper":
            on_log("warn", "未安装 faster-whisper 或没有本地音频，已把任务写入 cloud_job.json")
    if result.get("markdown"):
        result["status"] = "done"
        _write_transcript_outputs(folder, result)
    else:
        result = {"status": "pending", "source": "", "segments": [], "markdown": ""}
    return result
