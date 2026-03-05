"""Unit tests for webtoon bubble detector clustering logic."""

import numpy as np

from webtoon.bubble_detector import (
    cluster_detections,
    detect_bubbles,
    _cluster_bbox,
    _is_sfx_detection,
    _should_merge,
    _spans_image,
)
from webtoon.ocr import TextDetection


def _make_det(x1, y1, x2, y2, text="테스트", conf=0.9):
    """Helper to create a TextDetection."""
    return TextDetection(
        bbox_poly=[[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
        text=text,
        confidence=conf,
        bbox_rect=(x1, y1, x2, y2),
    )


class TestClusterDetections:
    def test_single_detection(self):
        dets = [_make_det(10, 10, 100, 30)]
        clusters = cluster_detections(dets)
        assert len(clusters) == 1
        assert len(clusters[0]) == 1

    def test_nearby_vertical_detections_clustered(self):
        # Two text lines close vertically (gap=20, within default threshold=40)
        dets = [
            _make_det(10, 10, 100, 30),
            _make_det(10, 50, 100, 70),  # gap is 20px
        ]
        clusters = cluster_detections(dets)
        assert len(clusters) == 1
        assert len(clusters[0]) == 2

    def test_far_apart_detections_separate(self):
        # Two text lines far apart vertically
        dets = [
            _make_det(10, 10, 100, 30),
            _make_det(10, 200, 100, 220),  # gap is 170px
        ]
        clusters = cluster_detections(dets)
        assert len(clusters) == 2

    def test_three_detections_two_clusters(self):
        dets = [
            _make_det(10, 10, 100, 30),
            _make_det(10, 50, 100, 70),   # close to first
            _make_det(10, 300, 100, 320),  # far from both
        ]
        clusters = cluster_detections(dets)
        assert len(clusters) == 2

    def test_empty_input(self):
        assert cluster_detections([]) == []

    def test_horizontal_non_overlap_separate(self):
        # Two detections at same Y but no horizontal overlap
        dets = [
            _make_det(10, 10, 50, 30),
            _make_det(200, 10, 250, 30),
        ]
        clusters = cluster_detections(dets)
        # They should be separate since they don't overlap horizontally
        assert len(clusters) == 2


class TestClusterBbox:
    def test_single_detection_bbox(self):
        dets = [_make_det(10, 20, 100, 50)]
        assert _cluster_bbox(dets) == (10, 20, 100, 50)

    def test_multiple_detections_bbox(self):
        dets = [
            _make_det(10, 20, 100, 50),
            _make_det(5, 60, 120, 80),
        ]
        assert _cluster_bbox(dets) == (5, 20, 120, 80)


class TestShouldMerge:
    def test_close_vertical_merge(self):
        cluster = [_make_det(10, 10, 100, 30)]
        det = _make_det(10, 50, 100, 70)
        assert _should_merge(cluster, det, gap=40) is True

    def test_far_vertical_no_merge(self):
        cluster = [_make_det(10, 10, 100, 30)]
        det = _make_det(10, 200, 100, 220)
        assert _should_merge(cluster, det, gap=40) is False


class TestSpansImage:
    """Reject bubble boundaries that span most of the image.

    Regression for 082: both bubbles had bbox=(0,0,690,1600) covering the
    entire 690x1600 image.  Image-edge contours should be rejected so the
    pipeline falls back to padded text bbox instead.
    """

    def test_full_image_span_rejected(self):
        """bbox covering entire image is rejected."""
        assert _spans_image((0, 0, 690, 1600), 690, 1600) is True

    def test_normal_bubble_accepted(self):
        """Normal-sized bubble passes the check."""
        assert _spans_image((100, 200, 400, 500), 690, 1600) is False

    def test_wide_but_short_accepted(self):
        """A wide notification bar (100% width, 10% height) is accepted."""
        assert _spans_image((0, 400, 690, 560), 690, 1600) is False

    def test_tall_but_narrow_accepted(self):
        """A tall narrow bubble (20% width, 80% height) is accepted."""
        assert _spans_image((200, 50, 340, 1350), 690, 1600) is False

    def test_threshold_boundary(self):
        """bbox at exactly 70% of both dimensions is rejected."""
        # 70% of 1000 = 700
        assert _spans_image((0, 0, 700, 700), 1000, 1000) is False  # equal not >
        assert _spans_image((0, 0, 701, 701), 1000, 1000) is True


class TestIsSfxDetection:
    """Pre-clustering SFX filter prevents oversized bboxes from
    contaminating dialogue clusters.

    Regression for 297/057: SFX "꽈양" (detected as "값", 230x318px)
    overlapped vertically with dialogue "도우러 가자!" and got merged
    into the same cluster, corrupting the speech bubble render.
    """

    def test_tall_single_char_is_sfx(self):
        """Single character >100px tall is SFX."""
        det = _make_det(15, 535, 245, 853, text="값")  # 318px tall
        assert _is_sfx_detection(det) is True

    def test_oversized_two_chars_is_sfx(self):
        """Two characters >200px tall is SFX."""
        det = _make_det(10, 10, 200, 226, text="하하")  # 216px tall
        assert _is_sfx_detection(det) is True

    def test_short_dialogue_not_sfx(self):
        """Short dialogue like '뭐?' (114px, 2 chars) is NOT SFX."""
        det = _make_det(300, 800, 450, 914, text="뭐?")  # 114px tall
        assert _is_sfx_detection(det) is False

    def test_normal_dialogue_not_sfx(self):
        """Normal multi-char dialogue is never SFX."""
        det = _make_det(100, 100, 400, 150, text="도우러 가자!")
        assert _is_sfx_detection(det) is False

    def test_small_single_char_not_sfx(self):
        """Small single character (normal text) is not SFX."""
        det = _make_det(100, 100, 130, 140, text="네")  # 40px tall
        assert _is_sfx_detection(det) is False


class TestDetectBubblesReturnType:
    """detect_bubbles() returns (bubbles, sfx_detections) tuple."""

    def test_returns_tuple(self):
        """Return value is a 2-tuple of (bubbles, sfx_list)."""
        img = np.full((400, 400, 3), 200, dtype=np.uint8)
        dets = [_make_det(100, 100, 300, 140, text="대화입니다")]
        result = detect_bubbles(img, dets)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_sfx_separated_from_dialogue(self):
        """SFX detections go to sfx_list, dialogue to bubbles."""
        img = np.full((600, 400, 3), 200, dtype=np.uint8)
        dialogue = _make_det(100, 100, 300, 140, text="대화입니다")
        sfx = _make_det(15, 535, 245, 853, text="값")  # tall single char
        bubbles, sfx_list = detect_bubbles(img, [dialogue, sfx])
        assert len(bubbles) == 1
        assert len(sfx_list) == 1
        assert sfx_list[0].text == "값"

    def test_no_sfx_returns_empty_list(self):
        """When no SFX present, sfx_list is empty."""
        img = np.full((400, 400, 3), 200, dtype=np.uint8)
        dets = [_make_det(100, 100, 300, 140, text="대화입니다")]
        bubbles, sfx_list = detect_bubbles(img, dets)
        assert len(sfx_list) == 0
        assert len(bubbles) == 1

    def test_empty_input(self):
        """Empty detections returns empty bubbles and empty sfx."""
        img = np.full((400, 400, 3), 200, dtype=np.uint8)
        bubbles, sfx_list = detect_bubbles(img, [])
        assert bubbles == []
        assert sfx_list == []
