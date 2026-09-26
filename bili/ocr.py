from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageEnhance, ImageOps

from bili.client import BiliClient
from bili.runtime import run_quiet
from bili.util import abs_url, write_json, write_text

OCR_FRAME_INTERVAL = 2.0
OCR_FRAME_MAX = 180
LogFn = Callable[[str, str], None]


def ocr_available() -> bool:
    try:
        from rapidocr import RapidOCR  # noqa: F401
        return True
    except Exception:
        try:
            from rapidocr_onnxruntime import RapidOCR  # noqa: F401
            return True
        except Exception:
            return False


_ENGINE = None


def _prepare_image(image_path: Path, output_path: Path) -> None:
    with Image.open(image_path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        max_side = max(image.size)
        if max_side > 4096:
            scale = 4096 / max_side
            image = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.Resampling.LANCZOS,
            )
        image = ImageEnhance.Contrast(image).enhance(1.12)
        image.save(output_path, format="PNG", optimize=True)


def _run_ocr(image_path: Path, min_confidence: float) -> dict[str, Any]:
    global _ENGINE
    modern = True
    try:
        from rapidocr import RapidOCR
    except Exception:
        from rapidocr_onnxruntime import RapidOCR
        modern = False

    if _ENGINE is None:
        _ENGINE = RapidOCR()
    with tempfile.TemporaryDirectory(prefix="bili-ocr-") as tmp:
        prepared = Path(tmp) / "prepared.png"
        _prepare_image(image_path, prepared)
        output = _ENGINE(
            str(prepared),
            use_cls=True,
            text_score=min_confidence,
        ) if modern else _ENGINE(str(prepared))
    if modern:
        boxes = getattr(output, "boxes", None)
        texts = getattr(output, "txts", None)
        scores = getattr(output, "scores", None)
        boxes = [] if boxes is None else boxes
        texts = [] if texts is None else texts
        scores = [] if scores is None else scores
        result = list(zip(boxes, texts, scores))
        elapsed = getattr(output, "elapse_list", None)
    else:
        result, elapsed = output
    lines = []
    for item in result or []:
        if len(item) < 3 or not item[1]:
            continue
        score = float(item[2] or 0)
        if score < min_confidence:
            continue
        lines.append(
            {
                "text": str(item[1]).strip(),
                "confidence": round(score, 4),
                "box": item[0].tolist() if hasattr(item[0], "tolist") else item[0],
            }
        )
    return {
        "text": "\n".join(line["text"] for line in lines),
        "lines": lines,
        "elapsed_seconds": elapsed,
    }


async def collect_images_and_ocr(
    client: BiliClient,
    urls: list[str],
    folder: Path,
    enabled: bool,
    on_log,
    min_confidence: float = 0.55,
    compute_backend: str = "local",
    gpu_worker_url: str = "",
    gpu_worker_token: str = "",
) -> list[dict[str, Any]]:
    image_dir = folder / "images"
    blocks: list[dict[str, Any]] = []
    use_remote = enabled and compute_backend == "cloud" and bool(gpu_worker_url)
    engine_ok = enabled and (use_remote or ocr_available())
    if enabled and not engine_ok:
        on_log("warn", "未安装 RapidOCR，动态图片会下载但不会 OCR。可执行 pip install rapidocr onnxruntime")
    semaphore = asyncio.Semaphore(3)

    async def download_one(idx: int, source_url: str) -> tuple[Path, str] | None:
        url = abs_url(source_url)
        if not url:
            return None
        ext = ".jpg"
        lower = url.split("?")[0].lower()
        for cand in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            if lower.endswith(cand):
                ext = cand
                break
        dest = image_dir / f"{idx:02d}{ext}"
        try:
            async with semaphore:
                await client.download_file(url, dest)
            return dest, url
        except Exception as exc:
            on_log("warn", f"动态图片下载失败：{exc}")
            return None

    downloaded = await asyncio.gather(
        *(download_one(idx, url) for idx, url in enumerate(urls, 1))
    )
    downloads = [item for item in downloaded if item is not None]
    for dest, url in downloads:
        text = ""
        lines: list[dict[str, Any]] = []
        elapsed = None
        if engine_ok:
            try:
                if use_remote:
                    from bili.gpu_remote import ocr_remote

                    result = await ocr_remote(
                        url=gpu_worker_url,
                        token=gpu_worker_token,
                        image_path=str(dest),
                        min_confidence=min_confidence,
                    )
                else:
                    result = await asyncio.to_thread(_run_ocr, dest, min_confidence)
                text = result["text"]
                lines = result["lines"]
                elapsed = result["elapsed_seconds"]
            except Exception as exc:
                on_log("warn", f"OCR 失败 {dest.name}：{exc}")
        blocks.append(
            {
                "file": str(dest.relative_to(folder)),
                "url": url,
                "text": text,
                "lines": lines,
                "elapsed_seconds": elapsed,
            }
        )
    if blocks:
        write_json(
            folder / "ocr.json",
            {
                "schema_version": 1,
                "engine": "RapidOCR-cloud" if use_remote else ("RapidOCR" if engine_ok else "not-installed"),
                "min_confidence": min_confidence,
                "images": blocks,
            },
        )
        markdown = ["# 图片 OCR", ""]
        for block in blocks:
            markdown.extend(
                [
                    f"## {block['file']}",
                    "",
                    block["text"] or "（未识别到高置信度文字）",
                    "",
                ]
            )
        write_text(folder / "ocr.md", "\n".join(markdown).strip() + "\n")
    return blocks


