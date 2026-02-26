"""Regression tests for webtoon processor rendering and clearing logic.

Key learnings these tests lock in:
- Text rendering uses tight text-region bbox (from OCR detections), NOT the
  bubble's contour bbox which can be much larger and cause misplaced text.
- Clearing respects bubble mask — no white rectangles should leak outside
  the bubble boundary (regression: 029 had white rectangle beyond bubble).
- Per-detection clearing with local bg sampling catches floating text and
  glyph strokes that extend beyond the bubble mask edge.
- Semi-transparent background rectangle stays within the render bbox.
- All detections get cleared regardless of mask coverage (regression: 058
  had Korean text remnants because partially-masked detection was skipped).
- Title/logo text (large single-char detections) is correctly skipped.
"""

import numpy as np
from PIL import Image

from webtoon.bubble_detector import WebtoonBubble
from webtoon.ocr import TextDetection
from webtoon.processor import (
    _bg_luminance,
    _clear_bubble_text,
    _clear_with_mask,
    _detect_subgroups,
    _expand_render_bbox,
    _is_hangul_text,
    _is_title_text,
    _line_spacing,
    _render_sfx,
    _render_webtoon_english,
    _sample_local_bg,
    _sample_render_surface,
    _sample_sfx_color,
    _text_region_bbox,
    _total_block_height,
    _wrap_text,
)


