"""Unified pipeline stages for manga processing."""

import logging
import os
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
from PIL import Image

from .bubble_detector import detect_bubbles
from .config import (
    EN_BASE_FONT_DIVISOR, EN_BASE_FONT_MAX, EN_BASE_FONT_MIN,
    MANGA_INPAINT_ENABLED, TEXT_DETECTION_ENABLED,
)
from .furigana import annotate as furigana_annotate
from .image_utils import (
    clear_text_in_contour,
    clear_text_in_region,
    contour_inner_bbox,
    load_image,
    load_image_pil,
)
from .ocr import extract_text_from_region, is_valid_japanese
from .text_renderer import (
    draw_debug_boxes, render_english, render_english_on_artwork,
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
    contour: np.ndarray | None
    ocr_text: str = ""
    is_valid: bool = False
    transformed: object = None  # furigana segments or english string
    is_artwork_text: bool = False  # True if text found on artwork (no bubble)


@dataclass
class PageResult:
    image_path: str
    name: str
    img_cv: np.ndarray | None = None
    img_pil: Image.Image | None = None
    bubbles_raw: list[dict] = field(default_factory=list)
    bubble_results: list[BubbleResult] = field(default_factory=list)
    output_img: Image.Image | None = None


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


def detect_page_bubbles(page: PageResult) -> None:
    """Stage 2: Run bubble detection, populates page.bubbles_raw."""
    log.info("Detecting bubbles: %s", page.name)
    page.bubbles_raw = detect_bubbles(page.img_cv)
    log.info("Found %d bubbles in %s", len(page.bubbles_raw), page.name)


def ocr_bubble(img_pil: Image.Image, bubble: dict) -> BubbleResult:
    """Stage 3: OCR + validation for a single bubble."""
    bbox = bubble["bbox"]
    contour = bubble.get("contour")
    is_artwork = bubble.get("is_artwork", False)
    text = extract_text_from_region(img_pil, bbox)
    valid = bool(text.strip()) and is_valid_japanese(text)
    if text.strip() and not valid:
        log.info("  OCR noise (not Japanese): %s, skipping", text)
    elif valid:
        log.info("  OCR: %s", text)
    return BubbleResult(bbox=bbox, contour=contour, ocr_text=text,
                        is_valid=valid, is_artwork_text=is_artwork)


def transform_furigana(br: BubbleResult) -> None:
    """Stage 4a: Annotate with furigana readings."""
    if not br.is_valid:
        return
    segments = furigana_annotate(br.ocr_text)
    if any(s["needs_furigana"] for s in segments):
        br.transformed = segments
    else:
        log.info("  No kanji in '%s', skipping furigana", br.ocr_text)


def transform_translate(br: BubbleResult) -> None:
    """Stage 4b: Translate Japanese to English."""
    if not br.is_valid:
        return
    english = translate(br.ocr_text)
    if english.strip():
        br.transformed = english
        log.info("  EN: %s", english)
    else:
        log.info("  Translation empty for '%s', skipping", br.ocr_text)


def detect_page_text(page: PageResult) -> None:
    """Stage 2b: Detect text outside bubbles using EasyOCR and stroke analysis.

    Two detection approaches run if text detection is enabled:
    1. EasyOCR — finds text on artwork (narration, signs, titles)
    2. Text-stroke clustering — finds vertical text in white panel areas
       where speech bubbles merge with the panel background

    Appends results to page.bubbles_raw.
    """
    if not TEXT_DETECTION_ENABLED:
        return

    from .text_detector import (
        detect_panel_text, detect_text_regions, find_unbubbled_text,
    )

    bubble_bboxes = [b["bbox"] for b in page.bubbles_raw]

    # 1. EasyOCR detection for artwork text
    log.info("Running text detection: %s", page.name)
    text_regions = detect_text_regions(page.img_cv)
    unbubbled = find_unbubbled_text(text_regions, bubble_bboxes)

    for region in unbubbled:
        page.bubbles_raw.append({
            "bbox": region.bbox,
            "type": "artwork_text",
            "is_artwork": True,
        })

    if unbubbled:
        log.info("Added %d artwork text regions for %s",
                 len(unbubbled), page.name)

    # 2. Text-stroke detection for panel-embedded text
    all_bboxes = [b["bbox"] for b in page.bubbles_raw]
    panel_texts = detect_panel_text(page.img_cv, all_bboxes)

    for bbox in panel_texts:
        page.bubbles_raw.append({
            "bbox": bbox,
            "type": "speech_bubble",
        })

    if panel_texts:
        log.info("Added %d panel text regions for %s",
                 len(panel_texts), page.name)


def render_page(page: PageResult, mode: PipelineMode, out_dir: str,
                debug: bool = False) -> None:
    """Stage 5: Clear bubbles, render transformed text, save output."""
    if debug:
        debug_img = draw_debug_boxes(page.img_pil, page.bubbles_raw)
        debug_img.save(os.path.join(out_dir, f"{page.name}-debug.png"))

    page.output_img = page.img_pil.copy()

    base_font_size = None
    if mode == PipelineMode.TRANSLATE:
        page_height = page.img_pil.height
        base_font_size = max(EN_BASE_FONT_MIN,
                             min(EN_BASE_FONT_MAX, page_height // EN_BASE_FONT_DIVISOR))
        log.info("Base English font size: %d (page height=%d)",
                 base_font_size, page_height)

    for br in page.bubble_results:
        if br.transformed is None:
            continue

        # Artwork text: inpaint background then render with overlay
        if br.is_artwork_text and mode == PipelineMode.TRANSLATE:
            inpainted = False
            if MANGA_INPAINT_ENABLED:
                from .inpainter import inpaint_region
                page.output_img = inpaint_region(page.output_img, br.bbox)
                inpainted = True
            render_english_on_artwork(page.output_img, br.bbox,
                                      br.transformed,
                                      base_font_size=base_font_size,
                                      inpainted=inpainted)
            continue

        # Clear text region using contour shape when available
        layout_bbox = br.bbox
        if br.contour is not None:
            layout_bbox = contour_inner_bbox(br.contour) or br.bbox
            clear_text_in_contour(page.output_img, br.contour)
        else:
            clear_text_in_region(page.output_img, br.bbox)

        # Render
        if mode == PipelineMode.FURIGANA:
            render_furigana_vertical(page.output_img, layout_bbox, br.transformed)
        else:
            render_english(page.output_img, layout_bbox, br.transformed,
                           base_font_size=base_font_size)

    # Save
    suffix = "-furigana.png" if mode == PipelineMode.FURIGANA else "-en.png"
    output_path = os.path.join(out_dir, f"{page.name}{suffix}")
    page.output_img.save(output_path)
    log.info("Saved: %s", output_path)
