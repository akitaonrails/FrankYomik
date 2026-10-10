"""Job processing: dataclasses and pipeline routing."""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pipeline.adapters.current import (
    PipelineDependencies,
    _config_value,
    _pipeline_mode,
    _webtoon_cleanup,
    _webtoon_detect,
    _webtoon_load_page,
    _webtoon_ocr,
    _webtoon_prepare_translation,
    _webtoon_render,
    _webtoon_translate,
    analyze_book_layout,
    cleanup_manga_page,
    clear_text_in_region,
    clear_text_strokes,
    compute_furigana_page_font_limits,
    decode_image_bytes,
    detect_page_bubbles,
    encode_image_pil,
    estimate_source_vertical_font_size,
    extract_bubble_mask_manga,
    furigana_annotate,
    load_page_from_memory,
    ocr_bubble,
    ocr_image,
    read_book_columns,
    render_book_furigana,
    render_cleaned_manga_page,
    render_english,
    render_furigana_vertical,
    transform_furigana,
    translate,
    translate_page,
)
from pipeline.current_backend import cleanup_for, language_for, provenance_for
from pipeline.detection import SemanticDetection, validate_detection_collection
from pipeline.document import (
    PAGE_DOCUMENT_SCHEMA_VERSION,
    VALID_PIPELINES,
    BoundingBox,
    CleanupMetadata,
    MaskReference,
    OcrAlternative,
    OcrResult,
    PageDocument,
    PageRegion,
    Polygon,
    Provenance,
    RenderMetadata,
    TransformResult,
    TranslationResult,
    UserEdit,
    make_region_id,
)
from pipeline.protocols import PipelineRuntime
from pipeline.revision import DEFAULT_PIPELINE_REVISION
from pipeline.translation import (
    MANGA_PAGE_STYLE,
    WEBTOON_PAGE_STYLE,
    PageTranslationRequest,
    TranslationItem,
)

log = logging.getLogger(__name__)

# Type for progress callback: (stage, detail, percent)
ProgressCallback = Callable[[str, str, int], None]


def production_dependencies() -> PipelineDependencies:
    """Build the production bundle without importing any concrete backend."""
    return PipelineDependencies(
        decode_image=decode_image_bytes,
        encode_image=encode_image_pil,
        analyze_layout=analyze_book_layout,
        read_columns=read_book_columns,
        render_book=render_book_furigana,
        extract_manga_mask=extract_bubble_mask_manga,
        annotate=furigana_annotate,
        clear_region=clear_text_in_region,
        clear_strokes=clear_text_strokes,
        ocr_prose=ocr_image,
        create_manga_page=load_page_from_memory,
        detect_manga=detect_page_bubbles,
        ocr_manga=ocr_bubble,
        cleanup_manga=cleanup_manga_page,
        render_manga=render_cleaned_manga_page,
        transform_furigana=transform_furigana,
        translate_text=translate,
        translate_page=translate_page,
        furigana_font_limits=compute_furigana_page_font_limits,
        estimate_source_font=estimate_source_vertical_font_size,
        render_translation=render_english,
        render_furigana=render_furigana_vertical,
        pipeline_mode=_pipeline_mode,
        config_value=_config_value,
        create_webtoon_page=_webtoon_load_page,
        detect_webtoon=_webtoon_detect,
        ocr_webtoon=_webtoon_ocr,
        translate_webtoon=_webtoon_translate,
        prepare_webtoon_translation=_webtoon_prepare_translation,
        cleanup_webtoon=_webtoon_cleanup,
        render_webtoon=_webtoon_render,
        provenance_for=provenance_for,
        language_for=language_for,
        cleanup_for=cleanup_for,
    )


@dataclass
class ProcessingJob:
    job_id: str
    pipeline: str  # manga_translate, manga_furigana, book_furigana, webtoon
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
    pipeline_revision: str = DEFAULT_PIPELINE_REVISION


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
    pipeline_revision: str = DEFAULT_PIPELINE_REVISION
    # "prose" when the page is typeset text rather than artwork. The manga
    # pipelines report it so a client can tell that a book needs book_furigana
    # without inferring it from a render that came back wrong.
    page_kind: str = ""
    cacheable: bool = True


def process_job(job: ProcessingJob,
                progress_cb: ProgressCallback | None = None,
                dependencies: PipelineRuntime | None = None,
                ) -> ProcessingResult:
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
                pipeline_revision=job.pipeline_revision,
            )

        deps = dependencies or production_dependencies()
        if job.rerender_from_metadata:
            result = _rerender_from_metadata(job, progress_cb, deps)
        elif job.pipeline == "book_furigana":
            result = _process_book(job, progress_cb, deps)
        elif job.pipeline.startswith("manga_"):
            result = _process_manga(job, progress_cb, deps)
        else:
            result = _process_webtoon(job, progress_cb, deps)

        elapsed_ms = int((time.monotonic() - start) * 1000)
        result.processing_time_ms = elapsed_ms
        result.pipeline_revision = job.pipeline_revision
        return result

    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        log.exception("Job %s failed", job.job_id)
        return ProcessingResult(
            job_id=job.job_id,
            status="failed",
            error=str(e),
            processing_time_ms=elapsed_ms,
            pipeline=job.pipeline,
            source_hash=job.source_hash,
            pipeline_revision=job.pipeline_revision,
        )


