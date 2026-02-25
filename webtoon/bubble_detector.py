"""Text-first bubble detection for Korean webtoons.

Unlike manga (contour-first), webtoons have irregular/colored bubbles that
don't respond well to binary threshold + contour analysis. Instead:
1. Run EasyOCR to find text regions
2. Cluster nearby text detections into bubble groups
3. Find bubble boundary around each cluster (edge contour → flood fill → padded bbox)
4. Sample background color from surrounding pixels
"""

import logging
from dataclasses import dataclass

import cv2
import numpy as np

from .config import (
    CLUSTER_GAP,
    CONTOUR_EXPAND,
    FLOOD_FILL_TOLERANCE,
    PAD_X,
    PAD_Y,
)
from .ocr import TextDetection

log = logging.getLogger(__name__)


@dataclass
class WebtoonBubble:
    """A detected bubble with its text content."""
    bbox: tuple[int, int, int, int]           # (x1, y1, x2, y2)
    text_regions: list[TextDetection]
    combined_text: str = ""
    has_bubble_boundary: bool = False          # True if visual boundary was found
    bg_color: tuple[int, int, int] = (255, 255, 255)  # Sampled background color
    bubble_mask: np.ndarray | None = None      # Optional mask for contour-based clearing


def cluster_detections(detections: list[TextDetection],
                       gap: int = CLUSTER_GAP) -> list[list[TextDetection]]:
    """Cluster text detections by vertical proximity.

    Text lines in the same speech bubble are typically close vertically.
    Groups detections where the vertical gap between consecutive bboxes
    is less than `gap` pixels and they overlap horizontally.
    """
    if not detections:
        return []

    # Sort by vertical center
    sorted_dets = sorted(detections, key=lambda d: (d.bbox_rect[1] + d.bbox_rect[3]) / 2)

    clusters: list[list[TextDetection]] = [[sorted_dets[0]]]

    for det in sorted_dets[1:]:
        merged = False
        for cluster in clusters:
            if _should_merge(cluster, det, gap):
                cluster.append(det)
                merged = True
                break
        if not merged:
            clusters.append([det])

    return clusters


def _should_merge(cluster: list[TextDetection], det: TextDetection,
                  gap: int) -> bool:
    """Check if a detection should join an existing cluster."""
    # Cluster bounding box
    cx1 = min(d.bbox_rect[0] for d in cluster)
    cy1 = min(d.bbox_rect[1] for d in cluster)
    cx2 = max(d.bbox_rect[2] for d in cluster)
    cy2 = max(d.bbox_rect[3] for d in cluster)

    dx1, dy1, dx2, dy2 = det.bbox_rect

    # Vertical gap check
    vert_gap = max(0, dy1 - cy2, cy1 - dy2)
    if vert_gap > gap:
        return False

    # Horizontal overlap check — at least some overlap
    horiz_overlap = min(cx2, dx2) - max(cx1, dx1)
    min_width = min(cx2 - cx1, dx2 - dx1)
    if min_width > 0 and horiz_overlap / min_width < -0.5:
        return False

    return True


def _cluster_bbox(cluster: list[TextDetection]) -> tuple[int, int, int, int]:
    """Compute bounding box of a text cluster."""
    x1 = min(d.bbox_rect[0] for d in cluster)
    y1 = min(d.bbox_rect[1] for d in cluster)
    x2 = max(d.bbox_rect[2] for d in cluster)
    y2 = max(d.bbox_rect[3] for d in cluster)
    return (x1, y1, x2, y2)


def find_bubble_boundary(img_cv: np.ndarray,
                         cluster: list[TextDetection]) -> WebtoonBubble:
    """Find the bubble boundary around a text cluster.

    Three-level fallback:
      Level 3: Edge-based contour detection (clear-outlined bubbles)
      Level 2: Flood fill from text area (colored/outline-less bubbles)
      Level 1: Padded text cluster bbox (always works)
    """
    text_bbox = _cluster_bbox(cluster)
    combined = " ".join(d.text for d in cluster)
    h, w = img_cv.shape[:2]

    # Sample background color from band around text
    bg_color = _sample_background(img_cv, text_bbox)

    # Level 3: Try edge-based contour
    contour_result = _find_contour_boundary(img_cv, text_bbox)
    if contour_result is not None:
        bbox, mask = contour_result
        return WebtoonBubble(
            bbox=bbox,
            text_regions=cluster,
            combined_text=combined,
            has_bubble_boundary=True,
            bg_color=bg_color,
            bubble_mask=mask,
        )

    # Level 2: Try flood fill
    fill_result = _flood_fill_boundary(img_cv, text_bbox, bg_color)
    if fill_result is not None:
        bbox, mask = fill_result
        return WebtoonBubble(
            bbox=bbox,
            text_regions=cluster,
            combined_text=combined,
            has_bubble_boundary=True,
            bg_color=bg_color,
            bubble_mask=mask,
        )

    # Level 1: Padded bbox (always works)
    x1, y1, x2, y2 = text_bbox
    padded = (
        max(0, x1 - PAD_X),
        max(0, y1 - PAD_Y),
        min(w, x2 + PAD_X),
        min(h, y2 + PAD_Y),
    )
    return WebtoonBubble(
        bbox=padded,
        text_regions=cluster,
        combined_text=combined,
        has_bubble_boundary=False,
        bg_color=bg_color,
    )