_SLIDE_NOISE = re.compile(r"[\s\-—_,.，。！？、：:；;·•“”\"'‘’（）()【】\[\]《》<>~～…]+")
SLIDE_DUP_THRESHOLD = 0.86


def normalize_slide_text(text: str) -> str:
    return _SLIDE_NOISE.sub("", text or "").lower()


def slide_similarity(left: str, right: str) -> float:
    a = normalize_slide_text(left)
    b = normalize_slide_text(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if shorter in longer and len(shorter) / max(len(longer), 1) >= 0.55:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def slides_are_duplicate(left: str, right: str, threshold: float = SLIDE_DUP_THRESHOLD) -> bool:
    return slide_similarity(left, right) >= threshold


def _prefer_slide_text(current: str, incoming: str) -> str:
    if len(incoming.strip()) > len(current.strip()):
        return incoming.strip()
    return current.strip()


def dedupe_transcript_segments(
    segments: list[dict[str, Any]],
    interval: float = OCR_FRAME_INTERVAL,
) -> list[dict[str, Any]]:
    """Merge nearby repeats (2s vs 6s slides) and drop later copies of the same card."""
    merged: list[dict[str, Any]] = []
    for item in segments:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        start = float(item.get("start") or 0)
        end = float(item.get("end") or (start + interval))
        if merged and slides_are_duplicate(str(merged[-1]["text"]), text):
            merged[-1]["text"] = _prefer_slide_text(str(merged[-1]["text"]), text)
            merged[-1]["end"] = max(float(merged[-1]["end"]), end)
            continue
        merged.append({"start": start, "end": end, "text": text})
    unique: list[dict[str, Any]] = []
    for item in merged:
        if any(slides_are_duplicate(str(prev["text"]), str(item["text"])) for prev in unique):
            continue
        unique.append(item)
    return [
        {"id": index, "start": item["start"], "end": item["end"], "text": item["text"]}
        for index, item in enumerate(unique, 1)
    ]


def merge_frame_ocr_segments(
    frames: list[dict[str, Any]],
    interval: float = OCR_FRAME_INTERVAL,
) -> list[dict[str, Any]]:
    """Collapse 2s samples of a slower-changing slide, then drop duplicate timestamps."""
    raw: list[dict[str, Any]] = []
    for frame in frames:
        text = str(frame.get("text") or "").strip()
        if not text:
            continue
        start = float(frame.get("start") or 0)
        raw.append({"start": start, "end": start + interval, "text": text})
    return dedupe_transcript_segments(raw, interval=interval)


def extract_video_frames(
    video_path: str | Path,
    dest_dir: Path,
    *,
    ffmpeg_path: str,
    interval: float = OCR_FRAME_INTERVAL,
    max_frames: int = OCR_FRAME_MAX,
) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for old in dest_dir.glob("frame_*.jpg"):
        old.unlink(missing_ok=True)
    rate = 1.0 / max(interval, 0.2)
    cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"fps={rate:.6f}",
        "-frames:v",
        str(max(1, max_frames)),
        "-q:v",
        "4",
        str(dest_dir / "frame_%04d.jpg"),
    ]
    proc = run_quiet(cmd)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "ffmpeg 截帧失败")[-240:])
    return sorted(dest_dir.glob("frame_*.jpg"))