def _report(cb: ProgressCallback | None, stage: str, detail: str, percent: int):
    """Call progress callback if provided."""
    if cb:
        try:
            cb(stage, detail, percent)
        except Exception:
            log.warning("Progress callback failed", exc_info=True)


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
    translation = region.get("translation")
    if isinstance(translation, dict):
        value = translation.get("text")
        if isinstance(value, str):
            return value
    transform = region.get("transform")
    if isinstance(transform, dict):
        return transform.get("value")
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


def _region_source_font_size(region: dict[str, Any]) -> int | None:
    value = region.get("source_font_size")
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def _region_ocr_text(region: dict[str, Any]) -> str:
    ocr = region.get("ocr")
    if isinstance(ocr, dict) and isinstance(ocr.get("text"), str):
        return ocr["text"]
    value = region.get("ocr_text")
    return value if isinstance(value, str) else ""


def _job_source_hash(job: ProcessingJob) -> str:
    return job.source_hash or hashlib.sha256(job.image_bytes).hexdigest()


def _page_id(job: ProcessingJob) -> str:
    return f"{job.pipeline}:{_job_source_hash(job)[:16]}"


def _next_region_id(
    page_id: str,
    kind: str,
    bbox: BoundingBox,
    collisions: dict[str, int],
) -> str:
    """Return a revision-scoped geometry ID with deterministic duplicate suffixes."""
    base = make_region_id(page_id, kind, bbox)
    occurrence = collisions.get(base, 0) + 1
    collisions[base] = occurrence
    return base if occurrence == 1 else f"{base}-{occurrence}"


def _provenance(
    deps: PipelineRuntime, pipeline: str, stage: str, revision: str,
) -> Provenance:
    if deps.provenance_for is None:
        raise ValueError("pipeline dependencies must describe backend provenance")
    return deps.provenance_for(pipeline, stage, revision)


def _language(deps: PipelineRuntime, pipeline: str) -> str:
    if deps.language_for is None:
        raise ValueError("pipeline dependencies must describe source language")
    return deps.language_for(pipeline)


def _cleanup_method(
    deps: PipelineRuntime, pipeline: str, kind: str,
) -> str | None:
    if deps.cleanup_for is None:
        raise ValueError("pipeline dependencies must describe cleanup policy")
    return deps.cleanup_for(pipeline, kind)


def _build_region(
    page_id: str,
    collisions: dict[str, int],
    kind: str,
    bbox: tuple[int, int, int, int] | list[int],
    reading_order: int,
    ocr_text: str,
    is_valid: bool,
    detection_provenance: Provenance,
    ocr_provenance: Provenance,
    render_provenance: Provenance,
    source_language: str,
    translation_provenance: Provenance,
    transform_provenance: Provenance,
    cleanup_provenance: Provenance,
    transformed: dict[str, Any] | None = None,
    source_font_size: int | None = None,
    target_language: str | None = None,
    confidence: float | None = None,
    cleanup_method: str | None = None,
    vertical: bool = False,
    region_id: str | None = None,
    parent_id: str | None = None,
    detector_label: str | None = None,
    polygon: Polygon | None = None,
    mask: MaskReference | None = None,
    ocr_confidence: float | None = None,
    ocr_alternatives: tuple[OcrAlternative, ...] = (),
    ocr_routing_reason: str | None = None,
) -> PageRegion:
    """Map current pipeline output into the normalized stage contract."""
    box = BoundingBox.from_value(bbox)
    translation = None
    transform = None
    if transformed is not None:
        transform_kind = str(transformed.get("kind") or "text")
        value = transformed.get("value")
        if transform_kind == "text" and target_language is not None:
            translation = TranslationResult(
                text=value if isinstance(value, str) else "",
                target_language=target_language,
                source_language=source_language,
                provenance=translation_provenance,
            )
        else:
            transform = TransformResult(
                kind=transform_kind,
                value=value,
                provenance=transform_provenance,
            )

    cleanup = None
    if cleanup_method is not None:
        selected_cleanup_provenance = cleanup_provenance
        if cleanup_method.startswith("uniform_fill"):
            selected_cleanup_provenance = Provenance(
                backend="deterministic-uniform-fill",
                revision=cleanup_provenance.revision,
                config_hash=cleanup_provenance.config_hash,
            )
        elif cleanup_method == "stroke_clear":
            selected_cleanup_provenance = Provenance(
                backend="deterministic-stroke-clear",
                revision=cleanup_provenance.revision,
                config_hash=cleanup_provenance.config_hash,
            )
        elif cleanup_method == "region_fill":
            selected_cleanup_provenance = Provenance(
                backend="deterministic-region-fill",
                revision=cleanup_provenance.revision,
                config_hash=cleanup_provenance.config_hash,
            )
        elif cleanup_method == "lama_tiles":
            selected_cleanup_provenance = Provenance(
                backend="simple-lama-inpainting",
                revision=cleanup_provenance.revision,
                model=cleanup_provenance.model,
                config_hash=cleanup_provenance.config_hash,
            )
        cleanup = CleanupMetadata(
            cleanup_method,
            selected_cleanup_provenance,
        )

    return PageRegion(
        id=(region_id
            if region_id is not None
            else _next_region_id(page_id, kind, box, collisions)),
        kind=kind,
        bbox=box,
        reading_order=reading_order,
        confidence=confidence,
        polygon=polygon,
        mask=mask,
        parent_id=parent_id,
        detector_label=detector_label,
        detection_provenance=detection_provenance,
        ocr=OcrResult(
            text=ocr_text,
            language=source_language,
            valid=is_valid,
            confidence=ocr_confidence,
            provenance=ocr_provenance,
            alternatives=ocr_alternatives,
            routing_reason=ocr_routing_reason,
        ),
        translation=translation,
        transform=transform,
        cleanup=cleanup,
        render=RenderMetadata(
            bbox=box,
            provenance=render_provenance,
            vertical=vertical,
        ),
        source_font_size=source_font_size,
        user=UserEdit(),
    )


