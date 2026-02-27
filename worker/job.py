"""Job processing: dataclasses and pipeline routing."""

import logging
import time
from dataclasses import dataclass, field

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

VALID_PIPELINES = {"manga_translate", "manga_furigana", "webtoon"}


@dataclass
class ProcessingJob:
    job_id: str
    pipeline: str  # manga_translate, manga_furigana, webtoon
    image_bytes: bytes
    priority: str = "high"


@dataclass
class ProcessingResult:
    job_id: str
    status: str  # completed, failed, degraded
    image_bytes: bytes | None = None
    error: str = ""
    processing_time_ms: int = 0
    bubble_count: int = 0


def process_job(job: ProcessingJob) -> ProcessingResult:
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
            result = _process_manga(job)
        else:
            result = _process_webtoon(job)

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


def _process_manga(job: ProcessingJob) -> ProcessingResult:
    """Run the manga pipeline (furigana or translate) on image bytes."""
    img_cv, img_pil = decode_image_bytes(job.image_bytes)
    page = load_page_from_memory(img_cv, img_pil, name=job.job_id)

    mode = (PipelineMode.FURIGANA if job.pipeline == "manga_furigana"
            else PipelineMode.TRANSLATE)

    # Detection
    detect_page_bubbles(page)
    detect_page_text(page)

    # OCR
    for bubble_dict in page.bubbles_raw:
        br = ocr_bubble(page.img_pil, bubble_dict)
        page.bubble_results.append(br)

    # Transform
    transform_fn = (transform_furigana if mode == PipelineMode.FURIGANA
                    else transform_translate)
    for br in page.bubble_results:
        transform_fn(br)

    # Render to bytes
    output_bytes = render_page_to_bytes(page, mode)
    bubble_count = sum(1 for br in page.bubble_results
                       if br.transformed is not None)

    return ProcessingResult(
        job_id=job.job_id,
        status="completed",
        image_bytes=output_bytes,
        bubble_count=bubble_count,
    )


def _process_webtoon(job: ProcessingJob) -> ProcessingResult:
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

    detect_text(page)
    cluster_and_find_bubbles(page)
    validate_and_translate(page)

    output_bytes = wt_render_bytes(page)
    bubble_count = sum(1 for r in page.regions if r.english)

    return ProcessingResult(
        job_id=job.job_id,
        status="completed",
        image_bytes=output_bytes,
        bubble_count=bubble_count,
    )