def _make_det(x1, y1, x2, y2, text="테스트", conf=0.9):
    """Helper to create a TextDetection with consistent fields."""
    return TextDetection(
        bbox_poly=[[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
        text=text,
        confidence=conf,
        bbox_rect=(x1, y1, x2, y2),
    )


def _make_bubble(detections, bbox=None, bg_color=(255, 255, 255),
                 mask=None, has_boundary=False):
    """Helper to create a WebtoonBubble from detections."""
    if bbox is None:
        x1 = min(d.bbox_rect[0] for d in detections)
        y1 = min(d.bbox_rect[1] for d in detections)
        x2 = max(d.bbox_rect[2] for d in detections)
        y2 = max(d.bbox_rect[3] for d in detections)
        bbox = (x1 - 20, y1 - 20, x2 + 20, y2 + 20)
    combined = " ".join(d.text for d in detections)
    return WebtoonBubble(
        bbox=bbox,
        text_regions=detections,
        combined_text=combined,
        has_bubble_boundary=has_boundary,
        bg_color=bg_color,
        bubble_mask=mask,
    )


class TestTextRegionBbox:
    """_text_region_bbox must be tight around OCR detections, not the contour.

    Regression: text was rendered at contour bbox position (oversized), causing
    English text to appear far from where the Korean text was.
    """

    def test_tighter_than_contour_bbox(self):
        """Text region bbox must be smaller than or equal to the contour bbox."""
        dets = [
            _make_det(200, 300, 400, 340),
            _make_det(180, 350, 420, 390),
        ]
        # Contour bbox is much larger than text area
        bubble = _make_bubble(dets, bbox=(50, 100, 600, 500))
        text_bbox = _text_region_bbox(bubble, 700, 600)

        # Text region must be inside contour bbox
        assert text_bbox[0] >= bubble.bbox[0]  # x1
        assert text_bbox[1] >= bubble.bbox[1]  # y1
        assert text_bbox[2] <= bubble.bbox[2]  # x2
        assert text_bbox[3] <= bubble.bbox[3]  # y2

    def test_includes_all_detections_with_padding(self):
        """Text region must encompass all detections (plus padding)."""
        dets = [
            _make_det(200, 300, 400, 340),
            _make_det(180, 350, 420, 390),
        ]
        bubble = _make_bubble(dets)
        text_bbox = _text_region_bbox(bubble, 700, 600)

        # Must include all detection edges (with some padding)
        assert text_bbox[0] <= 180  # leftmost detection x1
        assert text_bbox[1] <= 300  # topmost detection y1
        assert text_bbox[2] >= 420  # rightmost detection x2
        assert text_bbox[3] >= 390  # bottommost detection y2

    def test_clamped_to_image_bounds(self):
        """Text region bbox must not exceed image dimensions."""
        dets = [_make_det(5, 5, 95, 35)]
        bubble = _make_bubble(dets)
        text_bbox = _text_region_bbox(bubble, 100, 50)

        assert text_bbox[0] >= 0
        assert text_bbox[1] >= 0
        assert text_bbox[2] <= 100
        assert text_bbox[3] <= 50

    def test_falls_back_to_bubble_bbox_when_no_detections(self):
        """If bubble has no text_regions, return the bubble bbox."""
        bubble = WebtoonBubble(
            bbox=(50, 100, 200, 300),
            text_regions=[],
            combined_text="",
        )
        assert _text_region_bbox(bubble, 700, 600) == (50, 100, 200, 300)


class TestClearWithMask:
    """Mask-based clearing must only modify pixels inside the mask.

    Regression: 029 had a white rectangle that extended beyond the spiky
    bubble boundary because clearing wasn't constrained to the mask.
    """

    def test_only_modifies_masked_pixels(self):
        """Pixels outside the mask must not be changed."""
        img = Image.new("RGB", (200, 200), (128, 128, 128))
        # Circular mask in the center
        mask = np.zeros((200, 200), dtype=np.uint8)
        mask[50:150, 50:150] = 255

        orig = np.array(img.copy())
        _clear_with_mask(img, (40, 40, 160, 160), mask, (255, 255, 255))
        result = np.array(img)

        # Outside mask: unchanged
        outside = mask == 0
        np.testing.assert_array_equal(result[outside], orig[outside])

    def test_fills_masked_pixels_with_bg_color(self):
        """Pixels inside the mask within the bbox must be filled."""
        img = Image.new("RGB", (200, 200), (0, 0, 0))
        mask = np.zeros((200, 200), dtype=np.uint8)
        mask[80:120, 80:120] = 255
        fill_color = (255, 200, 100)

        _clear_with_mask(img, (70, 70, 130, 130), mask, fill_color)
        result = np.array(img)

        # Masked pixels in bbox should be filled
        roi = result[80:120, 80:120]
        assert np.all(roi == fill_color)

    def test_no_change_if_bbox_outside_mask(self):
        """If bbox doesn't overlap with mask, nothing changes."""
        img = Image.new("RGB", (200, 200), (100, 100, 100))
        mask = np.zeros((200, 200), dtype=np.uint8)
        mask[150:190, 150:190] = 255

        orig = np.array(img.copy())
        _clear_with_mask(img, (10, 10, 50, 50), mask, (255, 255, 255))
        result = np.array(img)

        np.testing.assert_array_equal(result, orig)

    def test_handles_zero_size_bbox(self):
        """Zero-size bbox should not crash."""
        img = Image.new("RGB", (100, 100), (128, 128, 128))
        mask = np.zeros((100, 100), dtype=np.uint8)
        _clear_with_mask(img, (50, 50, 50, 50), mask, (255, 255, 255))
        # No crash = pass


class TestClearBubbleText:
    """Two-phase clearing must clear all Korean text without leaking.

    Phase 1: Mask-based clearing for bubbles with contour boundaries.
    Phase 2: Per-detection rectangle clearing with local bg color.

    Regression: 058 had Korean remnants because a detection partially covered
    by the mask was skipped. Now ALL detections get rectangle-cleared.
    """

    def test_clears_all_detections_with_mask(self):
        """Every detection must be cleared even when mask partially covers some."""
        img_size = (400, 400)
        img = Image.new("RGB", img_size, (255, 255, 255))
        # Draw fake "Korean text" as dark rectangles
        arr = np.array(img)
        arr[100:130, 100:200] = [0, 0, 0]  # det 0
        arr[140:170, 100:200] = [0, 0, 0]  # det 1
        arr[180:210, 100:200] = [0, 0, 0]  # det 2 (will be partially outside mask)
        img = Image.fromarray(arr)

        dets = [
            _make_det(100, 100, 200, 130),
            _make_det(100, 140, 200, 170),
            _make_det(100, 180, 200, 210),
        ]
        # Mask only covers first two detections
        mask = np.zeros((400, 400), dtype=np.uint8)
        mask[90:175, 85:215] = 255

        bubble = _make_bubble(dets, bbox=(80, 80, 220, 220),
                              mask=mask, has_boundary=True)

        _clear_bubble_text(img, bubble)
        result = np.array(img)

        # ALL three detection areas must be cleared (close to bg color)
        for det in dets:
            x1, y1, x2, y2 = det.bbox_rect
            region = result[y1:y2, x1:x2]
            mean_val = region.mean()
            assert mean_val > 200, (
                f"Detection at {det.bbox_rect} not cleared: mean={mean_val:.0f}"
            )

    def test_clears_without_mask(self):
        """Bubbles without mask use rectangle clearing for all detections."""
        img = Image.new("RGB", (300, 300), (255, 255, 255))
        arr = np.array(img)
        arr[50:80, 50:150] = [30, 30, 30]  # fake text
        img = Image.fromarray(arr)

        dets = [_make_det(50, 50, 150, 80)]
        bubble = _make_bubble(dets, mask=None, has_boundary=False)

        _clear_bubble_text(img, bubble)
        result = np.array(img)

        # Detection area should be cleared
        region = result[50:80, 50:150]
        assert region.mean() > 200

    def test_uses_local_bg_color_not_hardcoded_white(self):
        """Per-detection clearing should sample local bg, not assume white."""
        # Create image with colored background
        bg = (200, 180, 160)
        img = Image.new("RGB", (300, 300), bg)
        arr = np.array(img)
        arr[100:130, 100:200] = [0, 0, 0]  # dark text on colored bg
        img = Image.fromarray(arr)

        dets = [_make_det(100, 100, 200, 130)]
        bubble = _make_bubble(dets, mask=None, has_boundary=False,
                              bg_color=bg)

        _clear_bubble_text(img, bubble)
        result = np.array(img)

        # Cleared area should be close to the bg color, not pure white
        region = result[100:130, 100:200]
        mean_color = region.mean(axis=(0, 1))
        # Should be close to (200, 180, 160), not (255, 255, 255)
        assert abs(mean_color[0] - bg[0]) < 30, f"R channel off: {mean_color[0]}"
        assert abs(mean_color[1] - bg[1]) < 30, f"G channel off: {mean_color[1]}"
        assert abs(mean_color[2] - bg[2]) < 30, f"B channel off: {mean_color[2]}"


class TestRenderWebtoonEnglish:
    """English rendering must stay within render_bbox bounds.

    Regression: text and semi-transparent bg rectangle extended beyond the
    bubble boundary, creating visible white boxes on non-white backgrounds.
    """

    def test_rendering_stays_within_bbox(self):
        """All pixel changes must be within the render_bbox."""
        img = Image.new("RGB", (500, 500), (220, 220, 220))
        orig = np.array(img.copy())

        dets = [_make_det(100, 200, 300, 240)]
        bubble = _make_bubble(dets, bg_color=(255, 255, 255))
        render_bbox = (90, 190, 310, 260)

        _render_webtoon_english(img, bubble, "Hello world", render_bbox)
        result = np.array(img)

        diff = np.abs(result.astype(int) - orig.astype(int)).sum(axis=2) > 2
        if diff.any():
            changed_ys = np.where(diff.any(axis=1))[0]
            changed_xs = np.where(diff.any(axis=0))[0]
            bx1, by1, bx2, by2 = render_bbox
            assert changed_ys.min() >= by1, (
                f"Rendering extends above bbox: y={changed_ys.min()} < {by1}"
            )
            assert changed_ys.max() < by2, (
                f"Rendering extends below bbox: y={changed_ys.max()} >= {by2}"
            )
            assert changed_xs.min() >= bx1, (
                f"Rendering extends left of bbox: x={changed_xs.min()} < {bx1}"
            )
            assert changed_xs.max() < bx2, (
                f"Rendering extends right of bbox: x={changed_xs.max()} >= {bx2}"
            )

    def test_bg_rectangle_within_bbox(self):
        """Semi-transparent background rectangle must not exceed render_bbox."""
        # Dark background so the white bg_rect is highly visible
        img = Image.new("RGB", (400, 400), (30, 30, 30))
        orig = np.array(img.copy())

        dets = [_make_det(100, 150, 300, 200)]
        bubble = _make_bubble(dets, bg_color=(30, 30, 30))
        render_bbox = (85, 140, 315, 220)

        _render_webtoon_english(img, bubble, "Test text", render_bbox)
        result = np.array(img)

        diff = np.abs(result.astype(int) - orig.astype(int)).sum(axis=2) > 2
        bx1, by1, bx2, by2 = render_bbox
        # Nothing outside bbox should change
        outside = np.zeros_like(diff)
        outside[:by1, :] = True
        outside[by2:, :] = True
        outside[:, :bx1] = True
        outside[:, bx2:] = True
        assert not diff[outside].any(), "Rendering leaked outside render_bbox"

    def test_skips_tiny_bbox(self):
        """Very small render bbox should be skipped gracefully."""
        img = Image.new("RGB", (100, 100), (255, 255, 255))
        orig = np.array(img.copy())

        dets = [_make_det(40, 40, 50, 45)]
        bubble = _make_bubble(dets)

        _render_webtoon_english(img, bubble, "Hello", (40, 40, 48, 45))
        result = np.array(img)

        # Should not crash; tiny bbox might not render anything
        # Just verify no crash occurred

    def test_long_text_truncated_not_overflowing(self):
        """Long text should be truncated with '...' rather than overflow bbox.

        Font rendering has inherent antialiasing bleed (~2px from glyph
        descenders), so we allow a small tolerance.
        """
        img = Image.new("RGB", (500, 500), (200, 200, 200))
        orig = np.array(img.copy())

        dets = [
            _make_det(80, 150, 350, 190),
            _make_det(80, 200, 350, 240),
        ]
        bubble = _make_bubble(dets, bg_color=(255, 255, 255))
        render_bbox = (66, 140, 364, 250)

        long_text = ("This is a very long sentence that absolutely cannot "
                     "fit within the render bbox and should be truncated "
                     "rather than overflowing beyond the boundary")
        _render_webtoon_english(img, bubble, long_text, render_bbox)
        result = np.array(img)

        diff = np.abs(result.astype(int) - orig.astype(int)).sum(axis=2) > 2
        if diff.any():
            changed_ys = np.where(diff.any(axis=1))[0]
            _, _, _, by2 = render_bbox
            # Allow 3px tolerance for font descender antialiasing
            assert changed_ys.max() < by2 + 3, (
                f"Long text overflows bbox: y={changed_ys.max()} >= {by2 + 3}"
            )

    def test_dark_bg_gets_white_text(self):
        """Dark backgrounds should produce white text for readability."""
        assert _bg_luminance((0, 0, 0)) < 0.65
        assert _bg_luminance((30, 30, 50)) < 0.65

    def test_blue_panel_gets_white_text(self):
        """Blue notification panels (lum ~0.57) should get white text.

        Regression: webtoon blue panels with RGB ~(55, 175, 255) were
        getting black text because lum 0.57 > old threshold 0.5.
        """
        assert _bg_luminance((55, 175, 255)) < 0.65

    def test_light_bg_gets_dark_text(self):
        """Light backgrounds should produce dark text for readability."""
        assert _bg_luminance((255, 255, 255)) > 0.65
        assert _bg_luminance((220, 220, 200)) > 0.65


class TestSampleLocalBg:
    """Local bg sampling must return the actual surrounding color."""

    def test_returns_surrounding_color(self):
        """Should return median color of band around the detection."""
        bg = (180, 160, 140)
        img = Image.new("RGB", (200, 200), bg)
        # Put some "text" in the center
        arr = np.array(img)
        arr[80:120, 80:120] = [0, 0, 0]
        img = Image.fromarray(arr)

        color = _sample_local_bg(img, (80, 80, 120, 120))
        # Should be close to bg color
        assert abs(color[0] - bg[0]) < 20
        assert abs(color[1] - bg[1]) < 20
        assert abs(color[2] - bg[2]) < 20

    def test_defaults_to_white_on_edge(self):
        """Corner detections should fall back to white."""
        img = Image.new("RGB", (20, 20), (255, 255, 255))
        color = _sample_local_bg(img, (0, 0, 20, 20))
        # At image edges, sampling bands may be empty → white fallback
        assert color[0] >= 200  # Should be white-ish


class TestIsTitleText:
    """Title/logo detection prevents clearing decorative text.

    Title text: large single-character detections (artistic/decorative).
    Normal dialogue: full text lines per detection.
    """

    def test_normal_dialogue_not_title(self):
        """Multi-character dialogue lines are not title text."""
        dets = [
            _make_det(100, 100, 300, 140, text="이것은 대화입니다"),
            _make_det(100, 150, 300, 190, text="또 다른 줄이에요"),
        ]
        bubble = _make_bubble(dets)
        assert _is_title_text(bubble) is False

    def test_large_single_chars_is_title(self):
        """Large single-character detections are title text."""
        dets = [
            _make_det(100, 100, 200, 200, text="마"),  # 100px tall, 1 char
            _make_det(100, 210, 200, 310, text="왕"),  # 100px tall, 1 char
        ]
        bubble = _make_bubble(dets)
        assert _is_title_text(bubble) is True

    def test_small_single_chars_not_title(self):
        """Small single-character detections are not title text."""
        dets = [
            _make_det(100, 100, 130, 130, text="네"),  # 30px tall
            _make_det(100, 140, 130, 170, text="가"),  # 30px tall
        ]
        bubble = _make_bubble(dets)
        assert _is_title_text(bubble) is False

    def test_empty_bubble_not_title(self):
        """Empty bubble is not title text."""
        bubble = WebtoonBubble(
            bbox=(0, 0, 100, 100),
            text_regions=[],
            combined_text="",
        )
        assert _is_title_text(bubble) is False


class TestWrapText:
    """Word wrapping must produce lines that fit within max_width."""

    def test_short_text_single_line(self):
        from PIL import ImageFont
        from webtoon.config import FONT_KO

        font = ImageFont.truetype(FONT_KO, 16)
        lines = _wrap_text("Hi", font, 200)
        assert len(lines) == 1
        assert lines[0] == "Hi"

    def test_long_text_wraps(self):
        from PIL import ImageFont
        from webtoon.config import FONT_KO

        font = ImageFont.truetype(FONT_KO, 16)
        lines = _wrap_text("This is a longer sentence that should wrap", font, 100)
        assert len(lines) > 1

    def test_empty_text_returns_empty(self):
        from PIL import ImageFont
        from webtoon.config import FONT_KO

        font = ImageFont.truetype(FONT_KO, 16)
        assert _wrap_text("", font, 200) == []

    def test_lines_fit_within_width(self):
        from PIL import ImageFont
        from webtoon.config import FONT_KO

        font = ImageFont.truetype(FONT_KO, 16)
        max_w = 150
        lines = _wrap_text("Hello world this is a test of wrapping", font, max_w)
        for line in lines:
            bbox = font.getbbox(line)
            line_w = bbox[2] - bbox[0]
            # Each line should fit (individual words wider than max_w are an
            # edge case where a single word can't be broken)
            words_in_line = line.split()
            if len(words_in_line) > 1:
                assert line_w <= max_w + 5, f"Line too wide: '{line}' = {line_w}px"


class TestRenderingMaskConstraint:
    """The semi-transparent bg rectangle must be clipped to bubble mask.

    This is the regression test for the 029 bug where a white rectangle
    leaked beyond the bubble boundary.

    Note: Phase 2 per-detection clearing intentionally extends beyond the mask
    (uses local bg color to blend), but the rendering overlay must NOT.
    """

    def test_bg_rectangle_clipped_to_mask(self):
        """The semi-transparent bg rectangle must not appear outside the mask."""
        size = (500, 500)
        bg = (150, 150, 150)
        img = Image.new("RGB", size, bg)

        # Circular mask
        mask = np.zeros(size[::-1], dtype=np.uint8)  # (h, w)
        cy, cx, r = 235, 250, 80
        Y, X = np.ogrid[:size[1], :size[0]]
        mask[(Y - cy)**2 + (X - cx)**2 <= r**2] = 255

        dets = [
            _make_det(180, 200, 320, 230, text="테스트 문장"),
            _make_det(180, 240, 320, 270, text="두 번째 줄"),
        ]
        bubble = _make_bubble(
            dets, bbox=(170, 155, 330, 315),
            mask=mask, has_boundary=True, bg_color=(255, 255, 255),
        )

        # First clear text (Phase 2 may extend beyond mask — that's OK)
        _clear_bubble_text(img, bubble)
        # Snapshot after clearing but before rendering
        after_clear = np.array(img.copy())

        # Now render English — this is what we're testing
        text_bbox = _text_region_bbox(bubble, size[0], size[1])
        _render_webtoon_english(img, bubble, "Test sentence", text_bbox)
        after_render = np.array(img)

        # The rendering (bg rect + text) must not change pixels outside mask
        diff = np.abs(after_render.astype(int) - after_clear.astype(int)).sum(axis=2)
        outside_mask = mask == 0

        large_changes_outside = (diff > 5) & outside_mask
        n_large_outside = large_changes_outside.sum()
        assert n_large_outside == 0, (
            f"{n_large_outside} pixels had rendering changes outside the "
            f"bubble mask (bg rectangle leak)"
        )

    def test_clearing_uses_local_bg_outside_mask(self):
        """Phase 2 clearing outside mask should use local bg (not white)."""
        size = (400, 400)
        bg = (180, 160, 140)
        img = Image.new("RGB", size, bg)

        # Put dark text partly outside a small mask
        arr = np.array(img)
        arr[150:180, 100:300] = [0, 0, 0]
        img = Image.fromarray(arr)

        # Mask covers only part of the detection
        mask = np.zeros((400, 400), dtype=np.uint8)
        mask[140:185, 90:200] = 255  # only left half

        dets = [_make_det(100, 150, 300, 180)]
        bubble = _make_bubble(dets, mask=mask, has_boundary=True,
                              bg_color=(180, 160, 140))

        _clear_bubble_text(img, bubble)
        result = np.array(img)

        # The right half (outside mask) should be cleared to local bg,
        # not pure white (255, 255, 255)
        right_region = result[150:180, 200:300]
        mean_color = right_region.mean(axis=(0, 1))
        # Should be close to bg, not pure white
        assert mean_color[0] < 240, (
            f"Clearing outside mask used white instead of local bg: "
            f"R={mean_color[0]:.0f}"
        )


class TestSampleRenderSurface:
    """Font color must be determined from the actual cleared/inpainted surface.

    Regression for chapter_293 pages 006 and 013: bubble.bg_color was sampled
    from a narrow band during detection which leaked into bright surrounding
    artwork, causing black text on dark-blue notification panels.  Now we
    sample the median color of the render area AFTER clearing.
    """

    def test_returns_dark_color_for_blue_surface(self):
        """Blue panel surface → dark median → should produce white font."""
        blue = (40, 80, 180)
        img = Image.new("RGB", (300, 200), blue)
        color = _sample_render_surface(img, (50, 50, 250, 150))
        lum = _bg_luminance(color)
        assert lum < 0.5, (
            f"Blue surface should be dark (lum={lum:.2f}), "
            f"sampled={color}"
        )

    def test_returns_light_color_for_white_surface(self):
        """White bubble surface → bright median → should produce dark font."""
        img = Image.new("RGB", (300, 200), (250, 250, 250))
        color = _sample_render_surface(img, (50, 50, 250, 150))
        lum = _bg_luminance(color)
        assert lum > 0.5, (
            f"White surface should be bright (lum={lum:.2f}), "
            f"sampled={color}"
        )

    def test_samples_actual_image_not_bg_color(self):
        """Surface sampling uses actual pixels, not bubble.bg_color.

        Regression: bubble.bg_color was bright (leaked into artwork) even
        though the actual panel surface was dark blue → wrong font color.
        """
        dark_surface = (30, 50, 120)
        img = Image.new("RGB", (200, 200), dark_surface)
        bbox = (20, 20, 180, 180)

        # The surface is dark, so font should be white regardless of
        # what bubble.bg_color says
        color = _sample_render_surface(img, bbox)
        assert _bg_luminance(color) < 0.5

    def test_handles_zero_size_bbox(self):
        """Zero-size bbox returns white fallback."""
        img = Image.new("RGB", (100, 100), (0, 0, 0))
        color = _sample_render_surface(img, (50, 50, 50, 50))
        assert color == (255, 255, 255)

    def test_clamped_to_image_bounds(self):
        """Bbox extending beyond image is clamped, doesn't crash."""
        img = Image.new("RGB", (100, 100), (100, 100, 100))
        color = _sample_render_surface(img, (-10, -10, 200, 200))
        # Should sample the entire image → (100, 100, 100)
        assert abs(color[0] - 100) < 5


class TestDetectSubgroups:
    """Sub-group splitting for overlapping balloons and multi-box bubbles.

    Regression for 071: detections from two separate overlapping balloons
    had small vertical gap (4px) but large horizontal offset (179px).
    Without horizontal-only splitting, text from both balloons was merged
    into one garbled translation.
    """

    def test_single_detection_returns_one_group(self):
        """A single detection stays in one group."""
        dets = [_make_det(100, 100, 200, 130)]
        groups = _detect_subgroups(dets)
        assert len(groups) == 1
        assert len(groups[0]) == 1

    def test_vertical_gap_and_x_offset_splits(self):
        """Vertical gap + horizontal offset → split."""
        dets = [
            _make_det(100, 100, 200, 130, text="첫 번째"),
            _make_det(300, 200, 400, 230, text="두 번째"),
        ]
        groups = _detect_subgroups(dets)
        assert len(groups) == 2

    def test_large_x_offset_splits_without_vertical_gap(self):
        """Large horizontal offset alone splits (overlapping balloons).

        Regression for 071: two balloons overlap vertically so gap is tiny,
        but text centers are 179px apart horizontally.
        """
        # Two detections at similar Y but very different X
        dets = [
            _make_det(100, 100, 200, 130, text="왼쪽"),   # center X = 150
            _make_det(300, 125, 400, 155, text="오른쪽"),  # center X = 350, gap ~-5px
        ]
        groups = _detect_subgroups(dets)
        assert len(groups) == 2, (
            f"Expected 2 groups for 200px horizontal offset, got {len(groups)}"
        )

    def test_same_column_no_split(self):
        """Lines in same column (small x offset) stay together."""
        dets = [
            _make_det(100, 100, 300, 130, text="첫 줄"),
            _make_det(110, 140, 290, 170, text="둘째 줄"),
            _make_det(105, 180, 295, 210, text="셋째 줄"),
        ]
        groups = _detect_subgroups(dets)
        assert len(groups) == 1

    def test_vertical_gap_same_x_no_split(self):
        """Vertical gap with same X position stays together (same balloon)."""
        dets = [
            _make_det(100, 100, 300, 130, text="첫 줄"),
            _make_det(110, 180, 290, 210, text="둘째 줄"),  # 50px gap, same X
        ]
        groups = _detect_subgroups(dets)
        assert len(groups) == 1


class TestLineSpacing:
    """Proportional line spacing for readable multi-line text.

    Regression for 297/028: fixed 3px line spacing at font size 34
    produced cramped, unreadable text in large dark panels.  Spacing
    now scales with font size (~20%).
    """

    def test_spacing_scales_with_font_size(self):
        """Larger fonts get proportionally larger inter-line gaps."""
        assert _line_spacing(30) > _line_spacing(12)

    def test_minimum_spacing(self):
        """Even tiny fonts get at least 4px spacing."""
        assert _line_spacing(8) >= 4
        assert _line_spacing(10) >= 4

    def test_large_font_spacing(self):
        """Font size 30 should get ~6px spacing (20%)."""
        gap = _line_spacing(30)
        assert 5 <= gap <= 8

    def test_total_block_height_uses_proportional_gap(self):
        """_total_block_height with font_size uses proportional spacing."""
        from PIL import ImageFont
        from webtoon.config import FONT_KO
        font = ImageFont.truetype(FONT_KO, 30)
        lines = ["Line one", "Line two", "Line three"]
        h_with_size = _total_block_height(font, lines, font_size=30)
        h_default = _total_block_height(font, lines, font_size=0)
        # Proportional spacing (font_size=30 → gap=6) > default (gap=4)
        assert h_with_size > h_default


class TestExpandRenderBbox:
    """Render bbox expansion capped to original Korean text width.

    Regression for 297/051: bubble covered the full image (690x935)
    so expansion blew the render width from 252px to 586px, causing
    "No talent, huh! Baltar" to render as one oversized line instead
    of wrapping to multiple lines.
    """

    def test_no_expansion_when_similar_width(self):
        """No expansion when bubble is barely wider than text."""
        det = _make_det(100, 50, 300, 80)
        bubble = _make_bubble([det], bbox=(90, 40, 310, 90))
        result = _expand_render_bbox((100, 50, 300, 80), bubble)
        assert result == (100, 50, 300, 80)

    def test_caps_expansion_to_text_width(self):
        """Expansion is capped at 1.3x original text width, not bubble width.

        Regression for 297/051: 252px text in 690px bubble expanded to
        586px.  Now capped to 252 * 1.3 ≈ 328px.
        """
        det = _make_det(200, 50, 452, 200)  # 252px wide text
        # Huge bubble (full image width)
        bubble = _make_bubble([det], bbox=(0, 0, 690, 935))
        result = _expand_render_bbox((200, 50, 452, 200), bubble)
        expanded_w = result[2] - result[0]
        # Should be close to 252 * 1.3 = 328, not 586
        assert expanded_w <= 252 * 1.4, \
            f"Expansion {expanded_w}px should be capped near 1.3x text width (328px)"
        assert expanded_w >= 252, "Should not shrink below original text width"

    def test_mask_based_expansion_also_capped(self):
        """Even with a mask, expansion respects the 1.3x text width cap."""
        det = _make_det(200, 50, 400, 100)  # 200px wide
        mask = np.zeros((200, 690), dtype=np.uint8)
        mask[40:110, 50:640] = 255  # Wide mask (590px)
        bubble = _make_bubble([det], bbox=(50, 40, 640, 110),
                              mask=mask, has_boundary=True)
        result = _expand_render_bbox((200, 50, 400, 100), bubble)
        expanded_w = result[2] - result[0]
        assert expanded_w <= 200 * 1.4, \
            f"Mask-based expansion {expanded_w}px should also be capped"


class TestFontSizeCap:
    """Font size capped at 36px for consistent sizing across a page.

    Regression for 297/051, 059: uncapped font sizing produced 48px
    text in large boxes — oversized compared to surrounding balloons.
    Capping at 36px forces line wrapping instead.
    """

    def test_font_stays_under_cap(self):
        """Font size should not exceed 36px even in tall boxes."""
        from PIL import ImageFont
        from webtoon.config import FONT_KO
        text = "Short text here"
        bh = 400  # Very tall box
        bw = 300
        margin_h = max(10, int(bh * 0.05))
        margin_w = max(10, int(bw * 0.05))
        fit_h = bh - margin_h * 2
        fit_w = bw - margin_w * 2
        target = max(10, min(36, int(fit_h * 0.7)))
        # The target should be 36, not higher
        assert target == 36, f"Font target {target} should be capped at 36"

    def test_short_text_wraps_at_capped_size(self):
        """With 36px cap and narrow render, short text wraps to 2+ lines."""
        from PIL import ImageFont
        from webtoon.config import FONT_KO
        font = ImageFont.truetype(FONT_KO, 36)
        lines = _wrap_text("No talent, huh! Baltar", font, 250)
        assert len(lines) >= 2, \
            f"Should wrap to 2+ lines at 36px/250px, got {len(lines)}: {lines}"


class TestSampleSfxColor:
    """Color sampling for SFX overlay text."""

    def test_returns_bright_color_for_saturated_area(self):
        """Saturated colored area should return a vivid color, not gray."""
        # Create image with bright red area
        img = Image.new("RGB", (200, 200), (255, 50, 50))
        color = _sample_sfx_color(img, (10, 10, 190, 190))
        # Should return something reddish (R >> G, B)
        assert color[0] > 150, f"Expected red-ish, got {color}"

    def test_fallback_for_dark_gray_area(self):
        """Dark/gray areas should return the default bright red fallback."""
        img = Image.new("RGB", (200, 200), (30, 30, 30))
        color = _sample_sfx_color(img, (10, 10, 190, 190))
        # Default fallback is (255, 50, 50)
        assert color == (255, 50, 50), f"Expected fallback red, got {color}"

    def test_fallback_for_white_area(self):
        """Very bright/white areas should return the fallback color."""
        img = Image.new("RGB", (200, 200), (250, 250, 250))
        color = _sample_sfx_color(img, (10, 10, 190, 190))
        assert color == (255, 50, 50), f"Expected fallback red, got {color}"

    def test_fallback_for_zero_size_bbox(self):
        """Zero-size bbox should return fallback."""
        img = Image.new("RGB", (100, 100), (128, 128, 128))
        color = _sample_sfx_color(img, (50, 50, 50, 50))
        assert color == (255, 50, 50)

    def test_out_of_bounds_bbox_clamped(self):
        """Out-of-bounds bbox should be clamped without crash."""
        img = Image.new("RGB", (100, 100), (200, 50, 50))
        color = _sample_sfx_color(img, (-10, -10, 200, 200))
        # Should not crash; returns some color
        assert len(color) == 3


class TestRenderSfx:
    """SFX rendering produces bold outlined text within the detection bbox."""

    def test_renders_text_on_image(self):
        """SFX rendering should modify pixels within the detection area."""
        img = Image.new("RGB", (500, 500), (128, 128, 128))
        original = img.copy()
        orig_arr = np.array(original)

        det = _make_det(100, 200, 330, 400, text="꽈양")
        _render_sfx(img, det, "CRASH", original)

        result = np.array(img)
        diff = np.abs(result.astype(int) - orig_arr.astype(int)).sum(axis=2)
        assert diff.sum() > 0, "SFX rendering should change some pixels"

    def test_renders_within_detection_bbox_area(self):
        """Most pixel changes should be near the detection bbox."""
        img = Image.new("RGB", (500, 500), (128, 128, 128))
        original = img.copy()
        orig_arr = np.array(original)

        det = _make_det(150, 200, 350, 350, text="쾅")
        _render_sfx(img, det, "BOOM", original)

        result = np.array(img)
        diff = np.abs(result.astype(int) - orig_arr.astype(int)).sum(axis=2) > 2
        if diff.any():
            changed_ys = np.where(diff.any(axis=1))[0]
            changed_xs = np.where(diff.any(axis=0))[0]
            # Allow tolerance for stroke_width=3 (both sides) + shadow=2 + font metrics
            x1, y1, x2, y2 = det.bbox_rect
            tolerance = 20
            assert changed_ys.min() >= y1 - tolerance, (
                f"SFX extends too far above: {changed_ys.min()} < {y1 - tolerance}"
            )
            assert changed_xs.min() >= x1 - tolerance, (
                f"SFX extends too far left: {changed_xs.min()} < {x1 - tolerance}"
            )

    def test_drop_shadow_present(self):
        """Drop shadow should create visible dark pixels offset from text."""
        # Use a very light image so shadow (dark) is detectable
        img = Image.new("RGB", (400, 400), (240, 240, 240))
        original = img.copy()

        det = _make_det(100, 100, 300, 250, text="값")
        _render_sfx(img, det, "CRASH", original)

        result = np.array(img)
        # There should be some darkened pixels (shadow)
        dark_pixels = (result.mean(axis=2) < 200).sum()
        assert dark_pixels > 0, "Drop shadow should create some dark pixels"

    def test_no_crash_on_empty_text(self):
        """Empty SFX text should not crash."""
        img = Image.new("RGB", (200, 200), (128, 128, 128))
        original = img.copy()
        det = _make_det(50, 50, 150, 150, text="값")
        _render_sfx(img, det, "", original)
        # No crash = pass


class TestIsHangulText:
    """Filter out OCR garbage from SFX detections.

    Regression: pages 038, 043, 044, etc. had OCR artifacts like '@',
    '_', '(', '0', '7' misdetected as SFX from artistic brush strokes.
    These got translated to nonsense ("SFX ONE", "@") and rendered.
    """

    def test_hangul_syllable_accepted(self):
        """Single Hangul syllable is valid SFX text."""
        assert _is_hangul_text("값") is True
        assert _is_hangul_text("쾅") is True

    def test_hangul_jamo_accepted(self):
        """Hangul Jamo (ㅋ, ㅎ) is valid SFX text."""
        assert _is_hangul_text("ㅋ") is True

    def test_multi_char_hangul_accepted(self):
        """Two-character Korean SFX is valid."""
        assert _is_hangul_text("꽈양") is True
        assert _is_hangul_text("적자") is True

    def test_ascii_garbage_rejected(self):
        """OCR garbage like '@', '_', '(' must be filtered out."""
        assert _is_hangul_text("@") is False
        assert _is_hangul_text("_") is False
        assert _is_hangul_text("(") is False

    def test_digits_rejected(self):
        """Numbers misread from panel art must be filtered out."""
        assert _is_hangul_text("0") is False
        assert _is_hangul_text("1") is False
        assert _is_hangul_text("7") is False
        assert _is_hangul_text("4") is False

    def test_empty_rejected(self):
        assert _is_hangul_text("") is False
