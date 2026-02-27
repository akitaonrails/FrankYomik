"""Job processing: dataclasses and pipeline routing."""

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
from PIL import Image

from pipeline.image_utils import decode_image_bytes, encode_image_pil
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


@dataclass
class ProcessingResult:
    job_id: str
    status: str  # completed, failed, degraded
    image_bytes: bytes | None = None
    error: str = ""
    processing_time_ms: int = 0
    bubble_count: int = 0


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
            )

        if job.pipeline.startswith("manga_"):
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
        )


def _report(cb: ProgressCallback | None, stage: str, detail: str, percent: int):
    """Call progress callback if provided."""
    if cb:
        try:
            cb(stage, detail, percent)
        except Exception:
            pass


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

    return ProcessingResult(
        job_id=job.job_id,
        status="completed",
        image_bytes=output_bytes,
        bubble_count=bubble_count,
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

    return ProcessingResult(
        job_id=job.job_id,
        status="completed",
        image_bytes=output_bytes,
        bubble_count=bubble_count,
    )
