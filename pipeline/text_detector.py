"""Japanese text detection for finding text outside speech bubbles.

Two detection approaches:
1. EasyOCR — finds text overlaid on artwork (narration, titles, signs)
2. Text-stroke clustering — finds vertical text columns in white panel areas
   where the bubble detector fails because the speech bubble merges with
   the panel background (no distinct contour to detect)

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


def detect_panel_text(
    img_cv: np.ndarray,
    bubble_bboxes: list[tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int]]:
    """Find vertical text columns in white panel areas that the bubble detector missed.

    Some speech bubbles merge with the white panel background, preventing the
    contour-based detector from isolating them.  This function works bottom-up:
    find dark text strokes, cluster nearby vertical columns, then OCR-validate.

    Args:
        img_cv: OpenCV BGR image array.
        bubble_bboxes: Bounding boxes from bubble detection (already handled).

    Returns:
        List of bboxes (x1, y1, x2, y2) for validated panel text regions.
    """
    import cv2
    from PIL import Image

    from .ocr import extract_text_from_region, is_valid_japanese

    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape

    # Invert: dark text strokes become white, white background becomes black
    _, inv = cv2.threshold(gray, 120, 255, cv2.THRESH_BINARY_INV)

    # Small vertical dilation connects characters within a text column
    # but not across panel gaps
    kernel_v = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 10))
    dilated = cv2.dilate(inv, kernel_v, iterations=1)
    kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, (4, 4))
    dilated = cv2.morphologyEx(dilated, cv2.MORPH_CLOSE, kernel_close)

    contours, _ = cv2.findContours(
        dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Find individual vertical text columns
    columns = []
    for cnt in contours:
        x, y, bw, bh = cv2.boundingRect(cnt)
        area = bw * bh
        if bh < 40 or bw < 8 or area < 400 or area > 50000:
            continue
        # Must be vertical (taller than wide) — manga text columns
        if bh / max(bw, 1) < 1.5:
            continue
        # Background must be white (text on white panel area)
        roi = gray[y:y + bh, x:x + bw]
        if roi.mean() < 180:
            continue
        # Must contain dark strokes (actual text content)
        dark_pct = np.sum(roi < 100) / roi.size
        if dark_pct < 0.05 or dark_pct > 0.50:
            continue
        # Skip columns inside already-detected bubbles
        if any(_containment((x, y, x + bw, y + bh), bb) > 0.3
               for bb in bubble_bboxes):
            continue
        columns.append((x, y, x + bw, y + bh))

    if not columns:
        return []

    # Cluster nearby columns into text groups (speech bubble-level)
    # Sort right-to-left (manga reading order)
    columns.sort(key=lambda c: -c[0])
    used: set[int] = set()
    groups: list[list[tuple[int, int, int, int]]] = []

    for i, col in enumerate(columns):
        if i in used:
            continue
        group = [col]
        used.add(i)
        # Expand group with nearby columns
        changed = True
        while changed:
            changed = False
            for j, ocol in enumerate(columns):
                if j in used:
                    continue
                for gcol in group:
                    x_gap = min(abs(ocol[0] - gcol[2]),
                                abs(gcol[0] - ocol[2]))
                    y_overlap = min(gcol[3], ocol[3]) - max(gcol[1], ocol[1])
                    if x_gap < 35 and y_overlap > 20:
                        group.append(ocol)
                        used.add(j)
                        changed = True
                        break
        groups.append(group)

    # OCR-validate each group
    img_rgb = cv2.cvtColor(img_cv, cv2.COLOR_BGR2RGB)
    img_pil = Image.fromarray(img_rgb)
    results = []
    for group in groups:
        if len(group) < 2:
            continue
        gx1 = min(c[0] for c in group)
        gy1 = min(c[1] for c in group)
        gx2 = max(c[2] for c in group)
        gy2 = max(c[3] for c in group)
        pad = 10
        bbox = (max(0, gx1 - pad), max(0, gy1 - pad),
                min(w, gx2 + pad), min(h, gy2 + pad))
        text = extract_text_from_region(img_pil, bbox)
        if text.strip() and is_valid_japanese(text.strip()) and len(text.strip()) >= 3:
            results.append(bbox)
            log.info("Panel text at (%d,%d)-(%d,%d): %s",
                     *bbox, text.strip()[:40])

    log.info("Found %d panel text regions from stroke analysis", len(results))
    return results
