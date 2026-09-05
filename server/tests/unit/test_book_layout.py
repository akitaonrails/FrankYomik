"""Unit tests for prose page layout analysis.

Pages are drawn rather than loaded so the expected geometry is known exactly:
column count, pitch and gutters all follow from what the test typeset.
"""

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from kindle.book_layout import (
    MIN_PROSE_COLUMNS,
    PageLayout,
    TextColumn,
    analyze,
    column_chunks,
)
from kindle.config import FONT_JP

SAMPLE = "風が頬を撫で髪を優しく揺らしている今日は良い天気だと思った"


def _font(size):
    try:
        return ImageFont.truetype(FONT_JP, size)
    except OSError:  # pragma: no cover - depends on the host's fonts
        pytest.skip("Japanese font unavailable")


def prose_page(columns=10, glyphs=20, pitch=30, gutter=30,
               dark=True, margin=40, gaps=None):
    """Draw a page of evenly typeset vertical columns, right to left."""
    width = margin * 2 + columns * pitch + (columns - 1) * gutter
    height = margin * 2 + glyphs * pitch
    img = Image.new("RGB", (width, height), (0, 0, 0) if dark else (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = _font(int(pitch * 0.9))
    ink = (200, 200, 200) if dark else (30, 30, 30)

    for c in range(columns):
        x = margin + c * (pitch + gutter)
        skip = (gaps or {}).get(c, ())
        for g in range(glyphs):
            if g in skip:
                continue
            draw.text((x, margin + g * pitch), SAMPLE[(c + g) % len(SAMPLE)],
                      font=font, fill=ink)
    return img


class TestAnalyze:
    def test_finds_every_column(self):
        layout = analyze(prose_page(columns=12))
        assert len(layout.columns) == 12
        assert layout.is_prose

    def test_columns_are_ordered_left_to_right(self):
        layout = analyze(prose_page(columns=6))
        xs = [c.x0 for c in layout.columns]
        assert xs == sorted(xs)

    def test_pitch_is_consistent_and_tracks_the_typeset_size(self):
        # Pitch is measured from ink, which runs a little narrower than the
        # em box because kana do not fill their cell. What matters is that
        # every column agrees, since pitch drives chunking and ruby size.
        layout = analyze(prose_page(columns=6, pitch=30))
        widths = [c.width for c in layout.columns]
        assert max(widths) - min(widths) <= 2, widths
        assert all(21 <= w <= 30 for w in widths), widths

    def test_detects_a_dark_page(self):
        layout = analyze(prose_page(dark=True))
        assert layout.dark_background
        # Ink is what the reader sees, not the antialiased average.
        assert min(layout.ink_color) > 150

    def test_detects_a_light_page(self):
        layout = analyze(prose_page(dark=False))
        assert not layout.dark_background
        assert max(layout.ink_color) < 100

    def test_gutter_sits_between_a_column_and_its_right_neighbour(self):
        layout = analyze(prose_page(columns=5, pitch=30, gutter=30))
        x0, x1 = layout.gutter_right_of(0)
        assert x0 == layout.columns[0].x1
        assert x1 == layout.columns[1].x0
        assert 20 <= x1 - x0 <= 40

    def test_last_column_gutters_to_the_page_edge(self):
        layout = analyze(prose_page(columns=4))
        x0, x1 = layout.gutter_right_of(3)
        assert x1 == layout.width


class TestIsProse:
    def test_a_page_of_columns_is_prose(self):
        assert analyze(prose_page(columns=MIN_PROSE_COLUMNS + 2)).is_prose

    def test_too_few_columns_is_not_prose(self):
        layout = analyze(prose_page(columns=2))
        assert not layout.is_prose

    def test_irregular_blocks_are_not_prose(self):
        # Stand-in for manga: a handful of boxes of unrelated sizes.
        img = Image.new("RGB", (900, 700), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        for x, y, w, h in [(40, 40, 200, 300), (300, 80, 60, 500),
                           (420, 200, 350, 120), (500, 400, 90, 250),
                           (700, 60, 150, 600), (100, 420, 120, 200)]:
            draw.rectangle([x, y, x + w, y + h], fill=(0, 0, 0))
        assert not analyze(img).is_prose

    def test_an_empty_page_is_not_prose(self):
        assert not analyze(Image.new("RGB", (400, 400), (0, 0, 0))).is_prose


class TestRuns:
    def test_a_full_column_is_one_run(self):
        layout = analyze(prose_page(columns=6, glyphs=16))
        assert all(len(c.runs) == 1 for c in layout.columns)

    def test_a_paragraph_break_splits_the_runs(self):
        # Three blank glyph cells in the middle of one column.
        layout = analyze(prose_page(columns=6, glyphs=20, gaps={2: (8, 9, 10)}))
        assert len(layout.columns[2].runs) == 2
        assert all(len(c.runs) == 1 for i, c in enumerate(layout.columns) if i != 2)

    def test_column_bounds_follow_the_runs(self):
        layout = analyze(prose_page(columns=5, glyphs=12))
        for column in layout.columns:
            assert column.y0 == column.runs[0][0]
            assert column.y1 == column.runs[-1][1]


class TestColumnChunks:
    def test_a_short_column_is_a_single_chunk(self):
        column = TextColumn(x0=0, x1=30, y0=0, y1=200, runs=((0, 200),))
        assert column_chunks(column, glyphs_per_chunk=12) == [(0, 200)]

    def test_a_long_column_is_split_into_readable_slices(self):
        column = TextColumn(x0=0, x1=30, y0=0, y1=1200, runs=((0, 1200),))
        chunks = column_chunks(column, glyphs_per_chunk=12)
        assert len(chunks) == 3
        assert chunks[0][0] == 0 and chunks[-1][1] == 1200
        # Contiguous, so no glyph falls between two chunks.
        assert all(a[1] == b[0] for a, b in zip(chunks, chunks[1:]))

    def test_chunks_never_cover_blank_paper(self):
        # OCR asked to read an empty slice invents text, so the gap is skipped.
        column = TextColumn(x0=0, x1=30, y0=0, y1=900,
                            runs=((0, 300), (600, 900)))
        chunks = column_chunks(column, glyphs_per_chunk=12)
        assert chunks == [(0, 300), (600, 900)]
        assert not any(c[0] >= 300 and c[1] <= 600 for c in chunks)

    def test_falls_back_to_the_whole_column_without_runs(self):
        column = TextColumn(x0=0, x1=30, y0=10, y1=250)
        assert column_chunks(column) == [(10, 250)]

    def test_an_empty_column_has_no_chunks(self):
        assert column_chunks(TextColumn(x0=0, x1=30, y0=5, y1=5)) == []
