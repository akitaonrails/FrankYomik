"""Unified pipeline stages for manga processing."""

import logging
import os
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
from PIL import Image

from pipeline.detection import SemanticDetection, legacy_processing_regions
from pipeline.document import Polygon

from .bubble_detector import detect_semantic_regions, extract_bubble_mask_manga
from .config import EN_PAGE_FONT_DIVISOR, MANGA_INPAINT_ENABLED, MIN_FONT_SIZE
from .furigana import annotate as furigana_annotate
from .image_utils import (
    clear_text_strokes,
    encode_image_pil,
    load_image,
    load_image_pil,
)
from .ocr import extract_text_from_region, is_valid_japanese
from .text_renderer import (
    compute_bubble_font_size,
    compute_furigana_page_font_limits,
    draw_debug_boxes,
    estimate_source_vertical_font_size,
    render_english,
    render_english_on_artwork,
    render_furigana_vertical,
)
from .translator import translate

log = logging.getLogger(__name__)


class PipelineMode(Enum):
    FURIGANA = "furigana"
    TRANSLATE = "translate"


@dataclass
class BubbleResult:
    bbox: tuple[int, int, int, int]
    ocr_text: str = ""
    is_valid: bool = False
    transformed: object = None  # furigana segments or english string
    is_artwork_text: bool = False  # True if text found on artwork (no bubble)
    mask: np.ndarray | None = None  # Bubble shape mask for curved clearing
    source_font_size: int | None = None  # Estimated original glyph scale


@dataclass(frozen=True)
class MangaRenderPlan:
    """Values prepared before cleanup and consumed by the render stage."""

    base_font_size: int | None
    font_sizes: dict[int, int]
    page_font_cap: int | None
    source_outlier_threshold: int | None
    page_font_floor: int | None
    artwork_inpainted: frozenset[int]


@dataclass
class PageResult:
    image_path: str
    name: str
    img_cv: np.ndarray | None = None
    img_pil: Image.Image | None = None
    bubbles_raw: list[dict] = field(default_factory=list)
    semantic_detections: list[SemanticDetection] = field(default_factory=list)
    bubble_results: list[BubbleResult] = field(default_factory=list)
    output_img: Image.Image | None = None
    render_plan: MangaRenderPlan | None = None


# --- Stage functions ---


def load_page(path: str) -> PageResult:
    """Stage 1: Load image as both OpenCV and Pillow formats."""
    name = os.path.splitext(os.path.basename(path))[0]
    return PageResult(
        image_path=path,
        name=name,
        img_cv=load_image(path),
        img_pil=load_image_pil(path),
    )


def load_page_from_memory(img_cv: np.ndarray, img_pil: Image.Image,
                          name: str = "page") -> PageResult:
    """Create PageResult from in-memory images (no file I/O)."""
    return PageResult(image_path="", name=name, img_cv=img_cv, img_pil=img_pil)


def detect_page_bubbles(page: PageResult) -> None:
    """Stage 2: Run bubble detection, populates page.bubbles_raw.

    Also extracts bubble shape masks for speech_bubble detections.
    Artwork text (SFX/narration) gets no mask — it uses inpainting or
    solid fill instead.
    """
    log.info("Detecting bubbles: %s", page.name)
    from dataclasses import replace

    semantic: list[SemanticDetection] = []
    for detection in detect_semantic_regions(page.img_cv):
        if detection.kind != "balloon":
            semantic.append(detection)
            continue
        mask = extract_bubble_mask_manga(page.img_cv, tuple(detection.bbox.as_list()))
        semantic.append(replace(
            detection,
            mask=mask,
            polygon=_mask_polygon(mask),
        ))
    page.semantic_detections = semantic
    page.bubbles_raw = legacy_processing_regions(semantic)

    for det in page.bubbles_raw:
        semantic_match = next(
            (item for item in semantic if item.id == det.get("semantic_id")), None,
        )
        if semantic_match is not None and semantic_match.mask is not None:
            det["mask"] = semantic_match.mask
        elif not det.get("is_artwork", False):
            det["mask"] = extract_bubble_mask_manga(page.img_cv, det["bbox"])
        else:
            det["mask"] = None

    log.info("Found %d bubbles in %s", len(page.bubbles_raw), page.name)


def _mask_polygon(mask: np.ndarray) -> Polygon | None:
    """Return a compact contour so mask geometry survives JSON persistence."""
    import cv2

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    perimeter = cv2.arcLength(contour, True)
    compact = cv2.approxPolyDP(contour, max(1.0, perimeter * 0.005), True)
    points = tuple((int(point[0][0]), int(point[0][1])) for point in compact)
    return Polygon(points) if len(points) >= 3 else None


