#!/usr/bin/env python3
"""Run Frank's Japanese OCR over a crop corpus while keeping the model loaded."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from PIL import Image


SERVER_DIR = Path(os.environ.get("FRANK_SERVER_DIR", Path(__file__).resolve().parents[3] / "server"))
sys.path.insert(0, str(SERVER_DIR))

from kindle.ocr import extract_text  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text())["cases"]
    results = {}
    for index, case in enumerate(manifest, start=1):
        image = Image.open(args.manifest.parent / case["image"]).convert("RGB")
        started = time.perf_counter()
        text = extract_text(image)
        results[case["id"]] = {
            "id": case["id"],
            "text": text,
            "seconds": round(time.perf_counter() - started, 3),
        }
        print(f"{index}/{len(manifest)} {case['id']}", flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"cases": results}, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
