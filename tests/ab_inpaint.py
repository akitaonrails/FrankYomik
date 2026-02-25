#!/usr/bin/env python3
"""A/B test: compare inpainting models on webtoon balloon text removal.

Runs the OCR + bubble detection pipeline on real webtoon images, then applies
each available inpainting backend and generates side-by-side comparison grids.

Usage:
    python tests/ab_inpaint.py                    # all available backends
    python tests/ab_inpaint.py lama               # specific backend(s)
    python tests/ab_inpaint.py --input-dir DIR    # custom test images
"""

import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from webtoon.bubble_detector import WebtoonBubble
from webtoon.inpainter import (
    build_inpaint_mask,
    get_backend_for_name,
    _BACKEND_MAP,
)
from webtoon.processor import (
    load_page,
    detect_text,
    cluster_and_find_bubbles,
)

DEFAULT_INPUT_DIR = "webtoon_data/747269/chapter_293"
OUTPUT_DIR = "tests/ab_inpaint_results"

# Backends to test
ALL_BACKENDS = list(_BACKEND_MAP.keys())


def find_test_images(input_dir: str, max_images: int = 5) -> list[str]:
    """Find JPEG/PNG images in the input directory."""
    exts = {".jpg", ".jpeg", ".png"}
    files = sorted(
        f for f in os.listdir(input_dir)
        if os.path.splitext(f)[1].lower() in exts
    )
    return [os.path.join(input_dir, f) for f in files[:max_images]]


def select_test_bubbles(
    image_paths: list[str],
    max_per_image: int = 3,
) -> list[tuple[str, Image.Image, WebtoonBubble]]:
    """Run pipeline stages 1-3 and select bubbles with masks for testing."""
    test_cases = []

    for path in image_paths:
        name = os.path.basename(path)
        print(f"  Processing {name}...", end="", flush=True)

        page = load_page(path)
        detect_text(page)
        cluster_and_find_bubbles(page)

        masked = [b for b in page.bubbles
                  if b.bubble_mask is not None and b.text_regions]
        print(f" {len(masked)} masked bubbles")

        for bubble in masked[:max_per_image]:
            test_cases.append((name, page.img_pil, bubble))

    return test_cases


def run_inpainting(
    backend_name: str,
    test_cases: list[tuple[str, Image.Image, WebtoonBubble]],
) -> list[tuple[Image.Image | None, float]]:
    """Run inpainting for all test cases with a specific backend."""
    backend = get_backend_for_name(backend_name)
    if backend is None:
        return [(None, 0.0)] * len(test_cases)

    results = []
    for _name, img, bubble in test_cases:
        from webtoon.inpainter import inpaint_bubble
        target = img.copy()
        start = time.time()
        ok = inpaint_bubble(img, bubble, backend=backend, target_img=target)
        elapsed = time.time() - start
        results.append((target if ok else None, elapsed))

    backend.unload()
    return results


