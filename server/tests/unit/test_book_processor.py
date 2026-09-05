"""Unit tests for reading a prose page.

OCR and MeCab are injected, so these cover the part that decides whether
furigana lands on the right characters: how text read from a slice maps back
to positions down the column.
"""

from PIL import Image

from kindle.book_layout import PageLayout, TextColumn
from kindle.book_processor import Chunk, character_spans, read_columns


def layout_of(*columns, width=400, height=600):
    return PageLayout(columns=tuple(columns), dark_background=True,
                      ink_color=(200, 200, 200), width=width, height=height)


def column(x0=100, x1=130, y0=0, y1=360, runs=((0, 360),)):
    return TextColumn(x0=x0, x1=x1, y0=y0, y1=y1, runs=runs)


def scripted_ocr(*texts):
    """OCR that returns the given texts, one per call, then nothing."""
    remaining = list(texts)
    return lambda img: remaining.pop(0) if remaining else ""


def fake_annotate(text):
    """Annotate every kanji-looking character with a one-kana reading."""
    return [
        {"text": ch, "furigana": "か" if "一" <= ch <= "鿿" else None,
         "needs_furigana": "一" <= ch <= "鿿"}
        for ch in text
    ]


class TestCharacterSpans:
    def test_splits_a_chunk_evenly_across_its_characters(self):
        spans = character_spans([Chunk(y0=0, y1=120, text="あいう")])
        assert spans == [(0, 40), (40, 80), (80, 120)]

    def test_each_chunk_anchors_its_own_characters(self):
        # The second chunk holds more text in the same height, so a miscount
        # in one chunk cannot slide the characters of the other.
        spans = character_spans([Chunk(y0=0, y1=100, text="あい"),
                                 Chunk(y0=100, y1=200, text="うえお")])
        assert spans[:2] == [(0, 50), (50, 100)]
        assert spans[2][0] == 100 and spans[-1][1] == 200

    def test_ignores_empty_chunks(self):
        assert character_spans([Chunk(y0=0, y1=50, text="")]) == []


class TestReadColumns:
    def test_reads_a_column_and_places_its_readings(self):
        page = Image.new("RGB", (400, 600), (0, 0, 0))
        lay = layout_of(column())

        readings = read_columns(page, lay, scripted_ocr("風が頬"), fake_annotate)

        assert len(readings) == 1
        reading = readings[0]
        assert reading.text == "風が頬"
        assert [r.reading for r in reading.ruby] == ["か", "か"]
        # 風 is the first character, 頬 the third, of a 360px column.
        assert reading.ruby[0].y0 == 0 and reading.ruby[0].y1 == 120
        assert reading.ruby[1].y0 == 240 and reading.ruby[1].y1 == 360

    def test_kana_only_text_gets_no_ruby(self):
        page = Image.new("RGB", (400, 600), (0, 0, 0))
        readings = read_columns(page, layout_of(column()),
                                scripted_ocr("あいうえお"), fake_annotate)
        assert readings[0].ruby == ()

    def test_a_reading_spans_all_of_its_characters(self):
        page = Image.new("RGB", (400, 600), (0, 0, 0))
        multi = lambda text: [{"text": "今日", "furigana": "きょう",
                               "needs_furigana": True},
                              {"text": "は", "furigana": None,
                               "needs_furigana": False}]
        readings = read_columns(page, layout_of(column(y1=300, runs=((0, 300),))),
                                scripted_ocr("今日は"), multi)
        ruby = readings[0].ruby[0]
        assert ruby.reading == "きょう"
        assert ruby.y0 == 0 and ruby.y1 == 200  # both kanji, not just the first

    def test_a_dropped_segment_does_not_slide_later_readings(self):
        # annotate skips whitespace-only morphemes; locating segments by search
        # keeps everything after one anchored to the right characters.
        page = Image.new("RGB", (400, 600), (0, 0, 0))
        with_hole = lambda text: [
            {"text": "風", "furigana": "かぜ", "needs_furigana": True},
            {"text": "頬", "furigana": "ほお", "needs_furigana": True},
        ]
        readings = read_columns(page, layout_of(column(y1=300, runs=((0, 300),))),
                                scripted_ocr("風 頬"), with_hole)
        first, second = readings[0].ruby
        assert (first.y0, first.y1) == (0, 100)
        assert (second.y0, second.y1) == (200, 300), "the space must be counted"

    def test_columns_with_nothing_readable_are_dropped(self):
        page = Image.new("RGB", (400, 600), (0, 0, 0))
        readings = read_columns(page, layout_of(column(), column(x0=200, x1=230)),
                                scripted_ocr("", "風"), fake_annotate)
        assert [r.index for r in readings] == [1]

    def test_reports_progress_for_every_column(self):
        page = Image.new("RGB", (400, 600), (0, 0, 0))
        seen = []
        read_columns(page, layout_of(column(), column(x0=200, x1=230)),
                     scripted_ocr("風", "頬"), fake_annotate,
                     progress=lambda done, total: seen.append((done, total)))
        assert seen == [(1, 2), (2, 2)]

    def test_crops_stay_inside_the_column(self):
        # Ruby the publisher already placed lives in the gutter; reading it
        # back in would corrupt the column's text.
        page = Image.new("RGB", (400, 600), (0, 0, 0))
        boxes = []

        def recording_ocr(img):
            boxes.append(img.size)
            return "風"

        read_columns(page, layout_of(column(x0=100, x1=130)),
                     recording_ocr, fake_annotate)
        assert boxes and all(w <= 30 + 2 * 2 for w, _ in boxes)