def _ocr_char_overlap(text1: str, text2: str) -> float:
    """Fraction of content chars in text1 that also appear in text2."""
    chars1 = {ch for ch in text1
              if not (0x3000 <= ord(ch) <= 0x303F or
                      0xFF01 <= ord(ch) <= 0xFF60 or
                      ch.isspace())}
    chars2 = {ch for ch in text2
              if not (0x3000 <= ord(ch) <= 0x303F or
                      0xFF01 <= ord(ch) <= 0xFF60 or
                      ch.isspace())}
    if not chars1:
        return 1.0
    return len(chars1 & chars2) / len(chars1)


def _verify_ocr_consistency(img_pil: Image.Image, bbox: tuple,
                            text: str) -> bool:
    """Detect manga-ocr hallucinations by comparing full vs center crop.

    Real text produces overlapping OCR across different crops; hallucinated
    text (e.g. on faces) produces unrelated strings each time.  Only called
    for short texts (< 15 chars) where hallucination risk is highest.
    """
    x1, y1, x2, y2 = bbox
    w, h = x2 - x1, y2 - y1
    if w < 20 or h < 20:
        return True
    # Shrink bbox by 15% on each side → center 70%
    dx, dy = max(3, w * 15 // 100), max(3, h * 15 // 100)
    center_bbox = (x1 + dx, y1 + dy, x2 - dx, y2 - dy)
    center_text = extract_text_from_region(img_pil, center_bbox)
    overlap = _ocr_char_overlap(text, center_text)
    if overlap < 0.35:
        log.info("  OCR hallucination rejected (overlap=%.2f): %s vs %s",
                 overlap, text, center_text)
        return False
    return True


def ocr_bubble(img_pil: Image.Image, bubble: dict) -> BubbleResult:
    """Stage 3: OCR + validation for a single bubble."""
    bbox = bubble["bbox"]
    is_artwork = bubble.get("is_artwork", False)
    text = extract_text_from_region(img_pil, bbox)
    valid = bool(text.strip()) and is_valid_japanese(text)
    # Detect manga-ocr hallucinations on non-text regions (faces, artwork).
    # Short texts are most prone to hallucination — verify by checking OCR
    # consistency across a center crop.
    if (valid and len(text.strip()) < 15
            and not _verify_ocr_consistency(img_pil, bbox, text)):
        valid = False
    if text.strip() and not valid:
        log.info("  OCR noise (not Japanese): %s, skipping", text)
    elif valid:
        log.info("  OCR: %s", text)
    return BubbleResult(bbox=bbox, ocr_text=text,
                        is_valid=valid, is_artwork_text=is_artwork,
                        mask=bubble.get("mask"))


def transform_furigana(br: BubbleResult) -> None:
    """Stage 4a: Annotate with furigana readings."""
    if not br.is_valid:
        return
    segments = furigana_annotate(br.ocr_text)
    if any(s["needs_furigana"] for s in segments):
        br.transformed = segments
    else:
        log.info("  No kanji in '%s', skipping furigana", br.ocr_text)


def transform_translate(br: BubbleResult, target_lang: str = "en") -> None:
    """Stage 4b: Translate Japanese to target language."""
    if not br.is_valid:
        return
    translated = translate(br.ocr_text, target_lang)
    if translated.strip():
        br.transformed = translated
        log.info("  Translated: %s", translated)
    else:
        log.info("  Translation empty for '%s', skipping", br.ocr_text)


def _compute_normalized_font_sizes(
    bubble_results: list, base_font_size: int | None,
) -> dict[int, int]:
    """Pre-compute and normalize font sizes for English bubbles.

    Uses mask-safe height (prevents clipping) with raw width (maximizes
    space).  Caps outliers at median * 1.4 so sizes stay consistent
    across the page while still allowing natural variation.
    """
    font_sizes: dict[int, int] = {}
    for i, br in enumerate(bubble_results):
        if br.transformed is None or br.is_artwork_text:
            continue
        size = compute_bubble_font_size(
            br.bbox, br.transformed, base_font_size, br.mask)
        if size is not None:
            font_sizes[i] = size

    # Cap outliers: need at least 3 bubbles for a meaningful median
    if len(font_sizes) >= 3:
        sizes = sorted(font_sizes.values())
        median = sizes[len(sizes) // 2]
        cap = int(median * 1.4)
        for i, value in font_sizes.items():
            if value > cap:
                log.debug("  Capping bubble %d font %d→%d (median=%d)",
                          i, value, cap, median)
                font_sizes[i] = cap

    return font_sizes


def render_page(page: PageResult, mode: PipelineMode, out_dir: str,
                debug: bool = False) -> None:
    """Stage 5: Clear bubbles, render transformed text, save output."""
    if debug:
        debug_img = draw_debug_boxes(page.img_pil, page.bubbles_raw)
        debug_img.save(os.path.join(out_dir, f"{page.name}-debug.png"))

    output_img = _render_page_image(page, mode, log_font_target=True)

    # Save
    suffix = "-furigana.png" if mode == PipelineMode.FURIGANA else "-en.png"
    output_path = os.path.join(out_dir, f"{page.name}{suffix}")
    output_img.save(output_path)
    log.info("Saved: %s", output_path)


def render_page_to_bytes(page: PageResult, mode: PipelineMode,
                         debug: bool = False) -> bytes:
    """Run render logic and return PNG bytes instead of saving to disk.

    Reuses the same clearing + rendering logic as render_page() but
    returns the result as encoded PNG bytes for the worker pipeline.
    """
    if debug:
        debug_img = draw_debug_boxes(page.img_pil, page.bubbles_raw)
        # Debug image is discarded in bytes mode — no filesystem to save to
        del debug_img

    return encode_image_pil(_render_page_image(page, mode))


def cleanup_page(page: PageResult, mode: PipelineMode,
                 log_font_target: bool = False) -> None:
    """Remove source text and retain the values needed for later rendering."""
    page.output_img = page.img_pil.copy()

    base_font_size = None
    font_sizes: dict[int, int] = {}
    if mode == PipelineMode.TRANSLATE:
        page_height = page.img_pil.height
        base_font_size = max(MIN_FONT_SIZE, page_height // EN_PAGE_FONT_DIVISOR)
        if log_font_target:
            log.info("Page font target: %dpx (page height=%d)",
                     base_font_size, page_height)
        font_sizes = _compute_normalized_font_sizes(
            page.bubble_results, base_font_size)

    page_font_cap = None
    source_outlier_threshold = None
    page_font_floor = None
    if mode == PipelineMode.FURIGANA:
        page_font_cap, source_outlier_threshold, page_font_floor = (
            _estimate_furigana_source_font_sizes(page)
        )

    artwork_inpainted: set[int] = set()
    for i, br in enumerate(page.bubble_results):
        if br.transformed is None:
            continue
        if br.is_artwork_text and mode == PipelineMode.TRANSLATE:
            if MANGA_INPAINT_ENABLED:
                from .inpainter import inpaint_region
                page.output_img = inpaint_region(page.output_img, br.bbox)
                artwork_inpainted.add(i)
            continue
        clear_text_strokes(page.output_img, br.bbox, mask=br.mask)

    page.render_plan = MangaRenderPlan(
        base_font_size=base_font_size,
        font_sizes=font_sizes,
        page_font_cap=page_font_cap,
        source_outlier_threshold=source_outlier_threshold,
        page_font_floor=page_font_floor,
        artwork_inpainted=frozenset(artwork_inpainted),
    )


def render_cleaned_page_to_bytes(page: PageResult, mode: PipelineMode) -> bytes:
    """Render transformed text after the explicit cleanup stage."""
    return encode_image_pil(_render_cleaned_page_image(page, mode))


def _render_cleaned_page_image(page: PageResult,
                               mode: PipelineMode) -> Image.Image:
    """Render without cleanup so orchestration can call both stages."""
    if page.output_img is None or page.render_plan is None:
        raise RuntimeError("manga cleanup stage must run before rendering")

    plan = page.render_plan
    for i, br in enumerate(page.bubble_results):
        if br.transformed is None:
            continue
        if br.is_artwork_text and mode == PipelineMode.TRANSLATE:
            render_english_on_artwork(
                page.output_img,
                br.bbox,
                br.transformed,
                base_font_size=plan.base_font_size,
                inpainted=i in plan.artwork_inpainted,
            )
            continue
        if mode == PipelineMode.FURIGANA:
            render_furigana_vertical(
                page.output_img,
                br.bbox,
                br.transformed,
                mask=br.mask,
                source_font_size=br.source_font_size,
                page_font_cap=plan.page_font_cap,
                source_outlier_threshold=plan.source_outlier_threshold,
                page_font_floor=plan.page_font_floor,
                layout_img=page.img_pil,
            )
        else:
            render_english(
                page.output_img,
                br.bbox,
                br.transformed,
                base_font_size=plan.font_sizes.get(i, plan.base_font_size),
                mask=br.mask,
            )
    return page.output_img


def _render_page_image(page: PageResult, mode: PipelineMode,
                       log_font_target: bool = False) -> Image.Image:
    """Compatibility composition of the cleanup and render stages."""
    cleanup_page(page, mode, log_font_target=log_font_target)
    return _render_cleaned_page_image(page, mode)


def _estimate_furigana_source_font_sizes(
    page: PageResult,
) -> tuple[int | None, int | None, int | None]:
    """Estimate each furigana region's original glyph size before clearing."""
    source_sizes: list[int] = []
    for br in page.bubble_results:
        if br.transformed is None:
            continue
        br.source_font_size = estimate_source_vertical_font_size(
            page.img_pil,
            br.bbox,
            br.ocr_text,
            mask=br.mask,
        )
        if br.source_font_size is not None:
            source_sizes.append(br.source_font_size)
    return compute_furigana_page_font_limits(source_sizes)