def crop_bubble_region(
    img: Image.Image,
    bubble: WebtoonBubble,
    pad: int = 20,
) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Crop image around a bubble with padding."""
    bx1, by1, bx2, by2 = bubble.bbox
    cx1 = max(0, bx1 - pad)
    cy1 = max(0, by1 - pad)
    cx2 = min(img.width, bx2 + pad)
    cy2 = min(img.height, by2 + pad)
    return img.crop((cx1, cy1, cx2, cy2)), (cx1, cy1, cx2, cy2)


def generate_comparison_grid(
    test_cases: list[tuple[str, Image.Image, WebtoonBubble]],
    backend_names: list[str],
    all_results: dict[str, list[tuple[Image.Image | None, float]]],
    output_dir: str,
) -> None:
    """Generate comparison grid images for each test bubble."""
    os.makedirs(output_dir, exist_ok=True)

    # Column headers: Original | Mask | backend1 | backend2 | ...
    columns = ["Original", "Mask"] + backend_names
    n_cols = len(columns)

    for i, (name, img, bubble) in enumerate(test_cases):
        crop, crop_box = crop_bubble_region(img, bubble)
        cx1, cy1, cx2, cy2 = crop_box
        cw, ch = crop.size

        # Build mask for display
        mask = build_inpaint_mask(bubble, (img.width, img.height))
        mask_crop = mask.crop(crop_box) if mask else Image.new("L", (cw, ch), 0)
        mask_rgb = Image.merge("RGB", [mask_crop, mask_crop, mask_crop])

        # Collect column images
        col_images = [crop, mask_rgb]
        for bname in backend_names:
            result_img, _time = all_results[bname][i]
            if result_img is not None:
                col_images.append(result_img.crop(crop_box))
            else:
                # Gray placeholder for unavailable backends
                placeholder = Image.new("RGB", (cw, ch), (128, 128, 128))
                col_images.append(placeholder)

        # Build grid
        label_h = 30
        grid_w = cw * n_cols + (n_cols - 1) * 2
        grid_h = ch + label_h
        grid = Image.new("RGB", (grid_w, grid_h), (40, 40, 40))
        draw = ImageDraw.Draw(grid)

        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc", 14)
        except OSError:
            font = ImageFont.load_default()

        x = 0
        for j, (col_img, label) in enumerate(zip(col_images, columns)):
            # Label
            bbox = font.getbbox(label)
            tw = bbox[2] - bbox[0]
            draw.text((x + (cw - tw) // 2, 4), label,
                      fill=(255, 255, 255), font=font)
            # Image
            grid.paste(col_img.resize((cw, ch)), (x, label_h))
            x += cw + 2

        out_name = f"{os.path.splitext(name)[0]}_bubble{i:02d}.png"
        grid.save(os.path.join(output_dir, out_name))
        print(f"  Saved {out_name}")


def print_timing_summary(
    backend_names: list[str],
    all_results: dict[str, list[tuple[Image.Image | None, float]]],
) -> None:
    """Print timing comparison table."""
    print(f"\n{'='*60}")
    print("TIMING SUMMARY (seconds per bubble)")
    print(f"{'─'*60}")
    print(f"{'Backend':<20s}{'Avg':>8s}{'Min':>8s}{'Max':>8s}{'Total':>8s}")
    print(f"{'─'*60}")

    for bname in backend_names:
        times = [t for _, t in all_results[bname] if t > 0]
        if not times:
            print(f"{bname:<20s}{'N/A':>8s}")
            continue
        avg_t = sum(times) / len(times)
        print(f"{bname:<20s}{avg_t:8.2f}{min(times):8.2f}"
              f"{max(times):8.2f}{sum(times):8.2f}")


def main():
    input_dir = DEFAULT_INPUT_DIR
    requested_backends = []

    # Parse args
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--input-dir" and i + 1 < len(args):
            input_dir = args[i + 1]
            i += 2
        else:
            requested_backends.append(args[i])
            i += 1

    if not requested_backends:
        requested_backends = ALL_BACKENDS

    # Validate input dir
    if not os.path.isdir(input_dir):
        print(f"Input directory not found: {input_dir}")
        sys.exit(1)

    # Find test images
    print(f"Input: {input_dir}")
    image_paths = find_test_images(input_dir)
    if not image_paths:
        print("No images found")
        sys.exit(1)
    print(f"Found {len(image_paths)} images")

    # Run OCR + bubble detection
    print("\nDetecting text and bubbles...")
    test_cases = select_test_bubbles(image_paths)
    if not test_cases:
        print("No masked bubbles found for testing")
        sys.exit(1)
    print(f"\nSelected {len(test_cases)} test bubbles")

    # Run each backend
    all_results: dict[str, list[tuple[Image.Image | None, float]]] = {}
    available_backends = []

    for bname in requested_backends:
        if bname not in _BACKEND_MAP:
            print(f"\n[--] Unknown backend: {bname}")
            continue

        print(f"\n{'='*60}")
        print(f"Testing {bname}...")
        print(f"{'='*60}")

        results = run_inpainting(bname, test_cases)
        success = sum(1 for r, _ in results if r is not None)

        if success > 0:
            all_results[bname] = results
            available_backends.append(bname)
            print(f"  {success}/{len(test_cases)} bubbles inpainted")
        else:
            print(f"  Backend unavailable or all inpaints failed")

    if not available_backends:
        print("\nNo backends available. Install simple-lama-inpainting "
              "or diffusers.")
        sys.exit(1)

    # Generate comparison grids
    print(f"\nGenerating comparison grids...")
    generate_comparison_grid(test_cases, available_backends, all_results,
                             OUTPUT_DIR)

    # Timing summary
    print_timing_summary(available_backends, all_results)

    print(f"\nResults saved to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