def _semantic_region_ids(
    page_id: str, detections: list[SemanticDetection],
) -> dict[str, str]:
    """Map detector-local identities to stable PageDocument identities."""
    validate_detection_collection(detections)
    return {
        item.id: make_region_id(
            page_id, item.kind, item.bbox, item.id,
        )
        for item in detections
    }


def _semantic_mask_reference(
    region_id: str, detection: SemanticDetection, width: int, height: int,
    encoding: str = "connected-component-v1",
) -> MaskReference | None:
    if detection.mask is None:
        return None
    return MaskReference(
        reference=f"derived://source/{region_id}",
        encoding=encoding,
        width=width,
        height=height,
    )


def _append_unprocessed_semantic_regions(
    regions: list[PageRegion],
    detections: list[SemanticDetection],
    represented_ids: set[str],
    ids: dict[str, str],
    width: int,
    height: int,
    provenance: Provenance,
    mask_encoding: str = "connected-component-v1",
) -> None:
    """Persist geometry which current OCR/render stages do not yet consume."""
    used_orders = {region.reading_order for region in regions}
    for detection in detections:
        if detection.id in represented_ids:
            continue
        reading_order = detection.reading_order
        if reading_order in used_orders:
            raise ValueError(
                f"semantic reading order {reading_order} was already assigned"
            )
        regions.append(PageRegion(
            id=ids[detection.id],
            kind=detection.kind,
            bbox=detection.bbox,
            reading_order=reading_order,
            confidence=detection.confidence,
            polygon=detection.polygon,
            mask=_semantic_mask_reference(
                ids[detection.id], detection, width, height,
                mask_encoding,
            ),
            parent_id=(None if detection.parent_id is None
                       else ids[detection.parent_id]),
            detector_label=detection.raw_label,
            detection_provenance=provenance,
        ))
        used_orders.add(reading_order)


class _ReadingOrderAllocator:
    """Reserve detector order and place compatibility-only records after it."""

    def __init__(self, detections: list[SemanticDetection]) -> None:
        validate_detection_collection(detections)
        self._used: set[int] = set()
        self._next_extra = max(
            (item.reading_order for item in detections), default=-1,
        ) + 1

    def semantic(self, detection: SemanticDetection) -> int:
        order = detection.reading_order
        if order in self._used:
            raise ValueError(f"semantic reading order {order} was already assigned")
        self._used.add(order)
        return order

    def extra(self) -> int:
        while self._next_extra in self._used:
            self._next_extra += 1
        order = self._next_extra
        self._used.add(order)
        self._next_extra += 1
        return order


def _build_metadata_payload(
    job: ProcessingJob,
    regions: list[PageRegion],
    img_w: int,
    img_h: int,
    stage_provenance: dict[str, Provenance],
) -> dict[str, Any]:
    """Build and validate the PageDocument returned with every result."""
    return PageDocument(
        schema_version=PAGE_DOCUMENT_SCHEMA_VERSION,
        page_id=_page_id(job),
        pipeline=job.pipeline,
        pipeline_revision=job.pipeline_revision,
        source_hash=_job_source_hash(job),
        width=img_w,
        height=img_h,
        regions=tuple(regions),
        stage_provenance=stage_provenance,
    ).to_dict()