def _sample_background(img_cv: np.ndarray,
                        text_bbox: tuple[int, int, int, int]) -> tuple[int, int, int]:
    """Sample the dominant background color from a band around the text area."""
    h, w = img_cv.shape[:2]
    x1, y1, x2, y2 = text_bbox
    band = 10

    # Collect pixels from a band around the text bbox
    regions = []
    # Top band
    if y1 - band >= 0:
        regions.append(img_cv[max(0, y1 - band):y1, x1:x2])
    # Bottom band
    if y2 + band <= h:
        regions.append(img_cv[y2:min(h, y2 + band), x1:x2])
    # Left band
    if x1 - band >= 0:
        regions.append(img_cv[y1:y2, max(0, x1 - band):x1])
    # Right band
    if x2 + band <= w:
        regions.append(img_cv[y1:y2, x2:min(w, x2 + band)])

    if not regions:
        return (255, 255, 255)

    pixels = np.concatenate([r.reshape(-1, 3) for r in regions if r.size > 0])
    if len(pixels) == 0:
        return (255, 255, 255)

    # Median color (robust to text strokes in the band)
    median = np.median(pixels, axis=0).astype(int)
    # Return as RGB (img_cv is BGR)
    return (int(median[2]), int(median[1]), int(median[0]))


def _find_contour_boundary(img_cv: np.ndarray,
                           text_bbox: tuple[int, int, int, int]
                           ) -> tuple[tuple[int, int, int, int], np.ndarray] | None:
    """Level 3: Find bubble contour using edge detection around text area.

    Works for bubbles with clear dark outlines (common in many webtoons).
    Constrained to avoid finding huge panel-spanning contours.
    """
    h, w = img_cv.shape[:2]
    x1, y1, x2, y2 = text_bbox
    tw, th = x2 - x1, y2 - y1
    # Search region: modest expansion around text (not half the bbox dimension)
    expand = CONTOUR_EXPAND + max(tw, th) // 4

    sx1 = max(0, x1 - expand)
    sy1 = max(0, y1 - expand)
    sx2 = min(w, x2 + expand)
    sy2 = min(h, y2 + expand)

    roi = img_cv[sy1:sy2, sx1:sx2]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if len(roi.shape) == 3 else roi

    edges = cv2.Canny(gray, 50, 150)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    edges = cv2.dilate(edges, kernel, iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    text_center = ((x1 + x2) // 2 - sx1, (y1 + y2) // 2 - sy1)
    text_area = tw * th
    best = None
    best_score = float("inf")

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < text_area * 0.8:
            continue
        if area > text_area * 4:  # Tighter cap: bubble shouldn't be >4x text area
            continue

        inside = cv2.pointPolygonTest(cnt, text_center, False)
        if inside < 0:
            continue

        score = area / text_area
        if score < best_score:
            best_score = score
            best = cnt

    if best is None:
        return None

    # Map contour back to full image coordinates
    best = best + np.array([sx1, sy1])
    bx, by, bw, bh = cv2.boundingRect(best)
    bbox = (bx, by, bx + bw, by + bh)

    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.drawContours(mask, [best], -1, 255, -1)

    return bbox, mask


def _flood_fill_boundary(img_cv: np.ndarray,
                         text_bbox: tuple[int, int, int, int],
                         bg_color: tuple[int, int, int]
                         ) -> tuple[tuple[int, int, int, int], np.ndarray] | None:
    """Level 2: Flood fill from text area using sampled background color.

    Works for bubbles without clear outlines but with uniform background color.
    Uses LAB color space for perceptually uniform distance.
    """
    h, w = img_cv.shape[:2]
    x1, y1, x2, y2 = text_bbox

    # Convert to LAB for perceptually uniform flood fill
    lab = cv2.cvtColor(img_cv, cv2.COLOR_BGR2LAB)

    # Seed point: center of text area
    seed_x = (x1 + x2) // 2
    seed_y = (y1 + y2) // 2
    seed_x = max(0, min(w - 1, seed_x))
    seed_y = max(0, min(h - 1, seed_y))

    tol = FLOOD_FILL_TOLERANCE
    mask = np.zeros((h + 2, w + 2), dtype=np.uint8)

    cv2.floodFill(
        lab.copy(), mask,
        (seed_x, seed_y),
        newVal=0,
        loDiff=(tol, tol, tol),
        upDiff=(tol, tol, tol),
        flags=cv2.FLOODFILL_MASK_ONLY | (255 << 8),
    )

    # Extract the inner mask (floodFill mask is padded by 1 on each side)
    fill_mask = mask[1:-1, 1:-1]

    # Check if filled area is reasonable
    fill_area = np.count_nonzero(fill_mask)
    text_area = (x2 - x1) * (y2 - y1)

    if fill_area < text_area * 0.8:   # Fill too small
        return None
    if fill_area > text_area * 15:    # Fill leaked into background
        return None

    # Bounding rect of filled area
    coords = cv2.findNonZero(fill_mask)
    if coords is None:
        return None

    fx, fy, fw, fh = cv2.boundingRect(coords)
    bbox = (fx, fy, fx + fw, fy + fh)

    return bbox, fill_mask


def detect_bubbles(img_cv: np.ndarray,
                   detections: list[TextDetection]) -> list[WebtoonBubble]:
    """Main entry point: cluster detections and find bubble boundaries.

    Args:
        img_cv: OpenCV BGR image.
        detections: EasyOCR text detections from ocr.detect_and_read().

    Returns:
        List of WebtoonBubble with text and boundary info.
    """
    clusters = cluster_detections(detections)
    bubbles = []

    for cluster in clusters:
        bubble = find_bubble_boundary(img_cv, cluster)
        log.info("  Bubble: %s texts, boundary=%s, bbox=%s",
                 len(cluster), bubble.has_bubble_boundary, bubble.bbox)
        bubbles.append(bubble)

    return bubbles
