#!/usr/bin/env python3
"""Regenerate the browser-test fixtures.

The originals were a real page from a commercial novel and the render the
server made from it, which is not something to commit to a public repository.
What the tests actually depend on is the *shape* of such a page: dark ground,
fine vertical text, and — the property that broke the render check — a
luminance standard deviation of about 0.02 once reduced to 16 pixels.

Run from the server directory so the Japanese font resolves:

    cd server && .venv/bin/python ../extension/test/browser/fixtures/make-fixtures.py
"""

import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.getcwd())
from kindle.config import FONT_JP  # noqa: E402

WIDTH, HEIGHT = 2200, 1257
COLUMNS = 29
TEXT = "風が頬を撫で髪を優しく揺らしている今日は良い天気だと思った街の灯を見ているのが好きだ"
HERE = os.path.dirname(os.path.abspath(__file__))


def draw_page() -> Image.Image:
    page = Image.new("RGB", (WIDTH, HEIGHT), (8, 8, 8))
    draw = ImageDraw.Draw(page)
    font = ImageFont.truetype(FONT_JP, 26)
    for column in range(COLUMNS):
        x = 120 + column * 68
        for row in range(28):
            draw.text((x, 60 + row * 30), TEXT[(column * 3 + row) % len(TEXT)],
                      font=font, fill=(150, 150, 150))
    return page


def add_furigana(page: Image.Image) -> Image.Image:
    render = page.copy()
    draw = ImageDraw.Draw(render)
    font = ImageFont.truetype(FONT_JP, 12)
    for column in range(COLUMNS):
        x = 120 + column * 68 + 30
        for row in range(0, 28, 2):
            draw.text((x, 62 + row * 30), "かぜ"[(row // 2) % 2], font=font,
                      fill=(150, 150, 150))
    return render


def main() -> None:
    page = draw_page()
    page.save(os.path.join(HERE, "page.png"))
    add_furigana(page).save(os.path.join(HERE, "render-of-page.png"))

    signature = np.asarray(page.convert("L").resize((16, 16), Image.BILINEAR),
                           dtype=float) / 255
    deviation = float(signature.std())
    print(f"page luma deviation at 16x16: {deviation:.4f}")
    # The fixture is only useful while it keeps the property under test: a real
    # novel page measured 0.0198, and the check misbehaved because of it.
    assert 0.015 < deviation < 0.03, "fixture no longer reproduces a low-contrast page"


if __name__ == "__main__":
    main()
