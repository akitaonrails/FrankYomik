"""Layout analysis for rasterised prose pages (Kindle reflowable books).

A novel page is not manga. There are no balloons and no artwork, just evenly
typeset columns of vertical Japanese — and that regularity is what makes the
page tractable without any model: a projection profile finds the columns
exactly, and their width gives the glyph pitch, which in turn gives every
character's position down the column.

The same regularity separates prose from manga, so [analyze] doubles as the
check that a page belongs in this pipeline at all.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

# A page is "dark" when its ink is lighter than its background (Kindle's dark
# theme). Ink is whatever sits far enough from the background level.
INK_DISTANCE = 60

# A body column runs most of the page; publisher ruby and page furniture do not.
MIN_COLUMN_HEIGHT_RATIO = 0.35
# Ruby columns are roughly half the width of the text they annotate, so a
# generous band around the median still excludes them.
COLUMN_WIDTH_TOLERANCE = (0.70, 1.45)
MIN_COLUMN_WIDTH_PX = 6
MIN_RUN_GAP_PX = 2
# A gap wider than one glyph is a paragraph break or the end of the text, not
# the space inside a comma.
RUN_GAP_PITCH_RATIO = 1.0

# Prose fingerprint: many columns of near-identical width. Manga pages have
# neither the count nor the uniformity.
MIN_PROSE_COLUMNS = 5
MAX_PROSE_WIDTH_SPREAD = 0.18

# manga-ocr expects about one bubble's worth of text; a whole column is far
# outside that and comes back as gibberish. Roughly a dozen glyphs reads
# reliably, and keeping chunks short also keeps position errors local.
GLYPHS_PER_CHUNK = 12


@dataclass(frozen=True)
class TextColumn:
    """One column of vertical text, in page pixel coordinates.

    [runs] are the stretches that actually hold glyphs. A column is rarely full
    — paragraphs end, and the last column of a page trails off — and OCR asked
    to read blank paper invents text to fill it, so the blanks are tracked
    rather than sliced through.
    """

    x0: int
    x1: int
    y0: int
    y1: int
    runs: tuple[tuple[int, int], ...] = ()

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def pitch(self) -> int:
        """Glyph advance down the column.

        CJK glyphs are square, so the column's width is also the distance from
        one character to the next.
        """
        return self.width


@dataclass(frozen=True)
class PageLayout:
    """What a prose page looks like: its columns and its colour polarity."""

    columns: tuple[TextColumn, ...]
    dark_background: bool
    ink_color: tuple[int, int, int]
    width: int
    height: int

    @property
    def is_prose(self) -> bool:
        """Whether this page is typeset prose rather than manga artwork."""
        if len(self.columns) < MIN_PROSE_COLUMNS:
            return False
        widths = np.array([c.width for c in self.columns], dtype=float)
        median = float(np.median(widths))
        if median <= 0:
            return False
        spread = float(np.max(np.abs(widths - median))) / median
        return spread <= MAX_PROSE_WIDTH_SPREAD

    def gutter_right_of(self, index: int) -> tuple[int, int]:
        """The empty strip to the right of column [index].

        Vertical Japanese places ruby on the right of its base text, and the
        page is read right to left, so this is the space between a column and
        the one the reader has just finished.
        """
        column = self.columns[index]
        right_neighbours = [c.x0 for c in self.columns if c.x0 > column.x1]
        edge = min(right_neighbours) if right_neighbours else self.width
        return column.x1, edge


def ink_mask(img: Image.Image) -> tuple[np.ndarray, bool, tuple[int, int, int]]:
    """Split a page into ink and background.

    Returns the mask, whether the page is light-on-dark, and the mean ink
    colour so rendered furigana can match the text already on the page.
    """
    gray = np.asarray(img.convert("L"), dtype=np.int16)
    background = int(np.median(gray))
    mask = np.abs(gray - background) > INK_DISTANCE
    dark_background = background < 128

    rgb = np.asarray(img.convert("RGB"), dtype=np.int16)
    if mask.any():
        # Antialiased glyph edges drag the mean towards the background, which
        # would render furigana visibly dimmer than the text beside it. The
        # glyph core is what the eye reads, so take that end of the range.
        luma = gray[mask]
        percentile = 90 if dark_background else 10
        core = luma >= np.percentile(luma, percentile) if dark_background \
            else luma <= np.percentile(luma, percentile)
        pixels = rgb[mask][core] if core.any() else rgb[mask]
        ink = tuple(int(max(0, min(255, v))) for v in pixels.mean(axis=0))
    else:
        ink = (255, 255, 255) if dark_background else (0, 0, 0)
    return mask, dark_background, ink  # type: ignore[return-value]


def _ink_runs(present: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True runs, merging gaps too small to be a real gutter."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(present):
        if value and start is None:
            start = index
        elif not value and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(present)))

    merged: list[tuple[int, int]] = []
    for run in runs:
        if merged and run[0] - merged[-1][1] < MIN_RUN_GAP_PX:
            merged[-1] = (merged[-1][0], run[1])
        else:
            merged.append(run)
    return [r for r in merged if r[1] - r[0] >= MIN_COLUMN_WIDTH_PX]


def _column_runs(mask: np.ndarray, x0: int, x1: int, pitch: int) -> tuple[tuple[int, int], ...]:
    """Stretches of a column that hold glyphs, in reading order."""
    rows = mask[:, x0:x1].any(axis=1)
    gap_limit = max(2, int(pitch * RUN_GAP_PITCH_RATIO))
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for y, value in enumerate(rows):
        if value and start is None:
            start = y
        elif not value and start is not None:
            runs.append((start, y))
            start = None
    if start is not None:
        runs.append((start, len(rows)))

    merged: list[tuple[int, int]] = []
    for run in runs:
        if merged and run[0] - merged[-1][1] <= gap_limit:
            merged[-1] = (merged[-1][0], run[1])
        else:
            merged.append(run)
    # A stray speck is not a glyph.
    return tuple(r for r in merged if r[1] - r[0] >= pitch // 2)


def analyze(img: Image.Image) -> PageLayout:
    """Find the text columns on a rasterised prose page."""
    mask, dark_background, ink = ink_mask(img)
    height, width = mask.shape

    rows_with_ink = np.where(mask.any(axis=1))[0]
    text_height = (rows_with_ink[-1] - rows_with_ink[0]) if len(rows_with_ink) else 0

    per_column = mask.sum(axis=0)
    runs = _ink_runs(per_column > max(3, int(0.02 * max(text_height, 1))))

    tall: list[tuple[int, int]] = []
    for x0, x1 in runs:
        ys = np.where(mask[:, x0:x1].any(axis=1))[0]
        if len(ys) and (ys[-1] - ys[0]) >= text_height * MIN_COLUMN_HEIGHT_RATIO:
            tall.append((x0, x1))

    columns: list[TextColumn] = []
    if tall:
        # Ruby and headings differ in width from the body; the median is the
        # body measure, so anything far from it is not a body column.
        median_width = float(np.median([x1 - x0 for x0, x1 in tall]))
        low, high = COLUMN_WIDTH_TOLERANCE
        for x0, x1 in tall:
            if not (median_width * low <= (x1 - x0) <= median_width * high):
                continue
            runs = _column_runs(mask, x0, x1, x1 - x0)
            if not runs:
                continue
            # Bound the column by its glyph runs: page furniture that happens
            # to share this x-range (a footer, a progress bar) is not text.
            columns.append(TextColumn(
                x0=x0, x1=x1, y0=runs[0][0], y1=runs[-1][1], runs=runs,
            ))

    return PageLayout(
        columns=tuple(columns),
        dark_background=dark_background,
        ink_color=ink,
        width=width,
        height=height,
    )


def column_chunks(column: TextColumn,
                  glyphs_per_chunk: int = GLYPHS_PER_CHUNK) -> list[tuple[int, int]]:
    """Split a column into OCR-sized vertical slices.

    Slices follow the column's glyph runs, so no chunk covers blank paper, and
    they divide each run evenly: a chunk's height over the characters read from
    it gives the pitch actually used, which keeps a dropped or invented
    character from sliding the rest of the column.
    """
    runs = column.runs or ((column.y0, column.y1),)
    chunk_span = max(1, column.pitch * glyphs_per_chunk)
    chunks: list[tuple[int, int]] = []
    for y0, y1 in runs:
        span = y1 - y0
        if span <= 0:
            continue
        count = max(1, round(span / chunk_span))
        step = span / count
        chunks.extend(
            (y0 + int(round(i * step)), y1 if i == count - 1 else y0 + int(round((i + 1) * step)))
            for i in range(count)
        )
    return chunks
