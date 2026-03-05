"""Job processing: dataclasses and pipeline routing."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

from kindle.bubble_detector import detect_bubbles, extract_bubble_mask_manga
from kindle.furigana import annotate as furigana_annotate
from kindle.image_utils import (
    clear_text_in_region,
    clear_text_strokes,
    decode_image_bytes,
    encode_image_pil,
)
from kindle.processor import (
    PipelineMode,
    detect_page_bubbles,
    load_page_from_memory,
    ocr_bubble,
    render_page_to_bytes,
    transform_furigana,
)
from kindle.translator import translate
from kindle.text_renderer import render_english, render_furigana_vertical

log = logging.getLogger(__name__)

# Type for progress callback: (stage, detail, percent)
ProgressCallback = Callable[[str, str, int], None]

VALID_PIPELINES = {"manga_translate", "manga_furigana", "webtoon"}


@dataclass
class ProcessingJob:
    job_id: str
    pipeline: str  # manga_translate, manga_furigana, webtoon
    image_bytes: bytes
    priority: str = "high"
    title: str = ""
    chapter: str = ""
    page_number: str = ""
    source_url: str = ""
    source_hash: str = ""
    rerender_from_metadata: bool = False
    metadata_payload: dict[str, Any] | None = None
    target_lang: str = "en"


@dataclass
class ProcessingResult:
    job_id: str
    status: str  # completed, failed, degraded
    image_bytes: bytes | None = None
    error: str = ""
    processing_time_ms: int = 0
    bubble_count: int = 0
    pipeline: str = ""
    source_hash: str = ""
    content_hash: str = ""
    render_hash: str = ""
    metadata_payload: dict[str, Any] | None = None


def process_job(job: ProcessingJob,
                progress_cb: ProgressCallback | None = None) -> ProcessingResult:
    """Process a single job by routing to the appropriate pipeline."""
    start = time.monotonic()

    try:
        if job.pipeline not in VALID_PIPELINES:
            return ProcessingResult(
                job_id=job.job_id,
                status="failed",
                error=f"Unknown pipeline: {job.pipeline}",
                pipeline=job.pipeline,
                source_hash=job.source_hash,
            )

        if job.rerender_from_metadata:
            result = _rerender_from_metadata(job, progress_cb)
        elif job.pipeline.startswith("manga_"):
            result = _process_manga(job, progress_cb)
        else:
            result = _process_webtoon(job, progress_cb)

        elapsed_ms = int((time.monotonic() - start) * 1000)
        result.processing_time_ms = elapsed_ms
        return result

    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        log.exception("Job %s failed: %s", job.job_id, e)
        return ProcessingResult(
            job_id=job.job_id,
            status="failed",
            error=str(e),
            processing_time_ms=elapsed_ms,
            pipeline=job.pipeline,
            source_hash=job.source_hash,
        )


def _report(cb: ProgressCallback | None, stage: str, detail: str, percent: int):
    """Call progress callback if provided."""
    if cb:
        try:
            cb(stage, detail, percent)
        except Exception:
            log.warning("Progress callback failed", exc_info=True)


def _norm_bbox(bbox: tuple[int, int, int, int], width: int,
               height: int) -> list[float]:
    x1, y1, x2, y2 = bbox
    if width <= 0 or height <= 0:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        round(max(0.0, min(1.0, x1 / width)), 6),
        round(max(0.0, min(1.0, y1 / height)), 6),
        round(max(0.0, min(1.0, x2 / width)), 6),
        round(max(0.0, min(1.0, y2 / height)), 6),
    ]


def _bbox_from_region(region: dict[str, Any], img_w: int,
                      img_h: int) -> tuple[int, int, int, int] | None:
    bbox = region.get("bbox")
    if isinstance(bbox, list) and len(bbox) == 4:
        try:
            x1, y1, x2, y2 = [int(v) for v in bbox]
            return (
                max(0, min(img_w, x1)),
                max(0, min(img_h, y1)),
                max(0, min(img_w, x2)),
                max(0, min(img_h, y2)),
            )
        except (ValueError, TypeError, IndexError):
            pass

    norm = region.get("bbox_norm")
    if isinstance(norm, list) and len(norm) == 4:
        try:
            x1 = int(float(norm[0]) * img_w)
            y1 = int(float(norm[1]) * img_h)
            x2 = int(float(norm[2]) * img_w)
            y2 = int(float(norm[3]) * img_h)
            return (
                max(0, min(img_w, x1)),
                max(0, min(img_h, y1)),
                max(0, min(img_w, x2)),
                max(0, min(img_h, y2)),
            )
        except (ValueError, TypeError, IndexError):
            pass
    return None


def _region_transformed_value(region: dict[str, Any]) -> Any:
    transformed = region.get("transformed")
    if isinstance(transformed, dict):
        return transformed.get("value")
    return transformed


def _region_manual_text(region: dict[str, Any]) -> str:
    user = region.get("user")
    if isinstance(user, dict):
        value = user.get("manual_translation")
        if isinstance(value, str):
            return value.strip()
    return ""


def _build_region(
    region_id: str,
    kind: str,
    bbox: tuple[int, int, int, int] | list[int],
    img_w: int,
    img_h: int,
    ocr_text: str,
    is_valid: bool,
    transformed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a standardised region dict for metadata payloads."""
    bbox_tuple = tuple(int(v) for v in bbox)
    return {
        "id": region_id,
        "kind": kind,
        "bbox": list(bbox_tuple),
        "bbox_norm": _norm_bbox(bbox_tuple, img_w, img_h),
        "ocr_text": ocr_text,
        "is_valid": is_valid,
        "transformed": transformed,
        "user": {"manual_translation": ""},
    }


