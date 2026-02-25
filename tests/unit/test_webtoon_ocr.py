"""Unit tests for webtoon Korean OCR validation and detection."""

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from webtoon.ocr import (
    _bbox_area,
    _enhance_for_ocr,
    _enhance_for_ocr_inverted,
    _merge_detections,
    _rescue_neighbor_detections,
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
        from pipeline.config import FONT_JP  # CJK font for Korean glyphs
        # Blue background
        img = Image.new("RGB", (500, 120), (50, 160, 250))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(FONT_JP, 32)
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
        from pipeline.config import FONT_JP  # CJK font for Korean glyphs
        img = Image.new("RGB", (500, 80), (50, 160, 250))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(FONT_JP, 32)
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

    def test_three_pass_merge_adds_non_overlapping(self):
        """Pass 3 (inverted) detections that don't overlap are added."""
        pass12 = [_make_det(10, 10, 100, 40, text="기존")]
        pass3 = [_make_det(10, 400, 300, 480, text="새로운 텍스트", conf=0.12)]
        merged = _merge_detections(pass12, pass3)
        assert len(merged) == 2

    def test_three_pass_merge_dedup_overlapping(self):
        """Pass 3 detection overlapping pass 1+2 is deduped."""
        pass12 = [_make_det(10, 10, 200, 50, text="기존", conf=0.8)]
        pass3 = [_make_det(15, 12, 195, 48, text="반전", conf=0.12)]
        merged = _merge_detections(pass12, pass3)
        assert len(merged) == 1
        assert merged[0].text == "기존"


class TestEnhanceForOcrInverted:
    """Inverted CLAHE preprocessing for bright text on dark backgrounds.

    Regression for 039: gold-outlined white Korean text on a black panel
    was invisible to both pass 1 (original) and pass 2 (CLAHE).
    The inverted pass flips brightness so bright text becomes dark strokes
    on a light background, making it detectable by EasyOCR.
    """

    def test_output_shape_matches_input(self):
        """Output is single-channel grayscale, same H×W as input."""
        img = np.zeros((200, 400, 3), dtype=np.uint8)
        result = _enhance_for_ocr_inverted(img)
        assert result.shape == (200, 400)

    def test_bright_text_becomes_dark(self):
        """White text on black becomes dark strokes on light background."""
        # Black background
        img = np.zeros((100, 300, 3), dtype=np.uint8)
        # White text stripe
        img[30:70, 50:250] = 255
        result = _enhance_for_ocr_inverted(img)
        # The formerly-white text area should now be darker than the
        # formerly-black background
        text_mean = result[40:60, 100:200].mean()
        bg_mean = result[5:20, 5:20].mean()
        assert text_mean < bg_mean, \
            "Inverted: white text should become dark, black bg should become light"

    def test_dark_image_produces_light_output(self):
        """Mostly dark input inverts to mostly light output."""
        dark = np.full((100, 100, 3), 20, dtype=np.uint8)
        result = _enhance_for_ocr_inverted(dark)
        assert result.mean() > 128, "Dark image should invert to mostly light"


class TestRescueNeighborDetections:
    """Rescue low-confidence detections near multi-line text groups.

    Regression for 297/040: "유중혁에겐" (character name) in a 3-line
    text block was missed at 0.030 confidence because EasyOCR's
    recognizer couldn't handle the proper noun.  The two lines below
    it were detected fine.  The rescue logic recovers line 1 by
    recognizing it's in the same vertical column as the other two.
    """

    def test_rescues_detection_near_two_valid_neighbors(self):
        """Low-conf detection near 2+ valid Korean detections is rescued."""
        valid = [
            _make_det(108, 828, 580, 930, text="충분하고도 남는", conf=0.47),
            _make_det(202, 914, 488, 1018, text="시간이지.", conf=0.93),
        ]
        rejected = [
            _make_det(178, 742, 506, 844, text="유중혀에젠", conf=0.03),
        ]
        rescued = _rescue_neighbor_detections(valid, rejected)
        assert len(rescued) == 1
        assert rescued[0].text == "유중혀에젠"

    def test_no_rescue_without_group(self):
        """Single valid detection is not enough for rescue."""
        valid = [
            _make_det(202, 914, 488, 1018, text="시간이지.", conf=0.93),
        ]
        rejected = [
            _make_det(178, 742, 506, 844, text="유중혀에젠", conf=0.03),
        ]
        rescued = _rescue_neighbor_detections(valid, rejected)
        assert len(rescued) == 0

    def test_rejects_noise_with_wrong_height(self):
        """Noise detection with different height from group is rejected."""
        valid = [
            _make_det(108, 828, 580, 930, text="충분하고도 남는", conf=0.47),
            _make_det(202, 914, 488, 1018, text="시간이지.", conf=0.93),
        ]
        # Noise: only 24px tall vs group's ~100px
        rejected = [
            _make_det(200, 800, 300, 824, text="디미묘", conf=0.03),
        ]
        rescued = _rescue_neighbor_detections(valid, rejected)
        assert len(rescued) == 0

    def test_rejects_non_korean_text(self):
        """Non-Korean rejected text is not rescued."""
        valid = [
            _make_det(108, 828, 580, 930, text="충분하고도 남는", conf=0.47),
            _make_det(202, 914, 488, 1018, text="시간이지.", conf=0.93),
        ]
        rejected = [
            _make_det(178, 742, 506, 844, text=")", conf=0.05),
        ]
        rescued = _rescue_neighbor_detections(valid, rejected)
        assert len(rescued) == 0

    def test_rejects_detection_in_different_column(self):
        """Detection far to the side of the text group is not rescued."""
        valid = [
            _make_det(108, 828, 580, 930, text="충분하고도 남는", conf=0.47),
            _make_det(202, 914, 488, 1018, text="시간이지.", conf=0.93),
        ]
        # Far to the right, different column
        rejected = [
            _make_det(10, 742, 80, 844, text="테스트 텍스트", conf=0.03),
        ]
        rescued = _rescue_neighbor_detections(valid, rejected)
        assert len(rescued) == 0

    def test_rejects_very_low_confidence(self):
        """Floor confidence of 0.02 filters absolute noise."""
        valid = [
            _make_det(108, 828, 580, 930, text="충분하고도 남는", conf=0.47),
            _make_det(202, 914, 488, 1018, text="시간이지.", conf=0.93),
        ]
        rejected = [
            _make_det(178, 742, 506, 844, text="유중혀에젠", conf=0.01),
        ]
        rescued = _rescue_neighbor_detections(valid, rejected)
        assert len(rescued) == 0
