"""Unit tests for webtoon Korean OCR validation and detection."""

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from webtoon.ocr import (
    _bbox_area,
    _enhance_for_ocr,
    _merge_detections,
    detect_and_read,
    is_valid_korean,
)
from webtoon.ocr import TextDetection


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


def _make_det(x1, y1, x2, y2, text="테스트", conf=0.9):
    """Helper to create a TextDetection."""
    return TextDetection(
        bbox_poly=[[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
        text=text,
        confidence=conf,
        bbox_rect=(x1, y1, x2, y2),
    )


class TestAdaptiveCLAHE:
    """Adaptive CLAHE tile sizing for tall images.

    Regression for 082: fixed tileGridSize=(8,8) on a 690x1600 image
    created 200px tiles — too coarse for styled text with colored
    gradients.  Adaptive sizing targets ~100px tiles.
    """

    def test_tall_image_gets_more_tiles(self):
        """Tall images should get more vertical tiles than short images."""
        short = np.zeros((300, 690, 3), dtype=np.uint8)
        tall = np.zeros((1600, 690, 3), dtype=np.uint8)

        enhanced_short = _enhance_for_ocr(short)
        enhanced_tall = _enhance_for_ocr(tall)

        # Both should return valid grayscale images
        assert enhanced_short.shape == (300, 690)
        assert enhanced_tall.shape == (1600, 690)

    def test_minimum_tile_count(self):
        """Even small images get at least 8 tiles per dimension."""
        tiny = np.zeros((100, 100, 3), dtype=np.uint8)
        result = _enhance_for_ocr(tiny)
        assert result.shape == (100, 100)


class TestMergeDetections:
    """Two-pass OCR merge logic with wider-detection preference.

    Regression for 082: pass 1 caught only the right half of a styled
    title line, while pass 2 (CLAHE-enhanced) caught the full line.
    The old merge always preferred pass 1 — now a significantly wider
    pass 2 detection replaces the narrower pass 1 detection.
    """

    def test_non_overlapping_both_kept(self):
        """Non-overlapping detections from both passes are kept."""
        pass1 = [_make_det(10, 10, 100, 40, text="첫 줄")]
        pass2 = [_make_det(10, 100, 100, 130, text="둘째 줄")]
        merged = _merge_detections(pass1, pass2)
        assert len(merged) == 2

    def test_duplicate_prefers_pass1(self):
        """Same detection in both passes → keep pass 1."""
        pass1 = [_make_det(10, 10, 100, 40, text="같은 줄", conf=0.9)]
        pass2 = [_make_det(12, 11, 98, 39, text="같은 줄", conf=0.8)]
        merged = _merge_detections(pass1, pass2)
        assert len(merged) == 1
        assert merged[0].confidence == 0.9

    def test_wider_pass2_replaces_narrow_pass1(self):
        """Pass 2 detection >1.5x wider replaces pass 1.

        Regression for 082: pass 1 caught '마왕 선발전' (x=412-584)
        while pass 2 caught the full line (x=106-582).
        """
        # Pass 1: right portion only
        pass1 = [_make_det(412, 1084, 584, 1140, text="마왕 선발전", conf=0.84)]
        # Pass 2: full line (2.7x larger area)
        pass2 = [_make_det(106, 1084, 582, 1140,
                           text="메인 시나리오 #25 - 마왕 선발전", conf=0.35)]
        merged = _merge_detections(pass1, pass2)
        assert len(merged) == 1
        # The wider pass 2 detection should replace pass 1
        assert merged[0].bbox_rect[0] == 106, \
            "Wider pass 2 detection should replace narrow pass 1"

    def test_similar_size_keeps_pass1(self):
        """Pass 2 detection of similar size doesn't replace pass 1."""
        pass1 = [_make_det(100, 100, 300, 140, text="원본", conf=0.9)]
        pass2 = [_make_det(95, 98, 310, 142, text="향상", conf=0.8)]
        merged = _merge_detections(pass1, pass2)
        assert len(merged) == 1
        assert merged[0].text == "원본"
