#!/usr/bin/env python3
"""Korean Webtoon Translation Pipeline CLI.

Usage:
    python process_webtoon.py download URL [--chapters 1-10]
    python process_webtoon.py translate DIRECTORY [--debug]
    python process_webtoon.py pipeline URL [--chapters 1-10] [--debug]
"""

import argparse
import glob
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from webtoon.config import OUTPUT_DIR
from webtoon.processor import (
    WebtoonPageResult,
    detect_bubbles_rtdetr,
    load_page,
    render_page,
    validate_and_translate,
)
from webtoon.scraper import download_chapter_range, download_episode, parse_naver_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


def _find_images(directory: str) -> list[str]:
    """Find all image files in a directory, sorted by name."""
    patterns = ["*.jpg", "*.jpeg", "*.png", "*.webp"]
    files = []
    for pat in patterns:
        files.extend(glob.glob(os.path.join(directory, pat)))
    return sorted(files)


def _process_page(path: str) -> WebtoonPageResult:
    """Run load → detect → validate+translate for a single page."""
    page = load_page(path)
    detect_bubbles_rtdetr(page)
    validate_and_translate(page)
    return page


def run_translate(image_dir: str, out_dir: str, debug: bool = False) -> None:
    """Run the translation pipeline on pre-downloaded webtoon images."""
    os.makedirs(out_dir, exist_ok=True)

    image_paths = _find_images(image_dir)
    if not image_paths:
        log.info("No images found in %s", image_dir)
        return

    log.info("=== WEBTOON TRANSLATE: %d images from %s ===",
             len(image_paths), image_dir)

    # Process pages (OCR + translate) — limited parallelism due to GPU OCR
    with ThreadPoolExecutor(max_workers=2) as pool:
        pages = list(pool.map(_process_page, image_paths))

    # Render pages in parallel
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda p: render_page(p, out_dir, debug=debug), pages))

    log.info("Done! Output in %s", out_dir)


def _parse_chapter_range(range_str: str) -> tuple[int, int]:
    """Parse chapter range string like '1-10' or '5'."""
    if "-" in range_str:
        parts = range_str.split("-", 1)
        return int(parts[0]), int(parts[1])
    n = int(range_str)
    return n, n


def cmd_download(args):
    """Download webtoon images from Naver."""
    if args.chapters:
        start, end = _parse_chapter_range(args.chapters)
        results = download_chapter_range(args.url, start, end)
        total = sum(len(files) for files in results.values())
        log.info("Downloaded %d images across %d chapters", total, len(results))
    else:
        files = download_episode(args.url)
        log.info("Downloaded %d images", len(files))


def cmd_translate(args):
    """Translate pre-downloaded webtoon images."""
    image_dir = args.directory
    # Use directory name to build output path
    dir_name = os.path.basename(os.path.normpath(image_dir))
    out_dir = os.path.join(OUTPUT_DIR, dir_name)
    run_translate(image_dir, out_dir, debug=args.debug)


def cmd_pipeline(args):
    """Download and translate in one step."""
    # Download
    parsed = parse_naver_url(args.url)
    if args.chapters:
        start, end = _parse_chapter_range(args.chapters)
        results = download_chapter_range(args.url, start, end)
        for chapter_no, files in sorted(results.items()):
            if files:
                chapter_dir = os.path.dirname(files[0])
                out_dir = os.path.join(OUTPUT_DIR, parsed["title_id"],
                                       f"chapter_{chapter_no}")
                run_translate(chapter_dir, out_dir, debug=args.debug)
    else:
        files = download_episode(args.url)
        if files:
            episode_dir = os.path.dirname(files[0])
            ep_name = f"chapter_{parsed['episode_no']}" if parsed["episode_no"] else "episode"
            out_dir = os.path.join(OUTPUT_DIR, parsed["title_id"], ep_name)
            run_translate(episode_dir, out_dir, debug=args.debug)


def main():
    parser = argparse.ArgumentParser(
        description="Korean Webtoon Translation Pipeline"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # download
    dl = subparsers.add_parser("download", help="Download webtoon images from Naver")
    dl.add_argument("url", help="Naver Webtoon episode URL")
    dl.add_argument("--chapters", help="Chapter range (e.g. '1-10' or '5')")
    dl.set_defaults(func=cmd_download)

    # translate
    tr = subparsers.add_parser("translate", help="Translate pre-downloaded images")
    tr.add_argument("directory", help="Directory containing webtoon images")
    tr.add_argument("--debug", action="store_true", help="Save debug images")
    tr.set_defaults(func=cmd_translate)

    # pipeline (download + translate)
    pl = subparsers.add_parser("pipeline", help="Download and translate in one step")
    pl.add_argument("url", help="Naver Webtoon episode URL")
    pl.add_argument("--chapters", help="Chapter range (e.g. '1-10' or '5')")
    pl.add_argument("--debug", action="store_true", help="Save debug images")
    pl.set_defaults(func=cmd_pipeline)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