def _build_metadata_payload(
    job: ProcessingJob,
    regions: list[dict[str, Any]],
    img_w: int,
    img_h: int,
) -> dict[str, Any]:
    """Build the top-level metadata payload returned with every result."""
    return {
        "schema_version": 1,
        "pipeline": job.pipeline,
        "source_hash": job.source_hash,
        "image": {"width": img_w, "height": img_h},
        "regions": regions,
    }


def _rerender_from_metadata(job: ProcessingJob,
                            progress_cb: ProgressCallback | None = None,
                            ) -> ProcessingResult:
    """Re-render using metadata only (skip detection/OCR/translation)."""
    _report(progress_cb, "rerender", "loading metadata", 15)
    payload = job.metadata_payload
    if not payload or not isinstance(payload.get("regions"), list):
        return ProcessingResult(
            job_id=job.job_id,
            status="failed",
            error="Rerender requires metadata with regions",
            pipeline=job.pipeline,
            source_hash=job.source_hash,
        )
    regions = payload["regions"]

    _report(progress_cb, "rerender", "preparing image", 25)
    img_cv, img_pil = decode_image_bytes(job.image_bytes)
    img_out = img_pil.copy()
    img_w, img_h = img_out.width, img_out.height

    _report(progress_cb, "rerender", "drawing edits", 50)

    # Two-pass rendering: clear all regions first, then render text.
    # Prevents overlapping bubbles from erasing each other's rendered text.

    # Pre-process regions into a renderable list
    render_items: list[dict] = []
    for ri, region in enumerate(regions):
        if not isinstance(region, dict):
            continue
        bbox = _bbox_from_region(region, img_w, img_h)
        if not bbox:
            continue

        manual = _region_manual_text(region)
        transformed_val = _region_transformed_value(region)
        kind = str(region.get("kind") or "bubble")

        mask = None
        if job.pipeline.startswith("manga_") and kind != "artwork_text":
            mask = extract_bubble_mask_manga(img_cv, bbox)

        if job.pipeline == "manga_furigana":
            if manual:
                transformed_val = furigana_annotate(manual)
            if not isinstance(transformed_val, list):
                continue
            render_items.append({"bbox": bbox, "mask": mask, "kind": kind,
                                 "mode": "furigana", "value": transformed_val})
        else:
            text = manual
            if not text:
                if isinstance(transformed_val, str):
                    text = transformed_val.strip()
                elif isinstance(transformed_val, list):
                    text = "".join(seg.get("text", "")
                                   for seg in transformed_val
                                   if isinstance(seg, dict)).strip()
            if not text:
                continue
            render_items.append({"bbox": bbox, "mask": mask, "kind": kind,
                                 "mode": "translate", "value": text})

    # Pass 1: Clear
    for item in render_items:
        if item["kind"] == "artwork_text":
            clear_text_in_region(img_out, item["bbox"])
        else:
            clear_text_strokes(img_out, item["bbox"], mask=item["mask"])

    # Pass 2: Render
    applied = 0
    for item in render_items:
        if item["mode"] == "furigana":
            render_furigana_vertical(img_out, item["bbox"], item["value"],
                                     mask=item["mask"])
        else:
            render_english(img_out, item["bbox"], item["value"],
                           mask=item["mask"])
        applied += 1

    _report(progress_cb, "rerender", "encoding", 95)
    output_bytes = encode_image_pil(img_out)
    return ProcessingResult(
        job_id=job.job_id,
        status="completed",
        image_bytes=output_bytes,
        bubble_count=applied,
        pipeline=job.pipeline,
        source_hash=job.source_hash,
        metadata_payload=payload,
    )


