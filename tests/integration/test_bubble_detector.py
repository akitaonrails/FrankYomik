"""Regression tests for bubble detection.

Locks in known-good detection counts and ensures face false positives
stay rejected.  Run after any threshold/filter changes.
"""

import os
import pytest
from pipeline.image_utils import load_image
from pipeline.bubble_detector import detect_bubbles, _is_color_page
from tests.conftest import DOCS_DIR


def _detect(name: str) -> list[dict]:
    img = load_image(os.path.join(DOCS_DIR, f"{name}.png"))
    return detect_bubbles(img)


def _bboxes(bubbles: list[dict]) -> list[tuple]:
    return [b["bbox"] for b in bubbles]


def _has_bubble_near(bboxes, target, tolerance=30):
    """Check if any detected bbox overlaps with target within tolerance."""
    tx1, ty1, tx2, ty2 = target
    for bx1, by1, bx2, by2 in bboxes:
        if (abs(bx1 - tx1) < tolerance and abs(by1 - ty1) < tolerance and
                abs(bx2 - tx2) < tolerance and abs(by2 - ty2) < tolerance):
            return True
    return False


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


# --- Minimum detection count tests ---
# These ensure threshold changes don't silently kill detection.

class TestMinimumDetectionCounts:
    def test_adult_min_bubbles(self):
        bubbles = _detect("adult")
        assert len(bubbles) >= 10, f"adult: expected >=10, got {len(bubbles)}"

    def test_adult2_min_bubbles(self):
        bubbles = _detect("adult2")
        assert len(bubbles) >= 20, f"adult2: expected >=20, got {len(bubbles)}"

    def test_adult3_min_bubbles(self):
        bubbles = _detect("adult3")
        assert len(bubbles) >= 10, f"adult3: expected >=10, got {len(bubbles)}"

    def test_adult4_min_bubbles(self):
        bubbles = _detect("adult4")
        assert len(bubbles) >= 22, f"adult4: expected >=22, got {len(bubbles)}"

    def test_adult5_min_bubbles(self):
        bubbles = _detect("adult5")
        assert len(bubbles) >= 12, f"adult5: expected >=12, got {len(bubbles)}"

    def test_shounen_min_bubbles(self):
        bubbles = _detect("shounen")
        assert len(bubbles) >= 11, f"shounen: expected >=11, got {len(bubbles)}"

    def test_shounen2_min_bubbles(self):
        bubbles = _detect("shounen2")
        assert len(bubbles) >= 16, f"shounen2: expected >=16, got {len(bubbles)}"

    def test_shounen3_min_bubbles(self):
        bubbles = _detect("shounen3")
        assert len(bubbles) >= 8, f"shounen3: expected >=8, got {len(bubbles)}"

    def test_shounen4_min_bubbles(self):
        bubbles = _detect("shounen4")
        assert len(bubbles) >= 21, f"shounen4: expected >=21, got {len(bubbles)}"

    def test_shounen5_min_bubbles(self):
        bubbles = _detect("shounen5")
        assert len(bubbles) >= 12, f"shounen5: expected >=12, got {len(bubbles)}"

    def test_shounen6_min_bubbles(self):
        bubbles = _detect("shounen6")
        assert len(bubbles) >= 24, f"shounen6: expected >=24, got {len(bubbles)}"

    def test_shounen7_min_bubbles(self):
        bubbles = _detect("shounen7")
        assert len(bubbles) >= 19, f"shounen7: expected >=19, got {len(bubbles)}"

    def test_shounen8_min_bubbles(self):
        bubbles = _detect("shounen8")
        assert len(bubbles) >= 16, f"shounen8: expected >=16, got {len(bubbles)}"

    def test_shounen9_min_bubbles(self):
        bubbles = _detect("shounen9")
        assert len(bubbles) >= 11, f"shounen9: expected >=11, got {len(bubbles)}"

    def test_shounen10_min_bubbles(self):
        bubbles = _detect("shounen10")
        assert len(bubbles) >= 13, f"shounen10: expected >=13, got {len(bubbles)}"


# --- Known bubble presence tests ---
# Specific bubbles that must be detected (were missed before and fixed).

