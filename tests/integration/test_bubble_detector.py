"""Integration tests for RT-DETR-v2 bubble detection.

Tests that the model loads, returns correct output format, and detects
bubbles on real manga test pages.
"""

import os
import pytest
from pipeline.image_utils import load_image
from pipeline.bubble_detector import detect_bubbles
from tests.conftest import DOCS_DIR


def _detect(name: str) -> list[dict]:
    img = load_image(os.path.join(DOCS_DIR, f"{name}.png"))
    return detect_bubbles(img)


def _bboxes(bubbles: list[dict]) -> list[tuple]:
    return [b["bbox"] for b in bubbles]


def _has_bubble_in_region(bboxes, region, min_overlap=0.3):
    """Check if any detected bbox significantly overlaps with a region."""
    rx1, ry1, rx2, ry2 = region
    r_area = (rx2 - rx1) * (ry2 - ry1)
    for bx1, by1, bx2, by2 in bboxes:
        ox1 = max(bx1, rx1)
        oy1 = max(by1, ry1)
        ox2 = min(bx2, rx2)
        oy2 = min(by2, ry2)
        if ox2 > ox1 and oy2 > oy1:
            overlap = (ox2 - ox1) * (oy2 - oy1)
            if overlap / r_area > min_overlap:
                return True
    return False


class TestOutputFormat:
    """Verify the detector returns correctly structured results."""

    def test_returns_list(self):
        result = _detect("shounen")
        assert isinstance(result, list)

    def test_dict_keys(self):
        result = _detect("shounen")
        assert len(result) > 0
        for det in result:
            assert "bbox" in det
            assert "type" in det
            assert "score" in det
            assert isinstance(det["bbox"], tuple)
            assert len(det["bbox"]) == 4
            assert det["type"] in ("speech_bubble", "artwork_text")
            assert 0.0 <= det["score"] <= 1.0

    def test_bbox_coordinates_valid(self):
        result = _detect("shounen")
        for det in result:
            x1, y1, x2, y2 = det["bbox"]
            assert x2 > x1, f"Invalid bbox width: {det['bbox']}"
            assert y2 > y1, f"Invalid bbox height: {det['bbox']}"


class TestDetectsContent:
    """Verify the model finds bubbles on each test page."""

    @pytest.mark.parametrize("name", [
        "adult", "adult2", "adult3", "adult4", "adult5",
        "shounen", "shounen2", "shounen3", "shounen4", "shounen5",
        "shounen6", "shounen7", "shounen8", "shounen9", "shounen10",
    ])
    def test_detects_at_least_one(self, name):
        """Every test page should have at least one detection."""
        result = _detect(name)
        assert len(result) >= 1, f"{name}: no detections found"

    @pytest.mark.parametrize("name,min_count", [
        ("shounen", 5),
        ("shounen2", 5),
        ("shounen4", 5),
        ("adult", 5),
        ("adult2", 5),
    ])
    def test_minimum_detections(self, name, min_count):
        """Pages with many bubbles should have reasonable detection counts."""
        result = _detect(name)
        assert len(result) >= min_count, \
            f"{name}: expected >={min_count}, got {len(result)}"
