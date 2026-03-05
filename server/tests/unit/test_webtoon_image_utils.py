"""Unit tests for webtoon image utilities (tall image splitting)."""

import numpy as np

from webtoon.image_utils import split_tall_image, stitch_detections, _iou
from webtoon.ocr import TextDetection


def _make_det(x1, y1, x2, y2, text="테스트", conf=0.9):
    return TextDetection(
        bbox_poly=[[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
        text=text,
        confidence=conf,
        bbox_rect=(x1, y1, x2, y2),
    )


class TestSplitTallImage:
    def test_short_image_no_split(self):
        img = np.zeros((500, 800, 3), dtype=np.uint8)
        strips = split_tall_image(img, max_height=2000)
        assert len(strips) == 1
        assert strips[0][1] == 0  # y_offset = 0

    def test_tall_image_splits(self):
        img = np.zeros((5000, 800, 3), dtype=np.uint8)
        strips = split_tall_image(img, max_height=2000, overlap=100)
        assert len(strips) >= 3
        # First strip starts at 0
        assert strips[0][1] == 0
        # Each strip is at most max_height tall
        for strip, y_offset in strips:
            assert strip.shape[0] <= 2000

    def test_strips_cover_entire_image(self):
        img = np.zeros((4500, 800, 3), dtype=np.uint8)
        strips = split_tall_image(img, max_height=2000, overlap=100)
        # Last strip should reach the end
        last_strip, last_offset = strips[-1]
        assert last_offset + last_strip.shape[0] == 4500

    def test_exact_multiple_height(self):
        img = np.zeros((4000, 800, 3), dtype=np.uint8)
        strips = split_tall_image(img, max_height=2000, overlap=100)
        assert len(strips) >= 2


class TestStitchDetections:
    def test_maps_coordinates(self):
        det = _make_det(10, 20, 100, 50)
        result = stitch_detections([([det], 500)])
        assert len(result) == 1
        assert result[0].bbox_rect == (10, 520, 100, 550)

    def test_deduplicates_overlapping(self):
        # Same detection from two overlapping strips
        det1 = _make_det(10, 20, 100, 50, conf=0.9)
        det2 = _make_det(10, 20, 100, 50, conf=0.8)
        result = stitch_detections([
            ([det1], 0),
            ([det2], 0),
        ])
        assert len(result) == 1
        assert result[0].confidence == 0.9  # Kept higher confidence

    def test_keeps_non_overlapping(self):
        det1 = _make_det(10, 20, 100, 50)
        det2 = _make_det(10, 300, 100, 330)
        result = stitch_detections([
            ([det1], 0),
            ([det2], 0),
        ])
        assert len(result) == 2


class TestIoU:
    def test_identical_boxes(self):
        assert _iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0

    def test_no_overlap(self):
        assert _iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0

    def test_partial_overlap(self):
        iou = _iou((0, 0, 10, 10), (5, 5, 15, 15))
        assert 0.1 < iou < 0.5
