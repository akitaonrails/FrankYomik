"""Unit tests for furigana annotation."""

from kindle.furigana import annotate


class TestAnnotate:
    def test_kanji_gets_furigana(self):
        segments = annotate("今日")
        assert any(s["needs_furigana"] for s in segments)
        assert any(s["furigana"] is not None for s in segments)

    def test_hiragana_no_furigana(self):
        segments = annotate("おはよう")
        for s in segments:
            assert not s["needs_furigana"]
            assert s["furigana"] is None

    def test_mixed_text_segments(self):
        segments = annotate("今日はいい天気ですね")
        texts = "".join(s["text"] for s in segments)
        assert texts == "今日はいい天気ですね"
        # Should have at least some kanji segments with furigana
        kanji_segs = [s for s in segments if s["needs_furigana"]]
        assert len(kanji_segs) >= 1

    def test_katakana_no_furigana(self):
        segments = annotate("ヒーロー")
        for s in segments:
            assert not s["needs_furigana"]

    def test_empty_string(self):
        segments = annotate("")
        # pykakasi returns one empty segment for empty input
        assert all(s["text"] == "" for s in segments)
        assert all(not s["needs_furigana"] for s in segments)
