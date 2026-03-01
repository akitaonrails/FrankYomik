"""Job processing: dataclasses and pipeline routing."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from pipeline.bubble_detector import detect_bubbles
from pipeline.furigana import annotate as furigana_annotate
from pipeline.config import EN_BASE_FONT_DIVISOR, EN_BASE_FONT_MAX, EN_BASE_FONT_MIN
from pipeline.image_utils import (
    clear_text_in_contour,
    clear_text_in_region,
    clear_text_strokes,
    contour_inner_bbox,
    decode_image_bytes,
    encode_image_pil,
)
from pipeline.processor import (
    PipelineMode,
    detect_page_bubbles,
    detect_page_text,
    load_page_from_memory,
    ocr_bubble,
    render_page_to_bytes,
    transform_furigana,
    transform_translate,
)
from pipeline.text_renderer import render_english, render_furigana_vertical

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
            pass


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
        except Exception:
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
        except Exception:
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


def _region_skipped(region: dict[str, Any]) -> bool:
    user = region.get("user")
    if not isinstance(user, dict):
        return False
    if user.get("false_positive") is True:
        return True
    if user.get("wrong_sfx") is True:
        return True
    return False


def _bbox_iou(a: tuple[int, int, int, int],
              b: tuple[int, int, int, int]) -> float:
    """Intersection-over-union between two (x1,y1,x2,y2) bboxes."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _match_contours(
    regions: list[dict[str, Any]],
    detected: list[dict],
    img_w: int,
    img_h: int,
) -> dict[int, Any]:
    """Match metadata regions to detected bubble contours by bbox IoU.

    Returns {region_index: contour_ndarray} for regions that matched.
    """
    import numpy as np
    matched: dict[int, np.ndarray] = {}
    used: set[int] = set()
    for ri, region in enumerate(regions):
        bbox = _bbox_from_region(region, img_w, img_h)
        if not bbox:
            continue
        best_iou = 0.0
        best_di = -1
        for di, det in enumerate(detected):
            if di in used:
                continue
            det_bbox = det.get("bbox")
            if not det_bbox:
                continue
            iou = _bbox_iou(bbox, det_bbox)
            if iou > best_iou:
                best_iou = iou
                best_di = di
        if best_iou > 0.3 and best_di >= 0:
            contour = detected[best_di].get("contour")
            if contour is not None:
                matched[ri] = contour
                used.add(best_di)
    return matched


