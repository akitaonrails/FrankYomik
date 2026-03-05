"""Regression tests for OCR text validation."""

import pytest
from kindle.ocr import is_valid_japanese


class TestValidJapanese:
    """Test is_valid_japanese() correctly accepts real dialogue."""

    def test_accepts_mixed_hiragana_kanji(self):
        assert is_valid_japanese("今日はいい天気ですね")

    def test_accepts_katakana_dialogue(self):
        assert is_valid_japanese("ボクのヒーロー")

    def test_accepts_hiragana_only(self):
        assert is_valid_japanese("おはよう")

    def test_accepts_dialogue_with_punctuation(self):
        assert is_valid_japanese("どうして？")

    def test_accepts_ellipsis_heavy_text(self):
        """Manga often has ellipsis-heavy dialogue like '…いや…'."""
        assert is_valid_japanese("．．．いや．．．")

    def test_accepts_long_kanji_with_hiragana(self):
        assert is_valid_japanese("先生の言葉を聞いてください")


class TestRejectNoise:
    """Test is_valid_japanese() correctly rejects OCR noise."""

    def test_rejects_empty(self):
        assert not is_valid_japanese("")

    def test_rejects_whitespace(self):
        assert not is_valid_japanese("   ")

    def test_rejects_single_char(self):
        assert not is_valid_japanese("え")

    def test_rejects_pure_punctuation(self):
        assert not is_valid_japanese("！？")

    def test_rejects_latin_only(self):
        assert not is_valid_japanese("Hello World")

    def test_accepts_short_kanji_dialogue(self):
        """Short kanji phrases are valid manga dialogue (e.g. 一人？)."""
        assert is_valid_japanese("一人？")
        assert is_valid_japanese("繊維")
        assert is_valid_japanese("経済産業大臣")

    def test_rejects_mostly_non_japanese(self):
        """Text that's mostly ASCII/non-Japanese."""
        assert not is_valid_japanese("abcdeあ")