def _rerender_from_metadata(job: ProcessingJob,
                            progress_cb: ProgressCallback | None = None,
                            deps: PipelineRuntime | None = None,
                            ) -> ProcessingResult:
    """Re-render using metadata only (skip detection/OCR/translation).

    Only the manga and webtoon pipelines render editable regions; a book page
    is annotated in the gutters and has nothing to replay.
    """
    deps = deps or production_dependencies()
    if job.pipeline == "book_furigana":
        return ProcessingResult(
            job_id=job.job_id,
            status="failed",
            error="book_furigana pages cannot be re-rendered from metadata",
            pipeline=job.pipeline,
            source_hash=job.source_hash,
            pipeline_revision=job.pipeline_revision,
        )
    _report(progress_cb, "rerender", "loading metadata", 15)
    payload = job.metadata_payload
    if not payload or not isinstance(payload.get("regions"), list):
        return ProcessingResult(
            job_id=job.job_id,
            status="failed",
            error="Rerender requires metadata with regions",
            pipeline=job.pipeline,
            source_hash=job.source_hash,
            pipeline_revision=job.pipeline_revision,
        )
    schema_version = payload.get("schema_version", 1)
    if (not isinstance(schema_version, int) or isinstance(schema_version, bool)
            or schema_version not in {1, PAGE_DOCUMENT_SCHEMA_VERSION}):
        raise ValueError("unsupported metadata schema version")
    document = None
    if schema_version == PAGE_DOCUMENT_SCHEMA_VERSION:
        document = PageDocument.from_dict(payload)
        if document.pipeline != job.pipeline:
            raise ValueError("metadata pipeline mismatch")
        if document.pipeline_revision != job.pipeline_revision:
            raise ValueError("metadata pipeline revision mismatch")
        if job.source_hash and document.source_hash != job.source_hash:
            raise ValueError("metadata source hash mismatch")
    regions = payload["regions"]

    _report(progress_cb, "rerender", "preparing image", 25)
    img_cv, img_pil = deps.decode_image(job.image_bytes)
    img_out = img_pil.copy()
    img_w, img_h = img_out.width, img_out.height
    if document is not None and (document.width, document.height) != (img_w, img_h):
        raise ValueError("metadata image dimensions do not match source image")

    _report(progress_cb, "rerender", "drawing edits", 50)

    # Page-wide font target for consistent sizing.
    base_font_size = None
    if job.pipeline != "manga_furigana":
        base_font_size = max(
            deps.config_value("MIN_FONT_SIZE"),
            img_h // deps.config_value("EN_PAGE_FONT_DIVISOR"),
        )

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
            mask = deps.extract_manga_mask(img_cv, bbox)

        if job.pipeline == "manga_furigana":
            if manual:
                transformed_val = deps.annotate(manual)
            if not isinstance(transformed_val, list):
                continue
            source_font_size = _region_source_font_size(region)
            if source_font_size is None:
                source_font_size = deps.estimate_source_font(
                    img_pil,
                    bbox,
                    _region_ocr_text(region) or manual,
                    mask=mask,
                )
            render_items.append({"bbox": bbox, "mask": mask, "kind": kind,
                                 "mode": "furigana", "value": transformed_val,
                                 "source_font_size": source_font_size})
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
            deps.clear_region(img_out, item["bbox"])
        else:
            deps.clear_strokes(img_out, item["bbox"], mask=item["mask"])

    # Pass 2: Render
    page_font_cap, source_outlier_threshold, page_font_floor = (
        deps.furigana_font_limits([
            int(item["source_font_size"])
            for item in render_items
            if item["mode"] == "furigana"
            and item.get("source_font_size") is not None
        ])
    )
    applied = 0
    for item in render_items:
        if item["mode"] == "furigana":
            deps.render_furigana(
                img_out, item["bbox"], item["value"],
                mask=item["mask"],
                source_font_size=item.get("source_font_size"),
                page_font_cap=page_font_cap,
                source_outlier_threshold=source_outlier_threshold,
                page_font_floor=page_font_floor,
                layout_img=img_pil,
            )
        else:
            deps.render_translation(
                img_out, item["bbox"], item["value"],
                base_font_size=base_font_size, mask=item["mask"])
        applied += 1

    _report(progress_cb, "rerender", "encoding", 95)
    output_bytes = deps.encode_image(img_out)
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
                   progress_cb: ProgressCallback | None = None,
                   deps: PipelineRuntime | None = None,
                   ) -> ProcessingResult:
    """Run the manga pipeline (furigana or translate) on image bytes."""
    deps = deps or production_dependencies()
    img_cv, img_pil = deps.decode_image(job.image_bytes)
    page = deps.create_manga_page(img_cv, img_pil, name=job.job_id)

    # Cheap, and the only reliable way to tell a novel from artwork: prose is
    # many columns of one width, manga is not.
    page_kind = "prose" if deps.analyze_layout(img_pil).is_prose else "artwork"

    mode_name = "furigana" if job.pipeline == "manga_furigana" else "translate"
    mode = deps.pipeline_mode(mode_name)

    # Detection (RT-DETR-v2 detects bubbles + artwork text in one pass)
    _report(progress_cb, "detecting_bubbles", "", 10)
    deps.detect_manga(page)

    # OCR
    total = len(page.bubbles_raw)
    for i, bubble_dict in enumerate(page.bubbles_raw):
        _report(progress_cb, "ocr", f"{i+1}/{total} bubbles", 20 + int(40 * (i+1) / max(total, 1)))
        br = deps.ocr_manga(page.img_pil, bubble_dict)
        page.bubble_results.append(br)

    # Assign the exact PageDocument identities before translation. Provider
    # responses are always joined by these IDs, never by list position.
    page_id = _page_id(job)
    semantic_detections: list[SemanticDetection] = list(
        getattr(page, "semantic_detections", []),
    )
    semantic_by_id = {item.id: item for item in semantic_detections}
    semantic_ids = _semantic_region_ids(page_id, semantic_detections)
    planned_collisions: dict[str, int] = {}
    planned_orders = _ReadingOrderAllocator(semantic_detections)
    region_plans: list[tuple[str, int, str]] = []
    for idx, br in enumerate(page.bubble_results):
        raw = page.bubbles_raw[idx] if idx < len(page.bubbles_raw) else {}
        semantic_id = raw.get("semantic_id") if isinstance(raw, dict) else None
        semantic = semantic_by_id.get(semantic_id)
        kind = (semantic.kind if semantic is not None else
                "artwork_text" if br.is_artwork_text else "bubble")
        region_id = (semantic_ids[semantic.id] if semantic is not None else
                     _next_region_id(
                         page_id, kind, BoundingBox.from_value(br.bbox),
                         planned_collisions,
                     ))
        order = (planned_orders.semantic(semantic) if semantic is not None
                 else planned_orders.extra())
        region_plans.append((region_id, order, kind))

    # Transform
    total_br = len(page.bubble_results)
    if mode_name == "furigana":
        _report(progress_cb, "translating", "", 65)
        for i, br in enumerate(page.bubble_results):
            _report(progress_cb, "translating", f"{i+1}/{total_br} bubbles",
                    65 + int(25 * (i+1) / max(total_br, 1)))
            deps.transform_furigana(br)
    else:
        translatable = [(i, br) for i, br in enumerate(page.bubble_results)
                        if br.is_valid]
        _report(progress_cb, "translating", f"{len(translatable)} bubbles", 65)
        if translatable:
            request = PageTranslationRequest(
                source_language="ja",
                target_language=job.target_lang,
                items=tuple(sorted((
                    TranslationItem(
                        region_plans[index][0], region_plans[index][1],
                        region_plans[index][2], br.ocr_text,
                    )
                    for index, br in translatable
                ), key=lambda item: item.reading_order)),
                style=MANGA_PAGE_STYLE,
            )
            translated = deps.translate_page(request).by_id()
            expected_ids = {item.id for item in request.items}
            if set(translated) != expected_ids:
                raise RuntimeError("page translator returned incomplete IDs")
            for done, (index, br) in enumerate(translatable, start=1):
                br.transformed = translated[region_plans[index][0]]
                _report(progress_cb, "translating",
                        f"{done}/{len(translatable)} bubbles",
                        65 + int(25 * done / len(translatable)))

    _report(progress_cb, "cleanup", "", 92)
    deps.cleanup_manga(page, mode)

    _report(progress_cb, "rendering", "", 95)
    output_bytes = deps.render_manga(page, mode)
    bubble_count = sum(1 for br in page.bubble_results
                       if br.transformed is not None)

    img_w, img_h = page.img_pil.width, page.img_pil.height
    collisions: dict[str, int] = {}
    revision = job.pipeline_revision
    detection_provenance = _provenance(
        deps, job.pipeline, "detection", revision)
    ocr_provenance = _provenance(deps, job.pipeline, "ocr", revision)
    translation_provenance = _provenance(
        deps, job.pipeline, "translation", revision)
    transform_provenance = _provenance(
        deps, job.pipeline, "transform", revision)
    cleanup_provenance = _provenance(
        deps, job.pipeline, "cleanup", revision)
    render_provenance = _provenance(deps, job.pipeline, "render", revision)
    source_language = _language(deps, job.pipeline)
    regions: list[PageRegion] = []
    represented_semantic_ids: set[str] = set()
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
        raw = page.bubbles_raw[idx] if idx < len(page.bubbles_raw) else {}
        semantic_id = raw.get("semantic_id") if isinstance(raw, dict) else None
        semantic = semantic_by_id.get(semantic_id)
        if semantic is not None:
            represented_semantic_ids.add(semantic.id)
        region_id, reading_order, kind = region_plans[idx]
        confidence = raw.get("score") if isinstance(raw, dict) else None
        if not isinstance(confidence, (int, float)):
            confidence = None
        regions.append(_build_region(
            page_id=page_id,
            collisions=collisions,
            kind=kind,
            bbox=br.bbox,
            reading_order=reading_order,
            ocr_text=br.ocr_text,
            is_valid=bool(br.is_valid),
            detection_provenance=detection_provenance,
            ocr_provenance=(getattr(br, "ocr_provenance", None)
                            or ocr_provenance),
            render_provenance=render_provenance,
            source_language=source_language,
            translation_provenance=translation_provenance,
            transform_provenance=transform_provenance,
            cleanup_provenance=cleanup_provenance,
            transformed=transformed_obj,
            source_font_size=br.source_font_size,
            target_language=(job.target_lang
                             if mode_name == "translate" else None),
            confidence=None if confidence is None else float(confidence),
            cleanup_method=(getattr(br, "cleanup_method", None)
                            or _cleanup_method(deps, job.pipeline, kind)),
            vertical=mode_name == "furigana",
            region_id=region_id,
            parent_id=(None if semantic is None or semantic.parent_id is None
                       else semantic_ids[semantic.parent_id]),
            detector_label=(None if semantic is None else semantic.raw_label),
            polygon=(None if semantic is None else semantic.polygon),
            mask=(None if semantic is None else _semantic_mask_reference(
                semantic_ids[semantic.id], semantic, img_w, img_h,
            )),
            ocr_confidence=getattr(br, "ocr_confidence", None),
            ocr_alternatives=getattr(br, "ocr_alternatives", ()),
            ocr_routing_reason=getattr(br, "ocr_routing_reason", None),
        ))

    _append_unprocessed_semantic_regions(
        regions,
        semantic_detections,
        represented_semantic_ids,
        semantic_ids,
        img_w,
        img_h,
        detection_provenance,
    )

    return ProcessingResult(
        job_id=job.job_id,
        status="completed",
        image_bytes=output_bytes,
        bubble_count=bubble_count,
        pipeline=job.pipeline,
        source_hash=job.source_hash,
        metadata_payload=_build_metadata_payload(
            job, regions, img_w, img_h,
            {
                "detection": detection_provenance,
                "ocr": ocr_provenance,
                ("transform" if mode_name == "furigana"
                 else "translation"): (
                    transform_provenance if mode_name == "furigana"
                    else translation_provenance
                ),
                "cleanup": cleanup_provenance,
                "render": render_provenance,
            },
        ),
        cacheable=not any(
            (getattr(item, "ocr_degraded", False)
             or getattr(item, "cleanup_degraded", False))
            for item in page.bubble_results
        ),
        page_kind=page_kind,
    )