def _process_manga(job: ProcessingJob,
                   progress_cb: ProgressCallback | None = None) -> ProcessingResult:
    """Run the manga pipeline (furigana or translate) on image bytes."""
    img_cv, img_pil = decode_image_bytes(job.image_bytes)
    page = load_page_from_memory(img_cv, img_pil, name=job.job_id)

    mode = (PipelineMode.FURIGANA if job.pipeline == "manga_furigana"
            else PipelineMode.TRANSLATE)

    # Detection (RT-DETR-v2 detects bubbles + artwork text in one pass)
    _report(progress_cb, "detecting_bubbles", "", 10)
    detect_page_bubbles(page)

    # OCR
    total = len(page.bubbles_raw)
    for i, bubble_dict in enumerate(page.bubbles_raw):
        _report(progress_cb, "ocr", f"{i+1}/{total} bubbles", 20 + int(40 * (i+1) / max(total, 1)))
        br = ocr_bubble(page.img_pil, bubble_dict)
        page.bubble_results.append(br)

    # Transform
    total_br = len(page.bubble_results)
    if mode == PipelineMode.FURIGANA:
        _report(progress_cb, "translating", "", 65)
        for i, br in enumerate(page.bubble_results):
            _report(progress_cb, "translating", f"{i+1}/{total_br} bubbles",
                    65 + int(25 * (i+1) / max(total_br, 1)))
            transform_furigana(br)
    else:
        # Translation calls are independent HTTP POSTs — run in parallel
        translatable = [(i, br) for i, br in enumerate(page.bubble_results)
                        if br.is_valid]
        _report(progress_cb, "translating", f"{len(translatable)} bubbles", 65)
        if translatable:
            with ThreadPoolExecutor(
                max_workers=min(8, len(translatable))
            ) as pool:
                futures = {
                    pool.submit(translate, br.ocr_text, job.target_lang): (i, br)
                    for i, br in translatable
                }
                done = 0
                for future in as_completed(futures):
                    i, br = futures[future]
                    done += 1
                    _report(progress_cb, "translating",
                            f"{done}/{len(translatable)} bubbles",
                            65 + int(25 * done / len(translatable)))
                    try:
                        english = future.result()
                        if english.strip():
                            br.transformed = english
                    except Exception:
                        log.warning("Translation failed for bubble %d",
                                    i, exc_info=True)

    # Render to bytes
    _report(progress_cb, "rendering", "", 95)
    output_bytes = render_page_to_bytes(page, mode)
    bubble_count = sum(1 for br in page.bubble_results
                       if br.transformed is not None)

    img_w, img_h = page.img_pil.width, page.img_pil.height
    regions: list[dict[str, Any]] = []
    for idx, br in enumerate(page.bubble_results):
        transformed_obj: dict[str, Any] | None = None
        if isinstance(br.transformed, list):
            transformed_obj = {
                "kind": "furigana_segments",
                "value": br.transformed,
            }
        elif isinstance(br.transformed, str):
            transformed_obj = {
                "kind": "text",
                "value": br.transformed,
            }
        regions.append(_build_region(
            region_id=f"r{idx+1}",
            kind="artwork_text" if br.is_artwork_text else "bubble",
            bbox=br.bbox,
            img_w=img_w, img_h=img_h,
            ocr_text=br.ocr_text,
            is_valid=bool(br.is_valid),
            transformed=transformed_obj,
        ))

    return ProcessingResult(
        job_id=job.job_id,
        status="completed",
        image_bytes=output_bytes,
        bubble_count=bubble_count,
        pipeline=job.pipeline,
        source_hash=job.source_hash,
        metadata_payload=_build_metadata_payload(job, regions, img_w, img_h),
    )


def _process_webtoon(job: ProcessingJob,
                     progress_cb: ProgressCallback | None = None) -> ProcessingResult:
    """Run the webtoon pipeline on image bytes."""
    from webtoon.processor import (
        detect_bubbles_rtdetr,
        load_page_from_memory as wt_load_page,
        render_page_to_bytes as wt_render_bytes,
        validate_and_translate,
    )

    img_cv, img_pil = decode_image_bytes(job.image_bytes)
    page = wt_load_page(img_cv, img_pil, name=job.job_id)

    _report(progress_cb, "detecting_bubbles", "", 20)
    detect_bubbles_rtdetr(page)

    _report(progress_cb, "translating", "", 50)
    validate_and_translate(page, parallel=True, target_lang=job.target_lang)

    _report(progress_cb, "rendering", "", 90)
    output_bytes = wt_render_bytes(page, target_lang=job.target_lang)
    bubble_count = sum(1 for r in page.regions if r.english)

    img_w, img_h = page.img_pil.width, page.img_pil.height
    regions: list[dict[str, Any]] = []

    for idx, region in enumerate(page.regions):
        bubble = region.bubble
        transformed_obj = None
        if region.english:
            transformed_obj = {"kind": "text", "value": region.english}
        regions.append(_build_region(
            region_id=f"r{idx+1}",
            kind="bubble",
            bbox=bubble.bbox,
            img_w=img_w, img_h=img_h,
            ocr_text=bubble.combined_text,
            is_valid=bool(region.is_valid),
            transformed=transformed_obj,
        ))

    # Persist SFX as editable regions too.
    for idx, det in enumerate(page.sfx_detections):
        regions.append(_build_region(
            region_id=f"sfx{idx+1}",
            kind="sfx",
            bbox=det.bbox_rect,
            img_w=img_w, img_h=img_h,
            ocr_text=det.text,
            is_valid=True,
            transformed={"kind": "text", "value": ""},
        ))

    return ProcessingResult(
        job_id=job.job_id,
        status="completed",
        image_bytes=output_bytes,
        bubble_count=bubble_count,
        pipeline=job.pipeline,
        source_hash=job.source_hash,
        metadata_payload=_build_metadata_payload(job, regions, img_w, img_h),
    )
