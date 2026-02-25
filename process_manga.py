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


def run_pipeline(image_paths: list[str], mode: PipelineMode, out_dir: str,
                 debug: bool = False) -> None:
    """Run the full pipeline with parallelized stages."""
    os.makedirs(out_dir, exist_ok=True)

    if not image_paths:
        log.info("No images found for %s pipeline", mode.value)
        return

    log.info("=== %s PIPELINE: %d images ===", mode.value.upper(), len(image_paths))

    # Stage 1+2: Load and detect bubbles (OpenCV releases GIL, pages independent)
    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(load_page, image_paths))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(detect_page_bubbles, pages))

    # Stage 3: OCR all bubbles (manga-ocr CPU singleton, lock-protected)
    all_ocr_tasks = []
    for page in pages:
        for bubble in page.bubbles_raw:
            all_ocr_tasks.append((page, bubble))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ocr_bubble, page.img_pil, bubble)
                   for page, bubble in all_ocr_tasks]
        all_results = [f.result() for f in futures]

    # Assign bubble results back to pages
    idx = 0
    for page in pages:
        count = len(page.bubbles_raw)
        page.bubble_results = all_results[idx:idx + count]
        idx += count

    # Stage 4: Transform (furigana or translate)
    transform_fn = transform_furigana if mode == PipelineMode.FURIGANA else transform_translate
    max_workers = 4 if mode == PipelineMode.FURIGANA else 2
    all_brs = [br for page in pages for br in page.bubble_results if br.is_valid]

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(transform_fn, all_brs))

    # Stage 5: Render (sequential — mutates shared output_img per page)
    for page in pages:
        render_page(page, mode, out_dir, debug=debug)

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