class TestKnownBubblePresence:
    def test_shounen2_top_left_bubbles(self):
        """Top-left bubbles that were missed due to edge_density threshold."""
        bboxes = _bboxes(_detect("shounen2"))
        assert _has_bubble_near(bboxes, (468, 431, 601, 624)), \
            "shounen2: missing top-left bubble near (468,431)"
        assert _has_bubble_near(bboxes, (684, 150, 793, 310)), \
            "shounen2: missing top-left bubble near (684,150)"

    def test_shounen2_large_text_bubble(self):
        """Large text bubble that should always be detected."""
        bboxes = _bboxes(_detect("shounen2"))
        assert _has_bubble_near(bboxes, (1005, 79, 1173, 461)), \
            "shounen2: missing large bubble near (1005,79)"

    def test_adult_key_bubbles(self):
        bboxes = _bboxes(_detect("adult"))
        assert _has_bubble_near(bboxes, (730, 143, 1005, 421), tolerance=40), \
            "adult: missing key bubble near (730,143)"
        assert _has_bubble_near(bboxes, (892, 457, 1166, 699), tolerance=40), \
            "adult: missing key bubble near (892,457)"

    def test_shounen_left_middle_bubble(self):
        """Tall vertical bubble with furigana on left page must be detected."""
        bboxes = _bboxes(_detect("shounen"))
        assert _has_bubble_near(bboxes, (687, 588, 817, 819), tolerance=40), \
            "shounen: missing left-middle bubble near (687,588)"

    def test_shounen6_right_page_bubbles(self):
        """Right-page speech bubbles on shounen6 that contain actual text."""
        bboxes = _bboxes(_detect("shounen6"))
        assert _has_bubble_near(bboxes, (1621, 440, 1744, 608), tolerance=40), \
            "shounen6: missing 'hitori' bubble near (1621,440)"
        assert _has_bubble_near(bboxes, (1854, 90, 2057, 332), tolerance=40), \
            "shounen6: missing large text bubble near (1854,90)"

    def test_shounen7_first_panel_bubbles(self):
        """First-panel bubbles on shounen7 that were missed when misclassified as color."""
        bboxes = _bboxes(_detect("shounen7"))
        assert _has_bubble_near(bboxes, (512, 302, 574, 380), tolerance=40), \
            "shounen7: missing first-panel bubble near (512,302)"

    def test_shounen10_first_balloon(self):
        """First balloon on shounen10 must be detected."""
        bboxes = _bboxes(_detect("shounen10"))
        assert _has_bubble_near(bboxes, (386, 517, 585, 913), tolerance=40), \
            "shounen10: missing first balloon near (386,517)"

    def test_shounen10_top_right_balloon(self):
        """Top-right balloon on shounen10, recovered via page-edge circularity exemption."""
        bboxes = _bboxes(_detect("shounen10"))
        assert _has_bubble_in_region(bboxes, (1900, 0, 2100, 350)), \
            "shounen10: missing top-right balloon in region (1900,0,2100,350)"


# --- Face false positive tests ---
# These regions must NOT be detected as bubbles.

class TestFaceRejection:
    def test_shounen2_no_girl_face(self):
        """Girl's face in shounen2 must not be detected."""
        bboxes = _bboxes(_detect("shounen2"))
        assert not _has_bubble_in_region(bboxes, (741, 569, 845, 659)), \
            "shounen2: girl face region (741,569,845,659) falsely detected"

    def test_shounen2_no_boy_face(self):
        """Boy's face area in shounen2 must not be detected."""
        bboxes = _bboxes(_detect("shounen2"))
        assert not _has_bubble_in_region(bboxes, (706, 380, 751, 485)), \
            "shounen2: boy face region (706,380,751,485) falsely detected"

    def test_shounen5_no_girl_face(self):
        """Girl's face in shounen5 must not be detected."""
        bboxes = _bboxes(_detect("shounen5"))
        assert not _has_bubble_in_region(bboxes, (843, 508, 952, 656)), \
            "shounen5: girl face region (843,508,952,656) falsely detected"

    def test_shounen2_no_girl_eye_face(self):
        """Girl's eye/face in shounen2 upper area must not be detected."""
        bboxes = _bboxes(_detect("shounen2"))
        assert not _has_bubble_in_region(bboxes, (863, 113, 1011, 280)), \
            "shounen2: girl eye/face region (863,113,1011,280) falsely detected"

    def test_shounen4_no_blue_hair_face(self):
        """Blue-haired girl's face in shounen4 must not be detected."""
        bboxes = _bboxes(_detect("shounen4"))
        assert not _has_bubble_in_region(bboxes, (1832, 204, 1965, 371)), \
            "shounen4: blue-haired girl face (1832,204,1965,371) falsely detected"

    def test_shounen3_no_face_false_positives(self):
        """shounen3 has window/face regions that must not be detected."""
        bubbles = _detect("shounen3")
        # Rect fallback recovers many real bubbles; count should stay bounded
        assert len(bubbles) <= 25, \
            f"shounen3: too many detections ({len(bubbles)}), likely face FPs"

    def test_shounen3_no_window_frame(self):
        """Window frame in shounen3 must not be detected as a bubble."""
        bboxes = _bboxes(_detect("shounen3"))
        assert not _has_bubble_in_region(bboxes, (1326, 779, 1448, 1026)), \
            "shounen3: window frame (1326,779,1448,1026) falsely detected"


# --- Color vs grayscale page detection ---

class TestColorDetection:
    def test_grayscale_pages(self):
        for name in ["adult", "adult2", "adult3", "adult4", "adult5",
                      "shounen", "shounen2", "shounen3", "shounen5",
                      "shounen6", "shounen7", "shounen10"]:
            img = load_image(os.path.join(DOCS_DIR, f"{name}.png"))
            assert not _is_color_page(img), f"{name} should be grayscale"

    def test_color_pages(self):
        for name in ["shounen4", "shounen8", "shounen9"]:
            img = load_image(os.path.join(DOCS_DIR, f"{name}.png"))
            assert _is_color_page(img), f"{name} should be color"
