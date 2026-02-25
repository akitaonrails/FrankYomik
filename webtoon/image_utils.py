"""Image utilities for tall webtoon strips (vertical scroll format).

Webtoon panels can be 800x15000+ pixels. Split into manageable strips
for OCR processing, then stitch results back together.
"""

import logging

import numpy as np

from .ocr import TextDetection

log = logging.getLogger(__name__)


def split_tall_image(img: np.ndarray, max_height: int = 2000,
                     overlap: int = 100) -> list[tuple[np.ndarray, int]]:
    """Split a tall image into overlapping horizontal strips.

    Args:
        img: OpenCV image (BGR).
        max_height: Maximum strip height in pixels.
        overlap: Pixels of overlap between adjacent strips.

    Returns:
        List of (strip_image, y_offset) tuples.
    """
    h, w = img.shape[:2]

    if h <= max_height:
        return [(img, 0)]

    strips = []
    y = 0
    while y < h:
        y_end = min(y + max_height, h)
        strip = img[y:y_end].copy()
        strips.append((strip, y))

        if y_end >= h:
            break
        y = y_end - overlap

    log.info("Split %dx%d image into %d strips (max_h=%d, overlap=%d)",
             w, h, len(strips), max_height, overlap)
    return strips


def stitch_detections(strip_results: list[tuple[list[TextDetection], int]]
                      ) -> list[TextDetection]:
    """Merge detections from overlapping strips, deduplicating overlap regions.

    Args:
        strip_results: List of (detections, y_offset) from each strip.

    Returns:
        Merged detections with coordinates mapped to the full image.
    """
    all_detections: list[TextDetection] = []

    for detections, y_offset in strip_results:
        for det in detections:
            # Map bbox to full image coordinates
            x1, y1, x2, y2 = det.bbox_rect
            mapped = TextDetection(
                bbox_poly=[[pt[0], pt[1] + y_offset] for pt in det.bbox_poly],
                text=det.text,
                confidence=det.confidence,
                bbox_rect=(x1, y1 + y_offset, x2, y2 + y_offset),
            )
            all_detections.append(mapped)

    # Deduplicate detections in overlap regions
    return _deduplicate(all_detections)


def _deduplicate(detections: list[TextDetection],
                 iou_threshold: float = 0.5) -> list[TextDetection]:
    """Remove duplicate detections using IoU (Intersection over Union)."""
    if len(detections) <= 1:
        return detections

    # Sort by confidence (keep highest)
    sorted_dets = sorted(detections, key=lambda d: d.confidence, reverse=True)
    keep = []

    for det in sorted_dets:
        is_dup = False
        for kept in keep:
            if _iou(det.bbox_rect, kept.bbox_rect) > iou_threshold:
                is_dup = True
                break
        if not is_dup:
            keep.append(det)

    removed = len(detections) - len(keep)
    if removed > 0:
        log.info("Deduplicated %d overlapping detections", removed)

    return keep


def _iou(a: tuple[int, int, int, int],
         b: tuple[int, int, int, int]) -> float:
    """Compute Intersection over Union of two bboxes."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b

    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)

    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0

    intersection = (ix2 - ix1) * (iy2 - iy1)
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    union = area_a + area_b - intersection

    return intersection / union if union > 0 else 0.0
