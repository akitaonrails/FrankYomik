"""Japanese text detection using EasyOCR for finding text outside speech bubbles.

Detects text regions on manga pages that the contour-based bubble detector
misses — typically narration overlaid on artwork, chapter titles on art, etc.

Uses a separate EasyOCR instance from the Korean webtoon reader (different
language, single pass, no text recognition — manga-ocr handles that).
"""

import logging
import threading
from dataclasses import dataclass

import numpy as np

from .config import TEXT_DETECTION_CONFIDENCE, TEXT_DETECTION_GPU

log = logging.getLogger(__name__)

# Singleton EasyOCR reader with thread-safe initialization
_reader = None
_init_lock = threading.Lock()


def _get_reader():
    """Lazy-load EasyOCR Japanese reader on first use."""
    global _reader
    if _reader is None:
        with _init_lock:
            if _reader is None:
                import easyocr
                log.info("Loading EasyOCR (Japanese, gpu=%s)...", TEXT_DETECTION_GPU)
                _reader = easyocr.Reader(["ja"], gpu=TEXT_DETECTION_GPU)
                log.info("EasyOCR Japanese loaded")
    return _reader


@dataclass
class TextRegion:
    """A detected text region on a manga page."""
    bbox: tuple[int, int, int, int]  # (x1, y1, x2, y2)
    confidence: float
    # No text field — manga-ocr will do the actual reading


def detect_text_regions(img_cv: np.ndarray) -> list[TextRegion]:
    """Detect text regions on a manga page using EasyOCR.

    Single-pass detection (no CLAHE/inverted — manga is cleaner than webtoons).
    Returns axis-aligned bounding boxes above the confidence threshold.

    Args:
        img_cv: OpenCV BGR image array.

    Returns:
        List of TextRegion with confidence above TEXT_DETECTION_CONFIDENCE.
    """
    import cv2

    reader = _get_reader()
    img_rgb = cv2.cvtColor(img_cv, cv2.COLOR_BGR2RGB)

    results = reader.readtext(img_rgb)

    regions = []
    for bbox_poly, _text, confidence in results:
        if confidence < TEXT_DETECTION_CONFIDENCE:
            continue

        xs = [pt[0] for pt in bbox_poly]
        ys = [pt[1] for pt in bbox_poly]
        bbox = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))

        regions.append(TextRegion(bbox=bbox, confidence=confidence))

    log.info("EasyOCR detected %d text regions (threshold=%.2f)",
             len(regions), TEXT_DETECTION_CONFIDENCE)
    return regions


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Intersection over Union of two (x1, y1, x2, y2) rectangles."""
    ix1 = max(a[0], b[0])
    iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2])
    iy2 = min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


def find_unbubbled_text(
    text_regions: list[TextRegion],
    bubble_bboxes: list[tuple[int, int, int, int]],
    iou_threshold: float = 0.3,
) -> list[TextRegion]:
    """Return text regions that don't overlap with any detected bubble.

    These are artwork-text candidates: narration on art, chapter titles, etc.

    Args:
        text_regions: All EasyOCR text detections on the page.
        bubble_bboxes: Bounding boxes from bubble detection.
        iou_threshold: Minimum IoU to consider a text region "inside" a bubble.

    Returns:
        Text regions with no significant bubble overlap.
    """
    unbubbled = []
    for region in text_regions:
        overlaps_bubble = any(
            _iou(region.bbox, bb) >= iou_threshold
            for bb in bubble_bboxes
        )
        if not overlaps_bubble:
            unbubbled.append(region)

    log.info("Found %d unbubbled text regions out of %d total",
             len(unbubbled), len(text_regions))
    return unbubbled
