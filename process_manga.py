#!/usr/bin/env python3
"""Frank Manga - Proof of Concept CLI.

Usage:
    python process_manga.py furigana          # all adult*.png → output/furigana/
    python process_manga.py translate          # all shounen*.png → output/translate/
    python process_manga.py all                # both
    python process_manga.py all --debug        # both + debug bounding box images
"""

import argparse
import glob
import logging
import os

from pipeline.config import DOCS_DIR, OUTPUT_DIR, EN_BASE_FONT_DIVISOR, EN_BASE_FONT_MIN, EN_BASE_FONT_MAX
from pipeline.image_utils import load_image, load_image_pil, clear_text_in_region, clear_text_in_contour
from pipeline.bubble_detector import detect_bubbles
from pipeline.ocr import extract_text_from_region, is_valid_japanese
from pipeline.furigana import annotate as furigana_annotate
from pipeline.translator import translate
from pipeline.text_renderer import (
    render_furigana_vertical,
    render_english,
    draw_debug_boxes,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


def _find_images(prefix: str) -> list[str]:
    """Find all docs/{prefix}*.png files, sorted by name."""
    pattern = os.path.join(DOCS_DIR, f"{prefix}*.png")
    return sorted(glob.glob(pattern))


def process_furigana(image_path: str, out_dir: str, debug: bool = False) -> None:
    """Add furigana to kanji in a single manga page."""
    name = os.path.splitext(os.path.basename(image_path))[0]
    log.info("--- Furigana: %s ---", name)

    img_cv = load_image(image_path)
    img_pil = load_image_pil(image_path)

    log.info("Detecting speech bubbles...")
    bubbles = detect_bubbles(img_cv)
    log.info("Found %d bubbles", len(bubbles))

    if debug:
        debug_img = draw_debug_boxes(img_pil, bubbles)
        debug_img.save(os.path.join(out_dir, f"{name}-debug.png"))

    output_img = img_pil.copy()

    for i, bubble in enumerate(bubbles):
        bbox = bubble["bbox"]
        log.info("Bubble %d/%d: bbox=%s", i + 1, len(bubbles), bbox)

        text = extract_text_from_region(img_pil, bbox)
        if not text.strip():
            log.info("  No text, skipping")
            continue
        if not is_valid_japanese(text):
            log.info("  OCR noise (not Japanese): %s, skipping", text)
            continue
        log.info("  OCR: %s", text)

        segments = furigana_annotate(text)
        if not any(s["needs_furigana"] for s in segments):
            log.info("  No kanji, skipping")
            continue

        contour = bubble.get("contour")
        if contour is not None:
            clear_text_in_contour(output_img, contour)
        else:
            clear_text_in_region(output_img, bbox)
        render_furigana_vertical(output_img, bbox, segments)

    output_path = os.path.join(out_dir, f"{name}-furigana.png")
    output_img.save(output_path)
    log.info("Saved: %s", output_path)


def process_translate(image_path: str, out_dir: str, debug: bool = False) -> None:
    """Translate Japanese dialogue to English in a single manga page."""
    name = os.path.splitext(os.path.basename(image_path))[0]
    log.info("--- Translate: %s ---", name)

    img_cv = load_image(image_path)
    img_pil = load_image_pil(image_path)

    log.info("Detecting speech bubbles...")
    bubbles = detect_bubbles(img_cv)
    log.info("Found %d bubbles", len(bubbles))

    if debug:
        debug_img = draw_debug_boxes(img_pil, bubbles)
        debug_img.save(os.path.join(out_dir, f"{name}-debug.png"))

    output_img = img_pil.copy()

    # Compute a consistent base font size from the page height
    page_height = img_pil.height
    base_font_size = max(EN_BASE_FONT_MIN, min(EN_BASE_FONT_MAX, page_height // EN_BASE_FONT_DIVISOR))
    log.info("Base English font size: %d (page height=%d)", base_font_size, page_height)

    for i, bubble in enumerate(bubbles):
        bbox = bubble["bbox"]
        log.info("Bubble %d/%d: bbox=%s", i + 1, len(bubbles), bbox)

        text = extract_text_from_region(img_pil, bbox)
        if not text.strip():
            log.info("  No text, skipping")
            continue
        if not is_valid_japanese(text):
            log.info("  OCR noise (not Japanese): %s, skipping", text)
            continue
        log.info("  OCR: %s", text)

        english = translate(text)
        if not english.strip():
            log.info("  Translation empty, skipping")
            continue
        log.info("  EN: %s", english)

        contour = bubble.get("contour")
        if contour is not None:
            clear_text_in_contour(output_img, contour)
        else:
            clear_text_in_region(output_img, bbox)
        render_english(output_img, bbox, english, base_font_size=base_font_size)

    output_path = os.path.join(out_dir, f"{name}-en.png")
    output_img.save(output_path)
    log.info("Saved: %s", output_path)


def main():
    parser = argparse.ArgumentParser(description="Frank Manga PoC - Add furigana or translate manga")
    parser.add_argument("command", choices=["furigana", "translate", "all"],
                        help="Pipeline to run")
    parser.add_argument("--debug", action="store_true",
                        help="Save debug images with bounding boxes")
    args = parser.parse_args()

    furigana_dir = os.path.join(OUTPUT_DIR, "furigana")
    translate_dir = os.path.join(OUTPUT_DIR, "translate")

    if args.command in ("furigana", "all"):
        os.makedirs(furigana_dir, exist_ok=True)
        images = _find_images("adult")
        log.info("=== FURIGANA PIPELINE: %d images ===", len(images))
        for img_path in images:
            process_furigana(img_path, furigana_dir, debug=args.debug)

    if args.command in ("translate", "all"):
        os.makedirs(translate_dir, exist_ok=True)
        images = _find_images("shounen")
        log.info("=== TRANSLATION PIPELINE: %d images ===", len(images))
        for img_path in images:
            process_translate(img_path, translate_dir, debug=args.debug)

    log.info("Done!")


if __name__ == "__main__":
    main()
