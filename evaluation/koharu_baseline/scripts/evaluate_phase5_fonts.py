#!/usr/bin/env python3
"""Audit shipped OFL fonts and Comic Neue fit on frozen layout cases."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPOSITORY = Path(__file__).resolve().parents[3]
SERVER = REPOSITORY / "server"
sys.path.insert(0, str(SERVER))

from kindle.text_renderer import _word_wrap  # noqa: E402

FONTS = {
    "dialogue_regular": SERVER / "fonts/ComicNeue-Regular.otf",
    "dialogue_bold": SERVER / "fonts/ComicNeue-Bold.otf",
    "japanese": SERVER / "fonts/genei_antique/GenEiAntiquePv5-M.ttf",
    "japanese_fallback": SERVER / "fonts/shippori_antique/ShipporiAntique-Regular.ttf",
}
SAMPLES = {
    "ascii": "AZ az 09 !?", "portuguese": "áàâãçéêíóôõúüÁÇ — João",
    "japanese": "日本語かなカナ", "korean": "한글", "arabic": "العربية",
    "hebrew": "עברית", "devanagari": "देवनागरी", "emoji": "😀",
}
LAYOUT_CASES = (
    {"id": "short", "text": "Wait!", "width": 150, "height": 70, "target": 34},
    {"id": "portuguese", "text": "Você já viu isso?", "width": 210, "height": 100, "target": 34},
    {"id": "long_dialogue",
     "text": "We need to leave before the doors close again.",
     "width": 230, "height": 135, "target": 34},
    {"id": "unbroken", "text": "anticonstitucionalissimamente",
     "width": 175, "height": 110, "target": 34},
)


def charset(path: Path) -> set[int]:
    raw = subprocess.check_output(
        ["fc-query", "--format=%{charset}", str(path)], text=True,
    )
    points: set[int] = set()
    for token in raw.split():
        if "-" in token:
            start, end = token.split("-", 1)
            points.update(range(int(start, 16), int(end, 16) + 1))
        else:
            points.add(int(token, 16))
    return points


def metadata(path: Path) -> tuple[str, str]:
    raw = subprocess.check_output(
        ["fc-query", "--format=%{family}|%{style}", str(path)], text=True,
    )
    family, _, style = raw.partition("|")
    return family, style


def audit() -> dict[str, object]:
    result: dict[str, object] = {}
    for role, path in FONTS.items():
        available = charset(path)
        family, style = metadata(path)
        result[role] = {
            "path": str(path.relative_to(REPOSITORY)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "family": family, "style": style, "glyph_count": len(available),
            "complete_samples": {
                name: all(ord(character) in available for character in text)
                for name, text in SAMPLES.items()
            },
        }
    return result


def fit_case(path: Path, case: dict[str, object]) -> dict[str, object]:
    text = str(case["text"])
    width, height = int(case["width"]), int(case["height"])
    target = int(case["target"])
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    chosen, lines = 8, []
    for size in range(target, 7, -1):
        font = ImageFont.truetype(str(path), size)
        candidate = _word_wrap(text, font, width, draw)
        if candidate and len(candidate) * int(size * 1.2) <= height:
            chosen, lines = size, candidate
            break
    font = ImageFont.truetype(str(path), chosen)
    widths = [draw.textbbox((0, 0), line, font=font)[2] for line in lines]
    return {
        "font_size": chosen, "line_count": len(lines), "lines": lines,
        "max_line_width": max(widths, default=0),
        "overflow": not lines or max(widths, default=0) > width
        or len(lines) * int(chosen * 1.2) > height,
    }


def main() -> None:
    cases = {}
    for case in LAYOUT_CASES:
        cases[str(case["id"])] = {
            "box": [case["width"], case["height"]],
            "target_font_size": case["target"],
            "regular": fit_case(FONTS["dialogue_regular"], case),
            "bold": fit_case(FONTS["dialogue_bold"], case),
        }
    print(json.dumps({
        "coverage": audit(), "frozen_layout_cases": cases,
        "license": "All audited files are distributed under SIL OFL 1.1",
    }, indent=2, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