def _process_book(job: ProcessingJob,
                  progress_cb: ProgressCallback | None = None,
                  deps: PipelineRuntime | None = None,
                  ) -> ProcessingResult:
    """Annotate a rasterised prose page with furigana.

    Nothing on the page is redrawn: the columns are read, MeCab supplies the
    readings, and the kana are added in the gutters where vertical Japanese
    puts ruby anyway.
    """
    deps = deps or production_dependencies()
    _, img_pil = deps.decode_image(job.image_bytes)

    _report(progress_cb, "analyzing_layout", "", 10)
    layout = deps.analyze_layout(img_pil)
    if not layout.is_prose:
        # Manga through this pipeline would produce furigana in the artwork.
        return ProcessingResult(
            job_id=job.job_id,
            status="failed",
            error=(f"page does not look like typeset prose "
                   f"({len(layout.columns)} text columns found); "
                   f"use a manga pipeline for this book"),
            pipeline=job.pipeline,
            source_hash=job.source_hash,
        )

    def on_column(done: int, total: int) -> None:
        _report(progress_cb, "ocr", f"{done}/{total} columns",
                20 + int(65 * done / max(total, 1)))

    readings = deps.read_columns(
        img_pil, layout,
        ocr=deps.ocr_prose,
        annotate=deps.annotate,
        progress=on_column,
    )

    _report(progress_cb, "rendering", "", 95)
    rendered = deps.render_book(
        img_pil, layout, readings,
        scale=deps.config_value("BOOK_RENDER_SCALE"),
    )
    output_bytes = deps.encode_image(rendered)

    img_w, img_h = img_pil.width, img_pil.height
    page_id = _page_id(job)
    collisions: dict[str, int] = {}
    revision = job.pipeline_revision
    detection_provenance = _provenance(
        deps, job.pipeline, "detection", revision)
    ocr_provenance = _provenance(deps, job.pipeline, "ocr", revision)
    translation_provenance = _provenance(
        deps, job.pipeline, "translation", revision)
    transform_provenance = _provenance(
        deps, job.pipeline, "transform", revision)
    cleanup_provenance = _provenance(
        deps, job.pipeline, "cleanup", revision)
    render_provenance = _provenance(deps, job.pipeline, "render", revision)
    regions = [
        _build_region(
            page_id=page_id,
            collisions=collisions,
            kind="text_column",
            bbox=(reading.column.x0, reading.column.y0,
                  reading.column.x1, reading.column.y1),
            reading_order=reading.index,
            ocr_text=reading.text,
            is_valid=bool(reading.text),
            detection_provenance=detection_provenance,
            ocr_provenance=ocr_provenance,
            render_provenance=render_provenance,
            source_language=_language(deps, job.pipeline),
            translation_provenance=translation_provenance,
            transform_provenance=transform_provenance,
            cleanup_provenance=cleanup_provenance,
            transformed={
                "kind": "furigana_ruby",
                "value": [
                    {"reading": r.reading, "y0": r.y0, "y1": r.y1}
                    for r in reading.ruby
                ],
            },
            vertical=True,
        )
        for reading in readings
    ]

    return ProcessingResult(
        job_id=job.job_id,
        status="completed",
        image_bytes=output_bytes,
        bubble_count=sum(len(r.ruby) for r in readings),
        pipeline=job.pipeline,
        source_hash=job.source_hash,
        metadata_payload=_build_metadata_payload(
            job, regions, img_w, img_h,
            {
                "detection": detection_provenance,
                "ocr": ocr_provenance,
                "transform": transform_provenance,
                "render": render_provenance,
            },
        ),
    )


