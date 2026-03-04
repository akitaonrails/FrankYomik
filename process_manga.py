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
from concurrent.futures import ThreadPoolExecutor

from pipeline.config import DOCS_DIR, OUTPUT_DIR
from pipeline.processor import (
    PageResult,
    PipelineMode,
    detect_page_bubbles,
    load_page,
    ocr_bubble,
    render_page,
    transform_furigana,
    transform_translate,
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


def _process_page(path: str, mode: PipelineMode) -> "PageResult":
    """Run load → detect → OCR → transform for a single page."""
    page = load_page(path)
    detect_page_bubbles(page)
    page.bubble_results = [ocr_bubble(page.img_pil, b) for b in page.bubbles_raw]
    transform_fn = transform_furigana if mode == PipelineMode.FURIGANA else transform_translate
    for br in page.bubble_results:
        if br.is_valid:
            transform_fn(br)
    return page


def run_pipeline(image_paths: list[str], mode: PipelineMode, out_dir: str,
                 debug: bool = False) -> None:
    """Run the full pipeline with page-level parallelism."""
    os.makedirs(out_dir, exist_ok=True)

    if not image_paths:
        log.info("No images found for %s pipeline", mode.value)
        return

    log.info("=== %s PIPELINE: %d images ===", mode.value.upper(), len(image_paths))

    # Process pages in parallel (each page flows through load→detect→OCR→transform)
    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(lambda p: _process_page(p, mode), image_paths))

    # Render pages in parallel (each page has its own independent output_img)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda page: render_page(page, mode, out_dir, debug=debug), pages))

    log.info("Done with %s pipeline!", mode.value)


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
        images = _find_images("adult")
        run_pipeline(images, PipelineMode.FURIGANA, furigana_dir, debug=args.debug)

    if args.command in ("translate", "all"):
        images = _find_images("shounen")
        run_pipeline(images, PipelineMode.TRANSLATE, translate_dir, debug=args.debug)


if __name__ == "__main__":
    main()
