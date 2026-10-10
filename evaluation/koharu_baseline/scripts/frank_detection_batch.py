#!/usr/bin/env python3
"""Run Frank's balloon detector over a directory while keeping the model loaded."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np


SERVER_DIR = Path(os.environ.get("FRANK_SERVER_DIR", Path(__file__).resolve().parents[3] / "server"))
sys.path.insert(0, str(SERVER_DIR))

import kindle.bubble_detector as detector  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    images = sorted(args.input_dir.glob("*.png"))
    if not images:
        raise SystemExit(f"no PNG inputs in {args.input_dir}")

    for image_path in images:
        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None:
            raise SystemExit(f"could not read {image_path}")
        height, width = image.shape[:2]
        case_dir = args.output_dir / image_path.stem
        mask_dir = case_dir / "masks"
        mask_dir.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        regions = detector.detect_bubbles(image)
        elapsed = time.perf_counter() - started
        predictions = []
        for index, region in enumerate(regions):
            label = "onomatopoeia" if region.get("is_artwork") else "bubble"
            item = {
                "id": f"frank-{label}-{index:04d}",
                "class": label,
                "score": float(region["score"]),
                "bbox": [float(value) for value in region["bbox"]],
                "mask": None,
            }
            if label == "bubble":
                mask = detector.extract_bubble_mask_manga(image, region["bbox"])
                if mask is not None:
                    mask_path = mask_dir / f"{item['id']}.png"
                    if not cv2.imwrite(str(mask_path), np.asarray(mask, dtype=np.uint8)):
                        raise RuntimeError(f"failed to write {mask_path}")
                    item["mask"] = str(mask_path.relative_to(case_dir))
            predictions.append(item)
        payload = {
            "schema_version": 1,
            "system": "frank",
            "stage": "detection",
            "image": str(image_path),
            "width": width,
            "height": height,
            "elapsed_seconds": elapsed,
            "model": detector.MODEL_ID,
            "predictions": predictions,
        }
        (case_dir / "predictions.json").write_text(json.dumps(payload, indent=2) + "\n")
        print(json.dumps({"case": image_path.stem, "seconds": round(elapsed, 3), "predictions": len(predictions)}), flush=True)


if __name__ == "__main__":
    main()