def _rerender_from_metadata(job: ProcessingJob,
                            progress_cb: ProgressCallback | None = None,
                            ) -> ProcessingResult:
    """Re-render using metadata only (skip detection/OCR/translation).

    Re-runs bubble detection to recover contour shapes so the clearing
    and layout logic is identical to the fresh pipeline.
    """
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

    _report(progress_cb, "rerender", "detecting bubbles", 25)
    img_cv, img_pil = decode_image_bytes(job.image_bytes)
    img_out = img_pil.copy()
    img_w, img_h = img_out.width, img_out.height

    # Re-run bubble detection on the original image to recover contours.
    # This is pure OpenCV (~50-100ms), no GPU needed.
    detected = detect_bubbles(img_cv)
    contour_map = _match_contours(regions, detected, img_w, img_h)

    _report(progress_cb, "rerender", "drawing edits", 50)

    # Calculate base font size the same way the fresh pipeline does.
    base_font_size = None
    if job.pipeline != "manga_furigana":
        base_font_size = max(EN_BASE_FONT_MIN,
                             min(EN_BASE_FONT_MAX, img_h // EN_BASE_FONT_DIVISOR))

    applied = 0
    for ri, region in enumerate(regions):
        if not isinstance(region, dict):
            continue
        if _region_skipped(region):
            continue
        bbox = _bbox_from_region(region, img_w, img_h)
        if not bbox:
            continue

        manual = _region_manual_text(region)
        transformed_val = _region_transformed_value(region)
        kind = str(region.get("kind") or "bubble")
        contour = contour_map.get(ri)

        if job.pipeline == "manga_furigana":
            if manual:
                transformed_val = furigana_annotate(manual)
            if not isinstance(transformed_val, list):
                continue
            # Use contour-based clearing when available (same as fresh)
            layout_bbox = bbox
            if contour is not None:
                layout_bbox = contour_inner_bbox(contour) or bbox
                clear_text_in_contour(img_out, contour)
            else:
                clear_text_strokes(img_out, bbox)
            render_furigana_vertical(img_out, layout_bbox, transformed_val)
            applied += 1
            continue

        # Translate/webtoon path: render plain English text.
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

        # Clear text using the same logic as the fresh pipeline:
        # contour shape when available, stroke-only fallback otherwise.
        layout_bbox = bbox
        if kind == "artwork_text":
            clear_text_in_region(img_out, bbox)
        elif contour is not None:
            layout_bbox = contour_inner_bbox(contour) or bbox
            clear_text_in_contour(img_out, contour)
        else:
            clear_text_strokes(img_out, bbox)
        render_english(img_out, layout_bbox, text, base_font_size=base_font_size)
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

    # Detection
    _report(progress_cb, "detecting_bubbles", "", 10)
    detect_page_bubbles(page)

    _report(progress_cb, "detecting_text", "", 20)
    detect_page_text(page)

    # OCR
    total = len(page.bubbles_raw)
    for i, bubble_dict in enumerate(page.bubbles_raw):
        _report(progress_cb, "ocr", f"{i+1}/{total} bubbles", 20 + int(40 * (i+1) / max(total, 1)))
        br = ocr_bubble(page.img_pil, bubble_dict)
        page.bubble_results.append(br)

    # Transform
    _report(progress_cb, "translating", "", 65)
    transform_fn = (transform_furigana if mode == PipelineMode.FURIGANA
                    else transform_translate)
    total_br = len(page.bubble_results)
    for i, br in enumerate(page.bubble_results):
        _report(progress_cb, "translating", f"{i+1}/{total_br} bubbles", 65 + int(25 * (i+1) / max(total_br, 1)))
        transform_fn(br)

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
        regions.append({
            "id": f"r{idx+1}",
            "kind": "artwork_text" if br.is_artwork_text else "bubble",
            "bbox": [int(v) for v in br.bbox],
            "bbox_norm": _norm_bbox(br.bbox, img_w, img_h),
            "ocr_text": br.ocr_text,
            "is_valid": bool(br.is_valid),
            "transformed": transformed_obj,
            "user": {
                "false_positive": False,
                "wrong_sfx": False,
                "undetected": False,
                "manual_translation": "",
            },
        })

    metadata_payload = {
        "schema_version": 1,
        "pipeline": job.pipeline,
        "source_hash": job.source_hash,
        "image": {
            "width": img_w,
            "height": img_h,
        },
        "regions": regions,
    }

    return ProcessingResult(
        job_id=job.job_id,
        status="completed",
        image_bytes=output_bytes,
        bubble_count=bubble_count,
        pipeline=job.pipeline,
        source_hash=job.source_hash,
        metadata_payload=metadata_payload,
    )


def _process_webtoon(job: ProcessingJob,
                     progress_cb: ProgressCallback | None = None) -> ProcessingResult:
    """Run the webtoon pipeline on image bytes."""
    from webtoon.processor import (
        cluster_and_find_bubbles,
        detect_text,
        load_page_from_memory as wt_load_page,
        render_page_to_bytes as wt_render_bytes,
        validate_and_translate,
    )

    img_cv, img_pil = decode_image_bytes(job.image_bytes)
    page = wt_load_page(img_cv, img_pil, name=job.job_id)

    _report(progress_cb, "detecting_text", "", 15)
    detect_text(page)

    _report(progress_cb, "detecting_bubbles", "", 30)
    cluster_and_find_bubbles(page)

    _report(progress_cb, "translating", "", 50)
    validate_and_translate(page)

    _report(progress_cb, "rendering", "", 90)
    output_bytes = wt_render_bytes(page)
    bubble_count = sum(1 for r in page.regions if r.english)

    img_w, img_h = page.img_pil.width, page.img_pil.height
    regions: list[dict[str, Any]] = []

    for idx, region in enumerate(page.regions):
        bubble = region.bubble
        transformed_obj = None
        if region.english:
            transformed_obj = {"kind": "text", "value": region.english}
        regions.append({
            "id": f"r{idx+1}",
            "kind": "bubble",
            "bbox": [int(v) for v in bubble.bbox],
            "bbox_norm": _norm_bbox(bubble.bbox, img_w, img_h),
            "ocr_text": bubble.combined_text,
            "is_valid": bool(region.is_valid),
            "transformed": transformed_obj,
            "user": {
                "false_positive": False,
                "wrong_sfx": False,
                "undetected": False,
                "manual_translation": "",
            },
        })

    # Persist SFX as editable regions too.
    for idx, det in enumerate(page.sfx_detections):
        bbox = det.bbox_rect
        regions.append({
            "id": f"sfx{idx+1}",
            "kind": "sfx",
            "bbox": [int(v) for v in bbox],
            "bbox_norm": _norm_bbox(bbox, img_w, img_h),
            "ocr_text": det.text,
            "is_valid": True,
            "transformed": {"kind": "text", "value": ""},
            "user": {
                "false_positive": False,
                "wrong_sfx": False,
                "undetected": False,
                "manual_translation": "",
            },
        })

    metadata_payload = {
        "schema_version": 1,
        "pipeline": job.pipeline,
        "source_hash": job.source_hash,
        "image": {
            "width": img_w,
            "height": img_h,
        },
        "regions": regions,
    }

    return ProcessingResult(
        job_id=job.job_id,
        status="completed",
        image_bytes=output_bytes,
        bubble_count=bubble_count,
        pipeline=job.pipeline,
        source_hash=job.source_hash,
        metadata_payload=metadata_payload,
    )
