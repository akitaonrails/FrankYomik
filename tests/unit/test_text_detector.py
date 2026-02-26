"""Unit tests for text detector overlap logic (no EasyOCR dependency)."""

from pipeline.text_detector import TextRegion, find_unbubbled_text, _iou


class TestIoU:
    def test_identical_boxes(self):
        assert _iou((0, 0, 100, 100), (0, 0, 100, 100)) == 1.0

    def test_no_overlap(self):
        assert _iou((0, 0, 50, 50), (100, 100, 200, 200)) == 0.0

    def test_partial_overlap(self):
        # 50x50 box at origin, 50x50 box offset by 25px
        # Intersection: 25x25 = 625
        # Union: 2500 + 2500 - 625 = 4375
        result = _iou((0, 0, 50, 50), (25, 25, 75, 75))
        assert abs(result - 625 / 4375) < 0.001

    def test_one_inside_other(self):
        # Small box entirely inside large box
        # Intersection = 100, Union = 10000 + 100 - 100 = 10000
        result = _iou((0, 0, 100, 100), (40, 40, 50, 50))
        assert abs(result - 100 / 10000) < 0.001

    def test_zero_area_box(self):
        assert _iou((0, 0, 0, 0), (0, 0, 100, 100)) == 0.0


class TestFindUnbubbledText:
    def test_no_overlap_returns_all(self):
        regions = [
            TextRegion(bbox=(100, 100, 200, 200), confidence=0.8),
            TextRegion(bbox=(300, 300, 400, 400), confidence=0.9),
        ]
        bubbles = [(0, 0, 50, 50)]
        result = find_unbubbled_text(regions, bubbles)
        assert len(result) == 2

    def test_overlapping_region_filtered(self):
        regions = [
            TextRegion(bbox=(10, 10, 90, 90), confidence=0.8),  # overlaps with bubble
            TextRegion(bbox=(300, 300, 400, 400), confidence=0.9),  # no overlap
        ]
        bubbles = [(0, 0, 100, 100)]
        result = find_unbubbled_text(regions, bubbles)
        assert len(result) == 1
        assert result[0].bbox == (300, 300, 400, 400)

    def test_empty_regions(self):
        result = find_unbubbled_text([], [(0, 0, 100, 100)])
        assert result == []

    def test_empty_bubbles_returns_all(self):
        regions = [
            TextRegion(bbox=(10, 10, 90, 90), confidence=0.8),
        ]
        result = find_unbubbled_text(regions, [])
        assert len(result) == 1

    def test_low_overlap_not_filtered(self):
        # Regions that barely touch (IoU < 0.3) should NOT be filtered
        regions = [
            TextRegion(bbox=(90, 0, 150, 50), confidence=0.8),
        ]
        bubbles = [(0, 0, 100, 50)]  # overlaps only 10px wide strip
        result = find_unbubbled_text(regions, bubbles)
        # IoU = (10*50) / (60*50 + 100*50 - 10*50) = 500/7500 = 0.067 < 0.3
        assert len(result) == 1

    def test_custom_iou_threshold(self):
        regions = [
            TextRegion(bbox=(10, 10, 90, 90), confidence=0.8),
        ]
        bubbles = [(0, 0, 100, 100)]
        # With very high threshold, even significant overlap passes
        result = find_unbubbled_text(regions, bubbles, iou_threshold=0.99)
        assert len(result) == 1
        # With very low threshold, even small overlap gets filtered
        result = find_unbubbled_text(regions, bubbles, iou_threshold=0.01)
        assert len(result) == 0

    def test_multiple_bubbles_checked(self):
        regions = [
            TextRegion(bbox=(10, 10, 90, 90), confidence=0.8),
        ]
        # First bubble doesn't overlap, second does
        bubbles = [(500, 500, 600, 600), (0, 0, 100, 100)]
        result = find_unbubbled_text(regions, bubbles)
        assert len(result) == 0


class TestTextRegionDataclass:
    def test_fields(self):
        r = TextRegion(bbox=(1, 2, 3, 4), confidence=0.95)
        assert r.bbox == (1, 2, 3, 4)
        assert r.confidence == 0.95
