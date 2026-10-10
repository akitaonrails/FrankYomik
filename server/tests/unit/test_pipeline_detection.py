"""Contract tests for backend-neutral semantic detection post-processing."""

import pytest

from pipeline.detection import (
    DetectionThresholds,
    SemanticDetection,
    adapt_rtdetr_detections,
    legacy_processing_regions,
    validate_detection_collection,
)
from pipeline.document import BoundingBox, Provenance

PROVENANCE = Provenance("rtdetr-v2", "weights-1", "detector@weights-1")


def test_rtdetr_adapter_preserves_balloon_and_text_relationship():
    regions = adapt_rtdetr_detections([
        {"label": "bubble", "bbox": (10, 10, 110, 130), "score": 0.91},
        {"label": "text_bubble", "bbox": (30, 30, 90, 100), "score": 0.88},
    ], PROVENANCE)

    balloon = next(item for item in regions if item.kind == "balloon")
    text = next(item for item in regions if item.kind == "text")
    assert text.parent_id == balloon.id
    assert text.raw_label == "text_bubble"
    assert text.provenance == PROVENANCE

    legacy = legacy_processing_regions(regions)
    assert len(legacy) == 1
    assert legacy[0]["bbox"] == (10, 10, 110, 130)
    assert legacy[0]["text_child_ids"] == [text.id]


def test_thresholds_and_deduplication_are_class_aware():
    regions = adapt_rtdetr_detections([
        {"label": "bubble", "bbox": (10, 10, 100, 100), "score": 0.80},
        {"label": "bubble", "bbox": (11, 11, 99, 99), "score": 0.75},
        {"label": "text_bubble", "bbox": (15, 15, 95, 95), "score": 0.70},
        {"label": "text_free", "bbox": (150, 20, 190, 70), "score": 0.59},
        {"label": "panel", "bbox": (0, 0, 200, 200), "score": 0.49},
    ], PROVENANCE, thresholds=DetectionThresholds())

    assert [item.kind for item in regions].count("balloon") == 1
    assert [item.kind for item in regions].count("text") == 1
    assert all(item.kind not in {"sfx", "panel"} for item in regions)


def test_reading_order_is_deterministic_for_manga_and_webtoon():
    raw = [
        {"label": "text_free", "bbox": (20, 200, 50, 240), "score": 0.8},
        {"label": "bubble", "bbox": (200, 10, 290, 100), "score": 0.9},
        {"label": "bubble", "bbox": (20, 10, 110, 100), "score": 0.9},
    ]

    manga = adapt_rtdetr_detections(raw, PROVENANCE, reading_direction="manga")
    webtoon = adapt_rtdetr_detections(raw, PROVENANCE, reading_direction="vertical")

    assert [item.bbox.x1 for item in manga] == [200, 20, 20]
    assert [item.bbox.x1 for item in webtoon] == [20, 200, 20]
    assert [item.reading_order for item in manga] == [0, 1, 2]


def test_manga_staggered_overlapping_regions_share_visual_row():
    regions = adapt_rtdetr_detections([
        {"label": "bubble", "bbox": (20, 65, 110, 125), "score": 0.9},
        {"label": "bubble", "bbox": (200, 20, 290, 80), "score": 0.9},
        {"label": "text_free", "bbox": (150, 170, 190, 210), "score": 0.8},
    ], PROVENANCE, reading_direction="manga")

    assert [item.bbox.x1 for item in regions] == [200, 20, 150]


def test_manga_panels_group_their_own_visual_rows_right_to_left():
    regions = adapt_rtdetr_detections([
        {"label": "panel", "bbox": (0, 0, 190, 300), "score": 0.9},
        {"label": "bubble", "bbox": (20, 30, 100, 100), "score": 0.9},
        {"label": "panel", "bbox": (200, 0, 400, 300), "score": 0.9},
        {"label": "bubble", "bbox": (300, 35, 380, 105), "score": 0.9},
        {"label": "text_bubble", "bbox": (320, 45, 360, 90), "score": 0.9},
    ], PROVENANCE, reading_direction="manga")

    assert [(item.kind, item.bbox.x1) for item in regions] == [
        ("panel", 200), ("balloon", 300), ("text", 320),
        ("panel", 0), ("balloon", 20),
    ]
    assert regions[2].parent_id == regions[1].id


def test_linking_chooses_smallest_equally_containing_balloon():
    regions = adapt_rtdetr_detections([
        {"label": "bubble", "bbox": (0, 0, 200, 200), "score": 0.9},
        {"label": "bubble", "bbox": (20, 20, 120, 120), "score": 0.9},
        {"label": "text_bubble", "bbox": (40, 40, 80, 80), "score": 0.9},
    ], PROVENANCE)

    text = next(item for item in regions if item.kind == "text")
    parent = next(item for item in regions if item.id == text.parent_id)
    assert parent.bbox.as_list() == [20, 20, 120, 120]


def test_raw_detection_permutation_does_not_change_identity_or_relationships():
    raw = [
        {"label": "bubble", "bbox": (10, 10, 110, 130), "score": 0.70},
        {"label": "text_bubble", "bbox": (30, 30, 90, 100), "score": 0.88},
        {"label": "bubble", "bbox": (10, 10, 110, 130), "score": 0.91},
        {"label": "text_free", "bbox": (150, 30, 190, 80), "score": 0.75},
    ]

    forward = adapt_rtdetr_detections(raw, PROVENANCE)
    reversed_input = adapt_rtdetr_detections(list(reversed(raw)), PROVENANCE)

    assert reversed_input == forward


@pytest.mark.parametrize(
    ("detections", "message"),
    [
        (
            [
                SemanticDetection(
                    "same", "balloon", BoundingBox(0, 0, 20, 20),
                    0.9, "bubble", PROVENANCE, reading_order=0,
                ),
                SemanticDetection(
                    "same", "text", BoundingBox(2, 2, 10, 10),
                    0.8, "text_bubble", PROVENANCE, reading_order=1,
                ),
            ],
            "ids",
        ),
        (
            [SemanticDetection(
                "text", "text", BoundingBox(2, 2, 10, 10),
                0.8, "text_bubble", PROVENANCE,
                parent_id="missing", reading_order=0,
            )],
            "unknown parent",
        ),
        (
            [
                SemanticDetection(
                    "balloon", "balloon", BoundingBox(0, 0, 20, 20),
                    0.9, "bubble", PROVENANCE, reading_order=0,
                ),
                SemanticDetection(
                    "sfx", "sfx", BoundingBox(30, 0, 50, 20),
                    0.8, "text_free", PROVENANCE, reading_order=0,
                ),
            ],
            "reading orders",
        ),
    ],
)
def test_semantic_collection_boundary_rejects_ambiguous_graphs(
    detections, message,
):
    with pytest.raises(ValueError, match=message):
        validate_detection_collection(detections)
