"""Unit tests for webtoon Korean OCR validation and detection."""

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from webtoon.ocr import detect_and_read, is_valid_korean


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


class TestDetectAndReadColorConversion:
    """Regression: EasyOCR expects RGB but OpenCV loads BGR.

    When detect_and_read received BGR arrays, it silently missed text on
    colored panels (e.g., white text on blue notification balloons).
    The fix converts BGR→RGB before passing to EasyOCR.

    These tests use a synthetic image with white Korean text on a blue
    background to verify detection works regardless of channel order.
    """

    @staticmethod
    def _make_blue_panel_with_text() -> tuple[np.ndarray, np.ndarray]:
        """Create a blue panel with white Korean text, return (RGB, BGR)."""
        from webtoon.config import FONT_KO
        # Blue background
        img = Image.new("RGB", (500, 120), (50, 160, 250))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(FONT_KO, 32)
        except OSError:
            return None, None
        draw.text((40, 20), "당신은 제한 시간 동안", font=font, fill=(255, 255, 255))
        draw.text((40, 60), "무사히 설화 핵을 지켜냈습니다", font=font, fill=(255, 255, 255))
        rgb = np.array(img)
        bgr = rgb[:, :, ::-1].copy()  # Flip channels
        return rgb, bgr

    def test_bgr_input_detects_same_as_rgb(self):
        """BGR numpy arrays must produce detections (not silently miss text).

        Regression for 007: second line of blue balloon was missed because
        EasyOCR received BGR instead of RGB.
        """
        rgb, bgr = self._make_blue_panel_with_text()
        if rgb is None:
            return  # Font not available in CI

        dets_rgb = detect_and_read(rgb)
        dets_bgr = detect_and_read(bgr)

        # Both should find text (exact count may vary, but neither should be 0)
        assert len(dets_rgb) > 0, "RGB input should detect Korean text"
        assert len(dets_bgr) > 0, "BGR input should detect Korean text (after conversion)"

    def test_pil_input_still_works(self):
        """PIL Image input should still produce detections."""
        from webtoon.config import FONT_KO
        img = Image.new("RGB", (500, 80), (50, 160, 250))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(FONT_KO, 32)
        except OSError:
            return
        draw.text((40, 20), "안녕하세요 테스트입니다", font=font, fill=(255, 255, 255))
        dets = detect_and_read(img)
        assert len(dets) > 0, "PIL Image input should detect Korean text"
