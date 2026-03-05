"""Unified pipeline stages for manga processing."""

import logging
import os
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
from PIL import Image

from .bubble_detector import detect_bubbles, extract_bubble_mask_manga
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
    ocr_text: str = ""
    is_valid: bool = False
    transformed: object = None  # furigana segments or english string
    is_artwork_text: bool = False  # True if text found on artwork (no bubble)
    mask: np.ndarray | None = None  # Bubble shape mask for curved clearing


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
    page.bubbles_raw = detect_bubbles(page.img_cv)

    for det in page.bubbles_raw:
        if not det.get("is_artwork", False):
            det["mask"] = extract_bubble_mask_manga(page.img_cv, det["bbox"])
        else:
            det["mask"] = None

    log.info("Found %d bubbles in %s", len(page.bubbles_raw), page.name)


def _ocr_char_overlap(text1: str, text2: str) -> float:
    """Fraction of content chars in text1 that also appear in text2."""
    chars1 = set(ch for ch in text1
                 if not (0x3000 <= ord(ch) <= 0x303F or
                         0xFF01 <= ord(ch) <= 0xFF60 or
                         ch.isspace()))
    chars2 = set(ch for ch in text2
                 if not (0x3000 <= ord(ch) <= 0x303F or
                         0xFF01 <= ord(ch) <= 0xFF60 or
                         ch.isspace()))
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
    if valid and len(text.strip()) < 15:
        if not _verify_ocr_consistency(img_pil, bbox, text):
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


def render_page(page: PageResult, mode: PipelineMode, out_dir: str,
                debug: bool = False) -> None:
    """Stage 5: Clear bubbles, render transformed text, save output."""
    if debug:
        debug_img = draw_debug_boxes(page.img_pil, page.bubbles_raw)
        debug_img.save(os.path.join(out_dir, f"{page.name}-debug.png"))

    page.output_img = page.img_pil.copy()

    # Page-wide font target for consistent sizing across all bubbles.
    # Proportional to image height so high-DPI screens get larger text.
    base_font_size = None
    if mode == PipelineMode.TRANSLATE:
        page_height = page.img_pil.height
        base_font_size = max(MIN_FONT_SIZE, page_height // EN_PAGE_FONT_DIVISOR)
        log.info("Page font target: %dpx (page height=%d)",
                 base_font_size, page_height)

    # Two-pass rendering: clear all bubbles first, then render all text.
    # This prevents overlapping bubbles from erasing each other's rendered text.

    # Pass 1: Clear text strokes and inpaint artwork
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

    # Pass 2: Render text
    for i, br in enumerate(page.bubble_results):
        if br.transformed is None:
            continue
        if br.is_artwork_text and mode == PipelineMode.TRANSLATE:
            render_english_on_artwork(page.output_img, br.bbox,
                                      br.transformed,
                                      base_font_size=base_font_size,
                                      inpainted=i in artwork_inpainted)
            continue
        if mode == PipelineMode.FURIGANA:
            render_furigana_vertical(page.output_img, br.bbox, br.transformed,
                                     mask=br.mask)
        else:
            render_english(page.output_img, br.bbox, br.transformed,
                           base_font_size=base_font_size, mask=br.mask)

    # Save
    suffix = "-furigana.png" if mode == PipelineMode.FURIGANA else "-en.png"
    output_path = os.path.join(out_dir, f"{page.name}{suffix}")
    page.output_img.save(output_path)
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

    page.output_img = page.img_pil.copy()

    base_font_size = None
    if mode == PipelineMode.TRANSLATE:
        page_height = page.img_pil.height
        base_font_size = max(MIN_FONT_SIZE, page_height // EN_PAGE_FONT_DIVISOR)

    # Two-pass rendering (same as render_page)
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

    for i, br in enumerate(page.bubble_results):
        if br.transformed is None:
            continue
        if br.is_artwork_text and mode == PipelineMode.TRANSLATE:
            render_english_on_artwork(page.output_img, br.bbox,
                                      br.transformed,
                                      base_font_size=base_font_size,
                                      inpainted=i in artwork_inpainted)
            continue
        if mode == PipelineMode.FURIGANA:
            render_furigana_vertical(page.output_img, br.bbox, br.transformed,
                                     mask=br.mask)
        else:
            render_english(page.output_img, br.bbox, br.transformed,
                           base_font_size=base_font_size, mask=br.mask)

    return encode_image_pil(page.output_img)
