from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance, ImageOps

from bili.client import BiliClient
from bili.util import abs_url, write_json, write_text


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
