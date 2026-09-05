"""Drawing furigana beside the text of a rasterised prose page.

Nothing about the page is redrawn. The typesetting is already good and the
reader is reading it, so the only change is kana added in the gutter — which
is where vertical Japanese puts ruby anyway, to the right of its base text.

Publisher ruby is already there for some words. Those gutters are left alone
rather than drawn over.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .book_layout import PageLayout, ink_mask
from .book_processor import ColumnReading
from .config import FONT_JP

# Ruby is conventionally about half the size of its base text; a little under
# keeps a long reading from overflowing a narrow gutter.
FURIGANA_SIZE_RATIO = 0.45
MIN_FURIGANA_SIZE = 7
# Breathing room between the text column and its ruby.
GUTTER_GAP_PX = 2
# Above this share of ink, the gutter already holds the publisher's own ruby.
OCCUPIED_INK_RATIO = 0.02


@lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_JP, size)
    except OSError:
        return ImageFont.load_default()


def _occupied(mask: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> bool:
    """Whether a gutter slot already has something in it."""
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(mask.shape[1], x1), min(mask.shape[0], y1)
    if x1 <= x0 or y1 <= y0:
        return True
    return float(mask[y0:y1, x0:x1].mean()) > OCCUPIED_INK_RATIO


def _draw_centered(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont,
                   char: str, color: tuple[int, int, int],
                   x: float, y: float, cell: float) -> None:
    """Draw one kana centred in its cell.

    Pillow places text by the font's ascender, which leaves kana sitting low
    and out of step with the kanji they annotate.
    """
    left, top, right, bottom = font.getbbox(char)
    draw.text((x + (cell - (right - left)) / 2 - left,
               y + (cell - (bottom - top)) / 2 - top),
              char, font=font, fill=color)


def render(img: Image.Image,
           layout: PageLayout,
           readings: Sequence[ColumnReading],
           scale: float = 1.0) -> Image.Image:
    """Return the page with furigana added in the gutters.

    [scale] supersamples the whole page. Furigana on a prose page is small by
    necessity — the gutter is one glyph wide — so rendering above 1.0 buys
    sharpness under the reader's magnifier, at four times the pixels per step.
    """
    mask, _, _ = ink_mask(img)

    out = img.convert("RGB")
    if scale != 1.0:
        out = out.resize((int(out.width * scale), int(out.height * scale)),
                         Image.LANCZOS)
    else:
        out = out.copy()
    draw = ImageDraw.Draw(out)
    color = layout.ink_color

    for reading in readings:
        if not reading.ruby:
            continue
        gutter_x0, gutter_x1 = layout.gutter_right_of(reading.index)
        gutter = gutter_x1 - gutter_x0 - GUTTER_GAP_PX
        if gutter < MIN_FURIGANA_SIZE:
            continue

        size = max(MIN_FURIGANA_SIZE,
                   min(int(reading.column.pitch * FURIGANA_SIZE_RATIO), gutter))
        font = _font(int(size * scale))
        x = gutter_x0 + GUTTER_GAP_PX

        for ruby in reading.ruby:
            span = len(ruby.reading) * size
            # Centre the reading on the characters it belongs to, the way ruby
            # sits beside its base text.
            top = (ruby.y0 + ruby.y1) // 2 - span // 2
            if _occupied(mask, x, top, x + size, top + span):
                continue
            for i, ch in enumerate(ruby.reading):
                _draw_centered(draw, font, ch, color,
                               x * scale, (top + i * size) * scale, size * scale)

    return out
