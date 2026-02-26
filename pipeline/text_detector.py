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


def _containment(text_bbox: tuple[int, int, int, int],
                  bubble_bbox: tuple[int, int, int, int]) -> float:
    """Fraction of text_bbox area that falls inside bubble_bbox.

    Returns 0.0-1.0.  A value of 1.0 means the text region is fully
    contained within the bubble.  This is better than IoU for this use
    case because text bboxes are much smaller than bubble bboxes.
    """
    ix1 = max(text_bbox[0], bubble_bbox[0])
    iy1 = max(text_bbox[1], bubble_bbox[1])
    ix2 = min(text_bbox[2], bubble_bbox[2])
    iy2 = min(text_bbox[3], bubble_bbox[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0
    text_area = (text_bbox[2] - text_bbox[0]) * (text_bbox[3] - text_bbox[1])
    if text_area == 0:
        return 0.0
    return inter / text_area


def find_unbubbled_text(
    text_regions: list[TextRegion],
    bubble_bboxes: list[tuple[int, int, int, int]],
    containment_threshold: float = 0.5,
) -> list[TextRegion]:
    """Return text regions that aren't contained within any detected bubble.

    Uses containment ratio (fraction of text region inside bubble) instead
    of IoU, because text bboxes are much smaller than bubble bboxes.

    Args:
        text_regions: All EasyOCR text detections on the page.
        bubble_bboxes: Bounding boxes from bubble detection.
        containment_threshold: Min fraction of text region inside a bubble
            to consider it "already handled".

    Returns:
        Text regions not significantly contained by any bubble.
    """
    unbubbled = []
    for region in text_regions:
        inside_bubble = any(
            _containment(region.bbox, bb) >= containment_threshold
            for bb in bubble_bboxes
        )
        if not inside_bubble:
            unbubbled.append(region)

    log.info("Found %d unbubbled text regions out of %d total",
             len(unbubbled), len(text_regions))
    return unbubbled
