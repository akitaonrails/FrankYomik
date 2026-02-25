"""Unit tests for webtoon bubble detector clustering logic."""

from webtoon.bubble_detector import (
    cluster_detections,
    _cluster_bbox,
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
