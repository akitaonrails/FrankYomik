"""Unit tests for drawing furigana beside a prose page.

The page itself must come back untouched — the reader is reading it — so these
check both what is added and what is left alone.
"""

import numpy as np
from PIL import Image, ImageDraw

from kindle.book_layout import PageLayout, TextColumn
from kindle.book_processor import ColumnReading, Ruby
from kindle.book_renderer import render


def page(width=300, height=400, dark=True):
    img = Image.new("RGB", (width, height), (0, 0, 0) if dark else (255, 255, 255))
    draw = ImageDraw.Draw(img)
    ink = (200, 200, 200) if dark else (20, 20, 20)
    # One column of solid cells standing in for glyphs.
    for i in range(8):
        draw.rectangle([100, 20 + i * 40, 130, 50 + i * 40], fill=ink)
    return img


def layout(width=300, height=400, dark=True):
    column = TextColumn(x0=100, x1=130, y0=20, y1=340, runs=((20, 340),))
    return PageLayout(columns=(column,), dark_background=dark,
                      ink_color=(200, 200, 200) if dark else (20, 20, 20),
                      width=width, height=height)


def reading(*ruby):
    lay = layout()
    return ColumnReading(index=0, column=lay.columns[0], chunks=(),
                         text="", ruby=tuple(ruby))


def ink_in(img, box):
    """How much ink sits in a region, as a share of its pixels."""
    crop = np.asarray(img.convert("L").crop(box), dtype=np.int16)
    background = int(np.median(np.asarray(img.convert("L"))))
    return float((np.abs(crop - background) > 60).mean())


class TestRender:
    def test_leaves_the_page_itself_untouched(self):
        src = page()
        out = render(src, layout(), [reading(Ruby("かぜ", 20, 60))])
        before = np.asarray(src.crop((100, 20, 130, 340)))
        after = np.asarray(out.crop((100, 20, 130, 340)))
        assert np.array_equal(before, after)

    def test_draws_the_reading_in_the_gutter(self):
        out = render(page(), layout(), [reading(Ruby("かぜ", 20, 60))])
        assert ink_in(out, (133, 10, 175, 80)) > 0.02

    def test_a_reading_sits_beside_its_own_characters(self):
        out = render(page(), layout(), [reading(Ruby("かぜ", 220, 260))])
        assert ink_in(out, (133, 200, 175, 280)) > 0.02
        assert ink_in(out, (133, 20, 175, 100)) == 0.0

    def test_nothing_is_drawn_without_readings(self):
        src = page()
        out = render(src, layout(), [reading()])
        assert np.array_equal(np.asarray(src), np.asarray(out))

    def test_leaves_a_gutter_the_publisher_already_used(self):
        # The book ships its own ruby for some words; drawing over it would
        # stack two readings on one another.
        src = page()
        ImageDraw.Draw(src).rectangle([132, 20, 150, 60], fill=(200, 200, 200))
        occupied = np.asarray(src.crop((130, 15, 175, 65)))

        out = render(src, layout(), [reading(Ruby("かぜ", 20, 60))])

        assert np.array_equal(occupied, np.asarray(out.crop((130, 15, 175, 65))))

    def test_matches_the_ink_colour_of_the_page(self):
        out = render(page(dark=True), layout(dark=True),
                     [reading(Ruby("かぜ", 20, 60))])
        gutter = np.asarray(out.convert("L").crop((133, 10, 175, 80)))
        assert gutter.max() > 150, "kana on a dark page must be light"

    def test_supersampling_scales_the_whole_page(self):
        out = render(page(), layout(), [reading(Ruby("かぜ", 20, 60))], scale=2.0)
        assert out.size == (600, 800)
        assert ink_in(out, (266, 20, 350, 160)) > 0.02

    def test_skips_a_gutter_too_narrow_to_read(self):
        narrow = PageLayout(
            columns=(TextColumn(x0=100, x1=130, y0=20, y1=340, runs=((20, 340),)),
                     TextColumn(x0=133, x1=163, y0=20, y1=340, runs=((20, 340),))),
            dark_background=True, ink_color=(200, 200, 200),
            width=300, height=400,
        )
        src = page()
        out = render(src, narrow, [ColumnReading(index=0, column=narrow.columns[0],
                                                 chunks=(), text="",
                                                 ruby=(Ruby("かぜ", 20, 60),))])
        assert np.array_equal(np.asarray(src), np.asarray(out))
