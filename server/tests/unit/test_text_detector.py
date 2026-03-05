"""Unit tests for text detector overlap logic (no EasyOCR dependency)."""

from kindle.text_detector import TextRegion, find_unbubbled_text, _containment


class TestContainment:
    def test_fully_contained(self):
        # Small text bbox fully inside large bubble
        assert _containment((40, 40, 60, 60), (0, 0, 100, 100)) == 1.0

    def test_no_overlap(self):
        assert _containment((0, 0, 50, 50), (100, 100, 200, 200)) == 0.0

    def test_partial_overlap(self):
        # Text bbox (0,0)-(100,50) partially inside bubble (50,0)-(200,50)
        # Intersection: 50x50 = 2500, text area: 100x50 = 5000
        result = _containment((0, 0, 100, 50), (50, 0, 200, 50))
        assert abs(result - 0.5) < 0.001

    def test_text_larger_than_bubble(self):
        # Text bbox larger than bubble — only partial containment
        # Text: 200x200=40000, Intersection: 100x100=10000
        result = _containment((0, 0, 200, 200), (50, 50, 150, 150))
        assert abs(result - 10000 / 40000) < 0.001

    def test_zero_area_text(self):
        assert _containment((0, 0, 0, 0), (0, 0, 100, 100)) == 0.0


class TestFindUnbubbledText:
    def test_no_overlap_returns_all(self):
        regions = [
            TextRegion(bbox=(100, 100, 200, 200), confidence=0.8),
            TextRegion(bbox=(300, 300, 400, 400), confidence=0.9),
        ]
        bubbles = [(0, 0, 50, 50)]
        result = find_unbubbled_text(regions, bubbles)
        assert len(result) == 2

    def test_contained_region_filtered(self):
        # Text fully inside bubble → filtered out
        regions = [
            TextRegion(bbox=(20, 20, 80, 80), confidence=0.8),
            TextRegion(bbox=(300, 300, 400, 400), confidence=0.9),
        ]
        bubbles = [(0, 0, 100, 100)]
        result = find_unbubbled_text(regions, bubbles)
        assert len(result) == 1
        assert result[0].bbox == (300, 300, 400, 400)

    def test_small_text_inside_large_bubble_filtered(self):
        # Realistic case: small text bbox inside much larger bubble bbox
        # Text 50x30 fully inside bubble 200x150
        regions = [
            TextRegion(bbox=(75, 60, 125, 90), confidence=0.8),
        ]
        bubbles = [(0, 0, 200, 150)]
        result = find_unbubbled_text(regions, bubbles)
        assert len(result) == 0  # should be filtered (100% contained)

    def test_empty_regions(self):
        result = find_unbubbled_text([], [(0, 0, 100, 100)])
        assert result == []

    def test_empty_bubbles_returns_all(self):
        regions = [
            TextRegion(bbox=(10, 10, 90, 90), confidence=0.8),
        ]
        result = find_unbubbled_text(regions, [])
        assert len(result) == 1

    def test_low_containment_not_filtered(self):
        # Text barely overlaps bubble edge — less than 50% contained
        # Text (90,0)-(150,50) = 60x50, overlap with (0,0)-(100,50) = 10x50
        # Containment = 500/3000 = 0.167 < 0.5
        regions = [
            TextRegion(bbox=(90, 0, 150, 50), confidence=0.8),
        ]
        bubbles = [(0, 0, 100, 50)]
        result = find_unbubbled_text(regions, bubbles)
        assert len(result) == 1

    def test_custom_threshold(self):
        # Text (20,20)-(80,80) fully inside (0,0)-(100,100) → containment=1.0
        regions = [
            TextRegion(bbox=(20, 20, 80, 80), confidence=0.8),
        ]
        bubbles = [(0, 0, 100, 100)]
        # Very high threshold → passes through
        result = find_unbubbled_text(regions, bubbles, containment_threshold=1.1)
        assert len(result) == 1
        # Low threshold → filtered
        result = find_unbubbled_text(regions, bubbles, containment_threshold=0.01)
        assert len(result) == 0

    def test_multiple_bubbles_checked(self):
        regions = [
            TextRegion(bbox=(20, 20, 80, 80), confidence=0.8),
        ]
        # First bubble doesn't contain, second does
        bubbles = [(500, 500, 600, 600), (0, 0, 100, 100)]
        result = find_unbubbled_text(regions, bubbles)
        assert len(result) == 0


class TestTextRegionDataclass:
    def test_fields(self):
        r = TextRegion(bbox=(1, 2, 3, 4), confidence=0.95)
        assert r.bbox == (1, 2, 3, 4)
        assert r.confidence == 0.95