def _process_webtoon(job: ProcessingJob,
                     progress_cb: ProgressCallback | None = None,
                     deps: PipelineRuntime | None = None,
                     ) -> ProcessingResult:
    """Run the webtoon pipeline on image bytes."""
    deps = deps or production_dependencies()
    img_cv, img_pil = deps.decode_image(job.image_bytes)
    page = deps.create_webtoon_page(img_cv, img_pil, name=job.job_id)

    _report(progress_cb, "detecting_bubbles", "", 20)
    deps.detect_webtoon(page)

    _report(progress_cb, "ocr", "", 35)
    deps.ocr_webtoon(page)

    _report(progress_cb, "translating", "", 50)
    deps.prepare_webtoon_translation(page)

    page_id = _page_id(job)
    semantic_detections = list(getattr(page, "semantic_detections", []))
    semantic_by_id = {item.id: item for item in semantic_detections}
    semantic_ids = _semantic_region_ids(page_id, semantic_detections)
    planned_collisions: dict[str, int] = {}
    planned_orders = _ReadingOrderAllocator(semantic_detections)
    planned_represented: set[str] = set()
    webtoon_region_plans: list[tuple[str, int, str, SemanticDetection | None]] = []
    for region in page.regions:
        bubble = region.bubble
        semantic = semantic_by_id.get(getattr(bubble, "semantic_id", None))
        is_first = semantic is not None and semantic.id not in planned_represented
        if is_first:
            planned_represented.add(semantic.id)
        kind = semantic.kind if is_first else "bubble"
        region_id = (semantic_ids[semantic.id] if is_first else
                     _next_region_id(
                         page_id, kind, BoundingBox.from_value(bubble.bbox),
                         planned_collisions,
                     ))
        order = (planned_orders.semantic(semantic) if is_first else
                 planned_orders.extra())
        webtoon_region_plans.append((region_id, order, kind,
                                     semantic if is_first else None))

    sfx_plans: list[tuple[str, int, SemanticDetection | None]] = []
    for detection in page.sfx_detections:
        semantic = next((
            item for item in semantic_detections
            if item.kind == "sfx" and item.id not in planned_represented
            and item.bbox.x1 <= (detection.bbox_rect[0] + detection.bbox_rect[2]) / 2 < item.bbox.x2
            and item.bbox.y1 <= (detection.bbox_rect[1] + detection.bbox_rect[3]) / 2 < item.bbox.y2
        ), None)
        if semantic is not None:
            planned_represented.add(semantic.id)
        region_id = (semantic_ids[semantic.id] if semantic is not None else
                     _next_region_id(
                         page_id, "sfx", BoundingBox.from_value(detection.bbox_rect),
                         planned_collisions,
                     ))
        order = (planned_orders.semantic(semantic) if semantic is not None else
                 planned_orders.extra())
        sfx_plans.append((region_id, order, semantic))

    items = [
        TranslationItem(plan[0], plan[1], plan[2], region.source_text)
        for region, plan in zip(page.regions, webtoon_region_plans, strict=True)
        if region.is_valid
    ]
    eligible_sfx = getattr(
        page, "sfx_translation_eligible",
        [True for _ in page.sfx_detections],
    )
    items.extend(
        TranslationItem(plan[0], plan[1], "sfx", detection.text)
        for detection, plan, eligible in zip(
            page.sfx_detections, sfx_plans, eligible_sfx, strict=True,
        )
        if eligible
    )
    if items:
        request = PageTranslationRequest(
            source_language="ko",
            target_language=job.target_lang,
            items=tuple(sorted(items, key=lambda item: item.reading_order)),
            style=WEBTOON_PAGE_STYLE,
        )
        translated = deps.translate_page(request).by_id()
        expected_ids = {item.id for item in request.items}
        if set(translated) != expected_ids:
            raise RuntimeError("page translator returned incomplete IDs")
        for region, plan in zip(page.regions, webtoon_region_plans, strict=True):
            if region.is_valid:
                region.english = translated[plan[0]]
        for index, (plan, eligible) in enumerate(zip(
            sfx_plans, eligible_sfx, strict=True,
        )):
            if eligible:
                page.sfx_translations[index] = translated[plan[0]].upper()

    _report(progress_cb, "cleanup", "", 80)
    deps.cleanup_webtoon(page)

    _report(progress_cb, "rendering", "", 90)
    output_bytes = deps.render_webtoon(page)
    bubble_count = sum(1 for r in page.regions if r.english)

    img_w, img_h = page.img_pil.width, page.img_pil.height
    collisions: dict[str, int] = {}
    revision = job.pipeline_revision
    detection_provenance = _provenance(
        deps, job.pipeline, "detection", revision)
    ocr_provenance = _provenance(deps, job.pipeline, "ocr", revision)
    translation_provenance = _provenance(
        deps, job.pipeline, "translation", revision)
    transform_provenance = _provenance(
        deps, job.pipeline, "transform", revision)
    cleanup_provenance = _provenance(
        deps, job.pipeline, "cleanup", revision)
    render_provenance = _provenance(deps, job.pipeline, "render", revision)
    source_language = _language(deps, job.pipeline)
    regions: list[PageRegion] = []
    represented_semantic_ids: set[str] = set()

    for idx, region in enumerate(page.regions):
        bubble = region.bubble
        region_id, reading_order, kind, semantic = webtoon_region_plans[idx]
        is_first_semantic_region = semantic is not None
        if is_first_semantic_region:
            represented_semantic_ids.add(semantic.id)
        transformed_obj = None
        if region.english:
            transformed_obj = {"kind": "text", "value": region.english}
        regions.append(_build_region(
            page_id=page_id,
            collisions=collisions,
            kind=kind,
            bbox=bubble.bbox,
            reading_order=reading_order,
            ocr_text=(region.source_text or bubble.combined_text),
            is_valid=bool(region.is_valid),
            detection_provenance=detection_provenance,
            ocr_provenance=ocr_provenance,
            render_provenance=render_provenance,
            source_language=source_language,
            translation_provenance=translation_provenance,
            transform_provenance=transform_provenance,
            cleanup_provenance=cleanup_provenance,
            transformed=transformed_obj,
            target_language=job.target_lang,
            cleanup_method=(getattr(bubble, "cleanup_method", None)
                            or _cleanup_method(deps, job.pipeline, "bubble")),
            region_id=region_id,
            detector_label=(semantic.raw_label
                            if is_first_semantic_region else None),
            polygon=(semantic.polygon if is_first_semantic_region else None),
            mask=(_semantic_mask_reference(
                semantic_ids[semantic.id], semantic, img_w, img_h,
                "webtoon-boundary-v1",
            ) if is_first_semantic_region else None),
        ))

    # Persist SFX as editable regions too.
    for idx, det in enumerate(page.sfx_detections):
        region_id, reading_order, semantic = sfx_plans[idx]
        if semantic is not None:
            represented_semantic_ids.add(semantic.id)
        regions.append(_build_region(
            page_id=page_id,
            collisions=collisions,
            kind="sfx",
            bbox=det.bbox_rect,
            reading_order=reading_order,
            ocr_text=det.text,
            is_valid=True,
            detection_provenance=detection_provenance,
            ocr_provenance=ocr_provenance,
            render_provenance=render_provenance,
            source_language=source_language,
            translation_provenance=translation_provenance,
            transform_provenance=transform_provenance,
            cleanup_provenance=cleanup_provenance,
            transformed=(
                {"kind": "text", "value": page.sfx_translations[idx]}
                if page.sfx_translations[idx] else None
            ),
            target_language=(job.target_lang
                             if page.sfx_translations[idx] else None),
            cleanup_method=_cleanup_method(deps, job.pipeline, "sfx"),
            region_id=region_id,
            parent_id=(None if semantic is None or semantic.parent_id is None
                       else semantic_ids[semantic.parent_id]),
            detector_label=(None if semantic is None else semantic.raw_label),
            polygon=(None if semantic is None else semantic.polygon),
        ))

    _append_unprocessed_semantic_regions(
        regions,
        semantic_detections,
        represented_semantic_ids,
        semantic_ids,
        img_w,
        img_h,
        detection_provenance,
        "webtoon-boundary-v1",
    )

    return ProcessingResult(
        job_id=job.job_id,
        status="completed",
        image_bytes=output_bytes,
        bubble_count=bubble_count,
        pipeline=job.pipeline,
        source_hash=job.source_hash,
        metadata_payload=_build_metadata_payload(
            job, regions, img_w, img_h,
            {
                "detection": detection_provenance,
                "ocr": ocr_provenance,
                "translation": translation_provenance,
                "cleanup": cleanup_provenance,
                "render": render_provenance,
            },
        ),
        cacheable=not any(
            getattr(region.bubble, "cleanup_degraded", False)
            for region in page.regions
        ),
    )
