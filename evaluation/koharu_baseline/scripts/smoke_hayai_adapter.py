#!/usr/bin/env python3
"""Exercise Frank's pinned Hayai adapter in a production-shaped worker image."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from PIL import Image

from kindle.hayai_ocr import HayaiOcrBackend


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args()
    if args.repeats < 1:
        raise SystemExit("--repeats must be positive")

    image = Image.open(args.input).convert("RGB")
    backend = HayaiOcrBackend(device="cuda", timeout_seconds=args.timeout)
    runs: list[dict[str, object]] = []
    try:
        for index in range(args.repeats):
            started = time.perf_counter()
            recognized = backend(image)
            runs.append({
                "index": index,
                "seconds": time.perf_counter() - started,
                "text": recognized,
                "matches_expected": recognized == args.expected,
            })
    finally:
        backend._stop_process()

    report = {
        "schema_version": 1,
        "input": str(args.input),
        "expected": args.expected,
        "runs": runs,
        "accepted": all(run["matches_expected"] for run in runs),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False))
    if not report["accepted"]:
        raise SystemExit("Hayai adapter output differs from the frozen reference")


if __name__ == "__main__":
    main()
