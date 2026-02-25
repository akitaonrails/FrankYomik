"""Unit tests for webtoon Korean OCR validation."""

from webtoon.ocr import is_valid_korean


class TestIsValidKorean:
    """Test Korean text validation logic."""

    def test_accepts_hangul_dialogue(self):
        assert is_valid_korean("안녕하세요") is True

    def test_accepts_mixed_hangul_punctuation(self):
        assert is_valid_korean("뭐야...이게!") is True

    def test_accepts_hangul_with_numbers(self):
        assert is_valid_korean("3번째 시도야") is True

    def test_accepts_short_hangul(self):
        assert is_valid_korean("네가") is True

    def test_rejects_empty(self):
        assert is_valid_korean("") is False

    def test_rejects_single_char(self):
        assert is_valid_korean("가") is False

    def test_rejects_whitespace(self):
        assert is_valid_korean("   ") is False

    def test_rejects_pure_ascii(self):
        assert is_valid_korean("hello world") is False

    def test_rejects_pure_numbers(self):
        assert is_valid_korean("12345") is False

    def test_rejects_pure_punctuation(self):
        assert is_valid_korean("...!!") is False

    def test_rejects_japanese_text(self):
        # Japanese should not pass Korean validation
        assert is_valid_korean("こんにちは") is False

    def test_rejects_chinese_text(self):
        assert is_valid_korean("你好世界") is False

    def test_accepts_hangul_jamo(self):
        # Compatibility Jamo (ㅋㅋㅋ = Korean laughter)
        assert is_valid_korean("ㅋㅋㅋ") is True

    def test_accepts_hangul_with_english_minority(self):
        # Korean with short English abbreviation
        assert is_valid_korean("오늘은 OK야") is True

    def test_rejects_majority_english(self):
        # Mostly English with a tiny bit of Korean
        assert is_valid_korean("This is a test 가") is False

    def test_excludes_cjk_punctuation_from_ratio(self):
        # Punctuation-heavy Korean should still pass
        assert is_valid_korean("「네가」...") is True
