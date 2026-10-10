#!/usr/bin/env python3
"""Run one Frank inpainting backend over a controlled corpus with one model load."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image


SERVER_DIR = Path(os.environ.get("FRANK_SERVER_DIR", Path(__file__).resolve().parents[3] / "server"))
sys.path.insert(0, str(SERVER_DIR))

from webtoon.inpainter import get_backend_for_name  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--backend", default="lama")
    args = parser.parse_args()

    backend = get_backend_for_name(args.backend)
    if backend is None:
        raise SystemExit(f"backend unavailable: {args.backend}")
    rows = []
    try:
        for input_path in sorted(args.input_dir.glob("*__input.png")):
            identifier = input_path.name.removesuffix("__input.png")
            mask_path = input_path.with_name(f"{identifier}__mask.png")
            source = Image.open(input_path).convert("RGB")
            mask = Image.open(mask_path).convert("L")
            started = time.perf_counter()
            generated = backend.inpaint(source, mask).convert("RGB")
            elapsed = time.perf_counter() - started
            if generated.size != source.size:
                generated = generated.resize(source.size, Image.Resampling.LANCZOS)
            source_pixels = np.asarray(source)
            generated_pixels = np.asarray(generated)
            selected = np.asarray(mask) >= 127
            composite = source_pixels.copy()
            composite[selected] = generated_pixels[selected]
            destination = args.output_dir / f"{identifier}.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(composite).save(destination)
            row = {"id": identifier, "engine": f"frank_{args.backend}", "seconds": round(elapsed, 3)}
            rows.append(row)
            print(json.dumps(row), flush=True)
    finally:
        backend.unload()
    (args.output_dir / "results.json").write_text(json.dumps(rows, indent=2) + "\n")


if __name__ == "__main__":
    main()