async def transcribe_video_ocr(
    *,
    video_path: str,
    folder: Path,
    ffmpeg_path: str,
    interval: float = OCR_FRAME_INTERVAL,
    min_confidence: float = 0.55,
    compute_backend: str = "local",
    gpu_worker_url: str = "",
    gpu_worker_token: str = "",
    should_cancel: Callable[[], bool] | None = None,
    on_log: LogFn | None = None,
) -> dict[str, Any]:
    """OCR on-screen text every ``interval`` seconds and return a transcript payload."""
    log = on_log or (lambda *_a, **_k: None)
    video = Path(video_path)
    if not video.is_file():
        return {"status": "pending", "source": "", "segments": [], "markdown": ""}
    from bili.media import which_ffmpeg

    ffmpeg = which_ffmpeg(ffmpeg_path)
    if not ffmpeg:
        log("warn", "没有 ffmpeg，无法从视频截帧做 OCR")
        return {"status": "pending", "source": "", "segments": [], "markdown": ""}
    use_remote = compute_backend == "cloud" and bool(gpu_worker_url)
    if not use_remote and not ocr_available():
        log("warn", "未安装 RapidOCR，无声视频无法做画面文字识别")
        return {"status": "pending", "source": "", "segments": [], "markdown": ""}
    frames_dir = folder / "ocr_frames"
    try:
        paths = await asyncio.to_thread(
            extract_video_frames,
            video,
            frames_dir,
            ffmpeg_path=ffmpeg,
            interval=interval,
        )
    except Exception as exc:
        log("warn", f"截帧失败：{exc}")
        return {"status": "pending", "source": "", "segments": [], "markdown": ""}
    if not paths:
        log("warn", "视频截帧得到 0 张图")
        return {"status": "pending", "source": "", "segments": [], "markdown": ""}
    log("info", f"无对白，改为每 {interval:g} 秒截帧 OCR · {len(paths)} 张")
    frame_rows: list[dict[str, Any]] = []
    for index, path in enumerate(paths):
        if should_cancel and should_cancel():
            raise asyncio.CancelledError()
        start = index * interval
        text = ""
        try:
            if use_remote:
                from bili.gpu_remote import ocr_remote

                payload = await ocr_remote(
                    url=gpu_worker_url,
                    token=gpu_worker_token,
                    image_path=str(path),
                    min_confidence=min_confidence,
                )
            else:
                payload = await asyncio.to_thread(_run_ocr, path, min_confidence)
            text = str((payload or {}).get("text") or "").strip()
        except Exception as exc:
            log("warn", f"画面 OCR 失败 {path.name}：{exc}")
        frame_rows.append({"start": start, "file": path.name, "text": text})
    segments = merge_frame_ocr_segments(frame_rows, interval=interval)
    before = sum(1 for row in frame_rows if str(row.get("text") or "").strip())
    if before and len(segments) < before:
        log("info", f"画面文字去重：{before} 条采样 → {len(segments)} 段")
    write_json(
        folder / "ocr_frames.json",
        {
            "schema_version": 1,
            "interval": interval,
            "engine": "RapidOCR-cloud" if use_remote else "RapidOCR",
            "frames": frame_rows,
        },
    )
    shutil.rmtree(frames_dir, ignore_errors=True)
    if not segments:
        log("warn", "画面 OCR 没有识别到文字")
        return {"status": "pending", "source": "", "segments": [], "markdown": ""}
    from bili.transcribe import segments_to_markdown

    source = f"rapidocr frames · {interval:g}s · {len(segments)} slides"
    markdown = segments_to_markdown(segments, source)
    log("ok", f"画面 OCR 转写完成 {len(segments)} 段")
    return {
        "status": "done",
        "source": source,
        "language": "zh",
        "language_probability": 0,
        "segments": segments,
        "markdown": markdown,
    }
