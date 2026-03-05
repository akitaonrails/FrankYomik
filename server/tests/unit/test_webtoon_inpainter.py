"""Unit tests for webtoon inpainter mask building and integration.

Tests mask-building logic without GPU. Backend inference is mocked.
"""

from unittest.mock import MagicMock, patch

import numpy as np
from PIL import Image

from webtoon.bubble_detector import WebtoonBubble
from webtoon.inpainter import build_inpaint_mask, inpaint_bubble
from webtoon.ocr import TextDetection


def _make_det(x1, y1, x2, y2, text="테스트", conf=0.9):
    """Helper to create a TextDetection."""
    return TextDetection(
        bbox_poly=[[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
        text=text,
        confidence=conf,
        bbox_rect=(x1, y1, x2, y2),
    )


def _make_bubble(detections, bbox=None, bg_color=(255, 255, 255),
                 mask=None, has_boundary=False):
    """Helper to create a WebtoonBubble."""
    if bbox is None:
        x1 = min(d.bbox_rect[0] for d in detections)
        y1 = min(d.bbox_rect[1] for d in detections)
        x2 = max(d.bbox_rect[2] for d in detections)
        y2 = max(d.bbox_rect[3] for d in detections)
        bbox = (x1, y1, x2, y2)
    return WebtoonBubble(
        bbox=bbox,
        text_regions=detections,
        combined_text=" ".join(d.text for d in detections),
        has_bubble_boundary=has_boundary,
        bg_color=bg_color,
        bubble_mask=mask,
    )


def _make_circular_mask(w, h, cx, cy, r):
    """Create a circular mask of given size."""
    mask = np.zeros((h, w), dtype=np.uint8)
    Y, X = np.ogrid[:h, :w]
    dist = (X - cx) ** 2 + (Y - cy) ** 2
    mask[dist <= r ** 2] = 255
    return mask


# --- TestBuildInpaintMask ---


class TestBuildInpaintMask:
    """Tests for build_inpaint_mask()."""

    def test_uses_text_rects_without_bubble_mask(self):
        """No bubble_mask → uses text rects directly for inpainting."""
        det = _make_det(50, 50, 150, 80)
        bubble = _make_bubble([det], mask=None)
        result = build_inpaint_mask(bubble, (200, 200), text_pad=5,
                                     text_dilate=0)
        assert result is not None
        result_arr = np.array(result)
        # Text rect area (padded by 5) should be white
        assert np.any(result_arr[45:85, 45:155] > 0)
        # Far corners should be black (no bubble mask to extend into)
        assert result_arr[0, 0] == 0

    def test_returns_none_without_text_regions(self):
        """Empty text_regions → returns None."""
        mask = np.ones((200, 200), dtype=np.uint8) * 255
        bubble = _make_bubble([], bbox=(20, 20, 180, 180), mask=mask)
        result = build_inpaint_mask(bubble, (200, 200))
        assert result is None

    def test_mask_intersects_bubble_and_text(self):
        """Result mask is intersection of bubble mask and text area."""
        # 200x200 image, bubble circle centered at (100, 100), radius 60
        mask = _make_circular_mask(200, 200, 100, 100, 60)
        det = _make_det(80, 90, 120, 110)  # text inside the circle
        bubble = _make_bubble([det], bbox=(40, 40, 160, 160),
                              mask=mask, has_boundary=True)

        result = build_inpaint_mask(bubble, (200, 200), erode_px=0,
                                    text_pad=5, text_dilate=0)
        assert result is not None
        result_arr = np.array(result)

        # Mask should have white pixels only where both bubble mask and
        # text area overlap
        assert np.any(result_arr > 0), "Mask should have white pixels"

        # Nothing outside the original bubble mask
        assert np.all(result_arr[mask == 0] == 0), \
            "Mask should not extend outside bubble"

    def test_erode_shrinks_mask(self):
        """Erosion shrinks the bubble mask via MinFilter.

        Verifies that the eroded mask step in build_inpaint_mask
        actually reduces pixel count when text fills most of the bubble.
        Uses high coverage (>85% for both) by keeping text small and
        centered well inside the bubble.
        """
        # 300x300 circular mask, large radius — text+pad stays inside
        mask = _make_circular_mask(300, 300, 150, 150, 140)
        det = _make_det(50, 50, 250, 250)
        bubble = _make_bubble([det], bbox=(10, 10, 290, 290),
                              mask=mask, has_boundary=True)

        result_no_erode = build_inpaint_mask(
            bubble, (300, 300), erode_px=0, text_pad=10, text_dilate=0)
        result_eroded = build_inpaint_mask(
            bubble, (300, 300), erode_px=5, text_pad=10, text_dilate=0)

        assert result_no_erode is not None
        assert result_eroded is not None

        count_no_erode = np.count_nonzero(np.array(result_no_erode))
        count_eroded = np.count_nonzero(np.array(result_eroded))
        assert count_eroded < count_no_erode, \
            "Erosion should reduce mask area"

    def test_text_padding_expands_mask(self):
        """text_pad expands the area around text detections."""
        mask = np.ones((200, 200), dtype=np.uint8) * 255
        det = _make_det(80, 80, 120, 120)
        bubble = _make_bubble([det], bbox=(0, 0, 200, 200),
                              mask=mask, has_boundary=True)

        result_small = build_inpaint_mask(
            bubble, (200, 200), erode_px=0, text_pad=2, text_dilate=0)
        result_large = build_inpaint_mask(
            bubble, (200, 200), erode_px=0, text_pad=20, text_dilate=0)

        assert result_small is not None
        assert result_large is not None

        count_small = np.count_nonzero(np.array(result_small))
        count_large = np.count_nonzero(np.array(result_large))
        assert count_large > count_small, \
            "Larger text_pad should produce larger mask"

    def test_text_dilate_catches_antialiasing(self):
        """text_dilate expands the final mask to catch glyph edges."""
        mask = np.ones((200, 200), dtype=np.uint8) * 255
        det = _make_det(80, 80, 120, 120)
        bubble = _make_bubble([det], bbox=(0, 0, 200, 200),
                              mask=mask, has_boundary=True)

        result_no_dilate = build_inpaint_mask(
            bubble, (200, 200), erode_px=0, text_pad=5, text_dilate=0)
        result_dilated = build_inpaint_mask(
            bubble, (200, 200), erode_px=0, text_pad=5, text_dilate=3)

        assert result_no_dilate is not None
        assert result_dilated is not None

        count_no_dilate = np.count_nonzero(np.array(result_no_dilate))
        count_dilated = np.count_nonzero(np.array(result_dilated))
        assert count_dilated > count_no_dilate, \
            "Dilation should expand mask area"

    def test_multiple_detections_union(self):
        """Multiple text detections create a union in the text mask."""
        mask = np.ones((300, 300), dtype=np.uint8) * 255
        det1 = _make_det(50, 50, 100, 70)
        det2 = _make_det(50, 120, 100, 140)  # second line of text
        bubble = _make_bubble([det1, det2], bbox=(20, 20, 280, 280),
                              mask=mask, has_boundary=True)

        result = build_inpaint_mask(bubble, (300, 300), erode_px=0,
                                    text_pad=5, text_dilate=0)
        assert result is not None
        result_arr = np.array(result)

        # Both text areas should have white pixels
        assert np.any(result_arr[45:75, 45:105] > 0), \
            "First text area should be in mask"
        assert np.any(result_arr[115:145, 45:105] > 0), \
            "Second text area should be in mask"

    def test_text_outside_bubble_uses_text_rects(self):
        """Text outside bubble mask triggers text-rects-only mode."""
        # Small circular mask
        mask = _make_circular_mask(200, 200, 100, 100, 30)
        # Text far outside the circle — coverage < 85%
        det = _make_det(5, 5, 25, 15)
        bubble = _make_bubble([det], bbox=(70, 70, 130, 130),
                              mask=mask, has_boundary=True)

        result = build_inpaint_mask(bubble, (200, 200), erode_px=0,
                                    text_pad=2, text_dilate=0)
        # Low coverage → uses text rects directly (still returns a mask)
        assert result is not None
        result_arr = np.array(result)
        # Text area should be white
        assert np.any(result_arr[3:17, 3:27] > 0)


# --- TestInpaintBubbleIntegration ---


class TestInpaintBubbleIntegration:
    """Integration tests with mocked backend (no GPU needed)."""

    def test_returns_false_when_disabled(self):
        """When INPAINT_ENABLED is False, returns False."""
        img = Image.new("RGB", (200, 200), (255, 255, 255))
        mask = np.ones((200, 200), dtype=np.uint8) * 255
        det = _make_det(50, 50, 150, 80)
        bubble = _make_bubble([det], mask=mask, has_boundary=True)

        with patch("webtoon.inpainter.INPAINT_ENABLED", False):
            result = inpaint_bubble(img, bubble)
        assert result is False

    def test_inpaints_without_bubble_mask(self):
        """Bubble without bubble_mask still inpaints using text rects."""
        img = Image.new("RGB", (200, 200), (255, 255, 255))
        target = img.copy()
        det = _make_det(50, 50, 150, 80)
        bubble = _make_bubble([det], mask=None)

        mock_backend = MagicMock()
        mock_backend.inpaint.return_value = Image.new("RGB", (200, 200),
                                                       (255, 0, 0))

        with patch("webtoon.inpainter.INPAINT_ENABLED", True):
            result = inpaint_bubble(img, bubble, backend=mock_backend,
                                    target_img=target)
        assert result is True

    def test_inpaint_modifies_only_masked_pixels(self):
        """Inpainting result should only modify pixels where mask is white."""
        # Create image with known pattern
        img = Image.new("RGB", (200, 200), (100, 150, 200))
        target = img.copy()
        mask = _make_circular_mask(200, 200, 100, 100, 50)
        det = _make_det(80, 80, 120, 120)
        bubble = _make_bubble([det], bbox=(50, 50, 150, 150),
                              mask=mask, has_boundary=True)

        # Mock backend that returns solid red
        mock_backend = MagicMock()
        red_img = Image.new("RGB", (200, 200), (255, 0, 0))
        mock_backend.inpaint.return_value = red_img

        with patch("webtoon.inpainter.INPAINT_ENABLED", True):
            result = inpaint_bubble(img, bubble, backend=mock_backend,
                                    target_img=target)

        assert result is True
        target_arr = np.array(target)
        orig_arr = np.array(img)

        # Pixels outside the mask should be unchanged
        # (check a corner that's definitely outside the circle)
        assert np.array_equal(target_arr[0, 0], orig_arr[0, 0]), \
            "Corner pixel should be unchanged"

    def test_accumulates_across_bubbles(self):
        """Multiple inpaint_bubble calls accumulate on the same target."""
        img = Image.new("RGB", (300, 300), (100, 100, 100))
        target = img.copy()

        # Two separate bubbles at different locations
        mask1 = np.zeros((300, 300), dtype=np.uint8)
        mask1[40:80, 40:80] = 255
        det1 = _make_det(45, 45, 75, 75)
        bubble1 = _make_bubble([det1], bbox=(30, 30, 90, 90),
                               mask=mask1, has_boundary=True)

        mask2 = np.zeros((300, 300), dtype=np.uint8)
        mask2[180:220, 180:220] = 255
        det2 = _make_det(185, 185, 215, 215)
        bubble2 = _make_bubble([det2], bbox=(170, 170, 230, 230),
                               mask=mask2, has_boundary=True)

        mock_backend = MagicMock()
        mock_backend.inpaint.return_value = Image.new("RGB", (300, 300),
                                                       (255, 0, 0))

        with patch("webtoon.inpainter.INPAINT_ENABLED", True):
            inpaint_bubble(img, bubble1, backend=mock_backend,
                           target_img=target)
            inpaint_bubble(img, bubble2, backend=mock_backend,
                           target_img=target)

        target_arr = np.array(target)
        # Both regions should have red pixels (from mock backend)
        assert target_arr[60, 60, 0] == 255, "First bubble region should be red"
        assert target_arr[200, 200, 0] == 255, "Second bubble region should be red"

    def test_handles_edge_bubble_bbox(self):
        """Bubble at image edge should not crash (clamp to bounds)."""
        img = Image.new("RGB", (100, 100), (200, 200, 200))
        target = img.copy()
        mask = np.ones((100, 100), dtype=np.uint8) * 255
        # Bubble touching right and bottom edges
        det = _make_det(70, 70, 98, 98)
        bubble = _make_bubble([det], bbox=(60, 60, 100, 100),
                              mask=mask, has_boundary=True)

        mock_backend = MagicMock()
        mock_backend.inpaint.return_value = Image.new("RGB", (100, 100),
                                                       (255, 0, 0))

        with patch("webtoon.inpainter.INPAINT_ENABLED", True):
            # Should not raise
            result = inpaint_bubble(img, bubble, backend=mock_backend,
                                    target_img=target)
        assert result is True
