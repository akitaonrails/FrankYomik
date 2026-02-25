"""Speech bubble detection using OpenCV contour analysis."""

import logging

import cv2
import numpy as np

from .config import MIN_BUBBLE_AREA, MAX_BUBBLE_AREA_RATIO

log = logging.getLogger(__name__)


def _overlap_ratio(a: tuple, b: tuple) -> float:
    """Compute fraction of bbox 'a' that overlaps with bbox 'b'."""
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    return inter / area_a if area_a > 0 else 0.0


def detect_bubbles(img_cv: np.ndarray) -> list[dict]:
    """Detect speech bubbles using OpenCV white-region contour analysis.

    Filters out false positives (faces, clothing) using:
    - Edge density: bubbles have few internal edges (text only), faces have many
    - Border darkness: speech bubbles have dark outlines
    - Brightness uniformity: bubble interiors are mostly pure white

    Returns list of dicts with keys: bbox (x1,y1,x2,y2), type.
    """
    h, w = img_cv.shape[:2]
    page_area = h * w
    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)

    # Edge map for texture analysis (computed once)
    edges = cv2.Canny(gray, 50, 150)

    # Threshold: speech bubbles are white (bright) regions
    _, thresh = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)

    # Gentle morphological cleanup
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    thresh = cv2.erode(thresh, kernel, iterations=1)
    thresh = cv2.dilate(thresh, kernel, iterations=1)

    # Bright pixel threshold for uniformity check
    _, bright_thresh = cv2.threshold(gray, 240, 255, cv2.THRESH_BINARY)

    # Use RETR_TREE to find nested contours (bubbles inside panels)
    contours, _ = cv2.findContours(thresh, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

    candidates = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < MIN_BUBBLE_AREA or area > page_area * MAX_BUBBLE_AREA_RATIO:
            continue

        x, y, bw, bh = cv2.boundingRect(cnt)
        aspect = max(bw, bh) / max(min(bw, bh), 1)
        if aspect > 4:
            continue

        # Convex hull solidity check
        hull = cv2.convexHull(cnt)
        hull_area = cv2.contourArea(hull)
        solidity = area / hull_area if hull_area > 0 else 0
        if solidity < 0.6:
            continue

        # Create interior mask
        mask = np.zeros(gray.shape, dtype=np.uint8)
        cv2.drawContours(mask, [cnt], -1, 255, -1)

        # Interior brightness check
        mean_val = cv2.mean(gray, mask=mask)[0]
        if mean_val < 180:
            continue

        # --- False positive filters ---

        # 1. Edge density: ratio of edge pixels inside the region
        #    Bubbles have sparse edges (just text strokes), faces have many
        edge_pixels = cv2.countNonZero(cv2.bitwise_and(edges, edges, mask=mask))
        edge_density = edge_pixels / area
        if edge_density > 0.10:
            continue

        # 2. Bright pixel ratio: fraction of very bright (>240) pixels
        #    Bubbles are mostly pure white, faces/skin have gradients
        bright_pixels = cv2.countNonZero(cv2.bitwise_and(bright_thresh, mask))
        bright_ratio = bright_pixels / area
        if bright_ratio < 0.65:
            continue

        # 3. Mid-tone ratio: faces have many mid-tone pixels (skin gradients),
        #    bubbles are bimodal (white background + black text, few mid-tones)
        mid_mask = cv2.inRange(gray, 80, 220)
        mid_pixels = cv2.countNonZero(cv2.bitwise_and(mid_mask, mask))
        mid_ratio = mid_pixels / area
        if mid_ratio > 0.15:
            continue

        # 4. Contour circularity: bubbles are round/elliptical,
        #    faces and clothing are irregular shapes
        perimeter = cv2.arcLength(cnt, True)
        if perimeter > 0:
            circularity = 4 * np.pi * area / (perimeter * perimeter)
            if circularity < 0.15:
                continue

        # 5. Border darkness: speech bubbles have dark outlines
        border_mask = np.zeros(gray.shape, dtype=np.uint8)
        cv2.drawContours(border_mask, [cnt], -1, 255, 3)
        border_only = cv2.subtract(border_mask, mask)
        border_pixels = cv2.countNonZero(border_only)
        if border_pixels > 0:
            border_mean = cv2.mean(gray, mask=border_only)[0]
            if border_mean > 160:
                continue

        # 6. Background uniformity: real bubble interiors are very uniform white.
        #    Faces have skin-tone gradients even in their brightest areas.
        white_pixels = gray[(mask > 0) & (gray > 200)]
        if len(white_pixels) > 50:
            white_std = float(np.std(white_pixels))
            if white_std > 15:
                continue

        # 7. Dark content analysis using eroded interior (excludes border).
        erode_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        inner_mask = cv2.erode(mask, erode_k, iterations=1)
        inner_area = cv2.countNonZero(inner_mask)
        if inner_area > 100:
            # 7a. Very-dark pixel check (gray < 60): text strokes are
            #     near-black. Face art features (hair anti-aliasing, skin
            #     shadows) are moderately dark (60-120) and don't count.
            very_dark = np.sum((inner_mask > 0) & (gray < 60))
            if very_dark / inner_area < 0.008:
                continue

            # 7b. Largest dark component size: text = many small strokes,
            #     face hair/eyes = fewer large blobs.  Uses wider threshold
            #     (gray < 120) for component connectivity.
            dark_in_region = np.zeros(gray.shape, dtype=np.uint8)
            dark_in_region[(inner_mask > 0) & (gray < 120)] = 255
            dark_count = cv2.countNonZero(dark_in_region)
            if dark_count > 0:
                num_labels, _, stats, _ = cv2.connectedComponentsWithStats(
                    dark_in_region, connectivity=8)
                if num_labels > 1:
                    component_areas = stats[1:, cv2.CC_STAT_AREA]
                    largest = int(max(component_areas))
                    if largest > inner_area * 0.08:
                        continue

        candidates.append({
            "bbox": (x, y, x + bw, y + bh),
            "type": "speech_bubble",
            "area": area,
        })

    # Remove overlapping detections: keep smaller (more specific) ones
    candidates.sort(key=lambda b: b["area"])
    filtered = []
    for c in candidates:
        is_dup = False
        for f in filtered:
            if (_overlap_ratio(c["bbox"], f["bbox"]) > 0.5 or
                    _overlap_ratio(f["bbox"], c["bbox"]) > 0.5):
                is_dup = True
                break
        if not is_dup:
            filtered.append(c)

    # Clean up internal keys
    for b in filtered:
        del b["area"]

    # Sort by position
    filtered.sort(key=lambda b: (b["bbox"][0], b["bbox"][1]))

    log.info("Detected %d bubbles", len(filtered))
    return filtered
