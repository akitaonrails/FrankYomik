#!/usr/bin/env python3
"""Expose Frank stages through stable file-based benchmark contracts.

Model imports happen only after argument parsing. Merely asking for --help or
validating the dataset cannot initialize CUDA.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from PIL import Image

from common import REPO_DIR, write_json


SERVER_DIR = Path(os.environ.get("FRANK_SERVER_DIR", REPO_DIR / "server"))
sys.path.insert(0, str(SERVER_DIR))


def detection(args: argparse.Namespace) -> None:
    import cv2
    import numpy as np

    import kindle.bubble_detector as detector
    from kindle.text_detector import detect_text_regions

    image_cv = cv2.imread(str(args.input), cv2.IMREAD_COLOR)
    if image_cv is None:
        raise SystemExit(f"could not read image: {args.input}")
    height, width = image_cv.shape[:2]
    args.mask_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    bubbles = detector.detect_bubbles(image_cv)
    text_regions = detect_text_regions(image_cv)
    elapsed = time.perf_counter() - started

    items = []
    for index, region in enumerate(bubbles):
        label = "onomatopoeia" if region.get("is_artwork") else "bubble"
        item = {
            "id": f"frank-{label}-{index:04d}",
            "class": label,
            "score": float(region["score"]),
            "bbox": [float(value) for value in region["bbox"]],
            "mask": None,
        }
        mask = None
        if not region.get("is_artwork"):
            mask = detector.extract_bubble_mask_manga(image_cv, region["bbox"])
        if mask is not None:
            mask_path = args.mask_dir / f"{item['id']}.png"
            if not cv2.imwrite(str(mask_path), np.asarray(mask, dtype=np.uint8)):
                raise RuntimeError(f"failed to write {mask_path}")
            item["mask"] = str(mask_path.relative_to(args.output.parent))
        items.append(item)

    for index, region in enumerate(text_regions):
        items.append(
            {
                "id": f"frank-text-{index:04d}",
                "class": "text",
                "score": float(region.confidence),
                "bbox": [float(value) for value in region.bbox],
                "mask": None,
            }
        )

    write_json(
        args.output,
        {
            "schema_version": 1,
            "system": "frank",
            "stage": "detection",
            "image": str(args.input),
            "width": width,
            "height": height,
            "elapsed_seconds": elapsed,
            "model": detector.MODEL_ID,
            "model_revision": getattr(getattr(detector._model, "config", None), "_commit_hash", None),
            "predictions": items,
        },
    )


def ocr(args: argparse.Namespace) -> None:
    image = Image.open(args.input).convert("RGB")
    started = time.perf_counter()
    if args.language == "ja":
        from kindle.ocr import extract_text

        text = extract_text(image)
        engine = "manga_ocr"
    elif args.language == "ko":
        from webtoon.ocr import detect_and_read

        detections = detect_and_read(image)
        detections.sort(key=lambda item: (item.bbox_rect[1], item.bbox_rect[0]))
        text = " ".join(item.text for item in detections)
        engine = "easyocr_ko"
    else:
        raise SystemExit(f"unsupported Frank OCR language: {args.language}")
    write_json(
        args.output,
        {
            "schema_version": 1,
            "system": "frank",
            "stage": "ocr",
            "engine": engine,
            "language": args.language,
            "text": text,
            "elapsed_seconds": time.perf_counter() - started,
        },
    )


def inpaint(args: argparse.Namespace) -> None:
    import numpy as np

    from webtoon.inpainter import get_backend_for_name

    source = Image.open(args.input).convert("RGB")
    mask = Image.open(args.mask).convert("L")
    if source.size != mask.size:
        raise SystemExit("input and mask dimensions differ")
    backend = get_backend_for_name(args.backend)
    if backend is None:
        raise SystemExit(f"Frank backend unavailable: {args.backend}")
    try:
        started = time.perf_counter()
        generated = backend.inpaint(source, mask).convert("RGB")
        elapsed = time.perf_counter() - started
    finally:
        backend.unload()
    if generated.size != source.size:
        generated = generated.resize(source.size, Image.Resampling.LANCZOS)
    # Evaluate the model inside the requested mask only. This also makes the
    # outside-mask invariant explicit and comparable across implementations.
    source_pixels = np.asarray(source)
    generated_pixels = np.asarray(generated)
    mask_pixels = np.asarray(mask) >= 127
    composite = source_pixels.copy()
    composite[mask_pixels] = generated_pixels[mask_pixels]
    Image.fromarray(composite).save(args.output)
    write_json(
        args.metadata,
        {
            "schema_version": 1,
            "system": "frank",
            "stage": "inpainting",
            "engine": args.backend,
            "elapsed_seconds": elapsed,
            "output": str(args.output),
        },
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(dest="command", required=True)

    detect = commands.add_parser("detection")
    detect.add_argument("--input", type=Path, required=True)
    detect.add_argument("--output", type=Path, required=True)
    detect.add_argument("--mask-dir", type=Path, required=True)
    detect.set_defaults(function=detection)

    recognize = commands.add_parser("ocr")
    recognize.add_argument("--input", type=Path, required=True)
    recognize.add_argument("--language", choices=("ja", "ko"), required=True)
    recognize.add_argument("--output", type=Path, required=True)
    recognize.set_defaults(function=ocr)

    paint = commands.add_parser("inpaint")
    paint.add_argument("--input", type=Path, required=True)
    paint.add_argument("--mask", type=Path, required=True)
    paint.add_argument("--backend", required=True)
    paint.add_argument("--output", type=Path, required=True)
    paint.add_argument("--metadata", type=Path, required=True)
    paint.set_defaults(function=inpaint)
    return result


if __name__ == "__main__":
    arguments = parser().parse_args()
    arguments.function(arguments)
