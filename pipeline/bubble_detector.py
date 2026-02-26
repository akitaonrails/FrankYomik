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


def _is_color_page(img_cv: np.ndarray) -> bool:
    """Detect if a page is color (vs grayscale manga).

    Uses HSV saturation: color pages have >10% of bright pixels with
    saturation > 30.  Near-black pixels (value < 40) are excluded because
    they produce meaningless high saturation in HSV, causing grayscale
    manga scans to be misclassified as color.
    """
    hsv = cv2.cvtColor(img_cv, cv2.COLOR_BGR2HSV)
    bright_enough = hsv[:, :, 2] >= 40
    n_bright = bright_enough.sum()
    if n_bright == 0:
        return False
    pct_saturated = np.sum((hsv[:, :, 1] > 30) & bright_enough) / n_bright
    return pct_saturated > 0.10


def _try_split_merged(cnt, img_shape):
    """Split a contour if it represents two or more merged bubbles.

    Uses progressive erosion: if the mask splits into 2+ substantial
    components, the contour likely covers overlapping bubbles.  Watershed
    assigns each original pixel to the nearest component for clean cuts.

    Returns a list of split contours, or None if no split was found.
    """
    mask = np.zeros(img_shape[:2], dtype=np.uint8)
    cv2.drawContours(mask, [cnt], -1, 255, -1)

    erode_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    for iters in range(1, 9):
        eroded = cv2.erode(mask, erode_k, iterations=iters)
        n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(eroded)
        n_components = n_labels - 1
        if n_components == 0:
            break
        if n_components < 2:
            continue

        # Keep only substantial components (not tiny erosion remnants)
        valid_ids = [
            j for j in range(1, n_labels)
            if stats[j, cv2.CC_STAT_AREA] > 500
        ]
        if len(valid_ids) < 2:
            continue

        # Build markers for watershed
        markers = np.zeros(img_shape[:2], dtype=np.int32)
        for new_id, j in enumerate(valid_ids, start=1):
            markers[labels == j] = new_id

        mask_3ch = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
        markers = cv2.watershed(mask_3ch, markers)

        # Extract contour for each watershed region (clipped to original mask)
        split_contours = []
        for new_id in range(1, len(valid_ids) + 1):
            region_mask = np.zeros(img_shape[:2], dtype=np.uint8)
            region_mask[(markers == new_id) & (mask > 0)] = 255
            region_cnts, _ = cv2.findContours(
                region_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if region_cnts:
                largest = max(region_cnts, key=cv2.contourArea)
                if cv2.contourArea(largest) >= MIN_BUBBLE_AREA:
                    split_contours.append(largest)

        if len(split_contours) >= 2:
            return split_contours
        return None

    return None


def detect_bubbles(img_cv: np.ndarray) -> list[dict]:
    """Detect speech bubbles using OpenCV white-region contour analysis.

    Uses separate threshold profiles for grayscale vs color manga pages.

    Returns list of dicts with keys: bbox (x1,y1,x2,y2), type.
    """
    h, w = img_cv.shape[:2]
    page_area = h * w
    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
    is_color = _is_color_page(img_cv)

    # --- Threshold profiles ---
    if is_color:
        log.info("Color page detected — using relaxed thresholds")
        bright_level = 220       # colored bubbles aren't pure white
        min_bright_ratio = 0.50  # lower bar for colored backgrounds
        max_edge_density = 0.12
        max_mid_ratio = 0.40     # colored backgrounds have many mid-tones
        min_dark_ratio = 0.008
        max_component_ratio = 0.08
        min_very_bright_ratio = 0.20  # reject colored faces (skin not >240)
        use_rect_fallback = True  # bounding rect fallback for dark check
    else:
        log.info("Grayscale page detected — using standard thresholds")
        bright_level = 240
        min_bright_ratio = 0.65
        max_edge_density = 0.13  # slightly higher: furigana adds edge pixels
        max_mid_ratio = 0.15
        min_dark_ratio = 0.008
        max_component_ratio = 0.08
        min_very_bright_ratio = 0.0  # no extra check for grayscale
        use_rect_fallback = True

    # Edge map for texture analysis (computed once)
    edges = cv2.Canny(gray, 50, 150)

    # Threshold: speech bubbles are white (bright) regions
    _, thresh = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)

    # Gentle morphological cleanup
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    thresh = cv2.erode(thresh, kernel, iterations=1)
    thresh = cv2.dilate(thresh, kernel, iterations=1)

    # Bright pixel threshold for uniformity check
    _, bright_thresh = cv2.threshold(gray, bright_level, 255, cv2.THRESH_BINARY)
    # Very bright threshold (>240) for face rejection on color pages
    _, very_bright_thresh = cv2.threshold(gray, 240, 255, cv2.THRESH_BINARY)

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

        # 1. Edge density
        edge_pixels = cv2.countNonZero(cv2.bitwise_and(edges, edges, mask=mask))
        edge_density = edge_pixels / area
        if edge_density > max_edge_density:
            continue

        # 2. Bright pixel ratio
        bright_pixels = cv2.countNonZero(cv2.bitwise_and(bright_thresh, mask))
        bright_ratio = bright_pixels / area
        if bright_ratio < min_bright_ratio:
            continue

        # 2b. Very bright ratio (>240) — rejects colored faces/skin on color pages
        if min_very_bright_ratio > 0:
            vb_pixels = cv2.countNonZero(cv2.bitwise_and(very_bright_thresh, mask))
            vb_ratio = vb_pixels / area
            if vb_ratio < min_very_bright_ratio:
                continue

        # 3. Mid-tone ratio
        mid_mask = cv2.inRange(gray, 80, 220)
        mid_pixels = cv2.countNonZero(cv2.bitwise_and(mid_mask, mask))
        mid_ratio = mid_pixels / area
        if mid_ratio > max_mid_ratio:
            continue

        # 4. Contour circularity
        perimeter = cv2.arcLength(cnt, True)
        if perimeter > 0:
            circularity = 4 * np.pi * area / (perimeter * perimeter)
            if circularity < 0.15:
                continue

        # 5. Border darkness
        border_mask = np.zeros(gray.shape, dtype=np.uint8)
        cv2.drawContours(border_mask, [cnt], -1, 255, 3)
        border_only = cv2.subtract(border_mask, mask)
        border_pixels = cv2.countNonZero(border_only)
        if border_pixels > 0:
            border_mean = cv2.mean(gray, mask=border_only)[0]
            if border_mean > 160:
                continue

        # 6. Background uniformity
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
            # 7a. Very-dark pixel check (gray < 60)
            very_dark = np.sum((inner_mask > 0) & (gray < 60))
            dark_ratio_60 = very_dark / inner_area
            if dark_ratio_60 < min_dark_ratio:
                if use_rect_fallback and very_dark == 0 and bw * bh > 0:
                    # Rect fallback: contour may not encompass text
                    # strokes. Check bounding rect with band-pass filter
                    # (too low = no text, too high = surrounding art).
                    rect_roi = gray[y:y+bh, x:x+bw]
                    rect_dark = np.sum(rect_roi < 60) / rect_roi.size
                    if not (0.013 <= rect_dark <= 0.10):
                        continue
                    # Verify contour interior has some dark content
                    # (< 120).  Rejects empty white regions where only
                    # panel borders contribute dark pixels to the rect.
                    inner_dark = np.sum((inner_mask > 0) & (gray < 120))
                    if inner_dark == 0:
                        continue
                else:
                    continue

            # 7b. Largest dark component size
            dark_in_region = np.zeros(gray.shape, dtype=np.uint8)
            dark_in_region[(inner_mask > 0) & (gray < 120)] = 255
            dark_count = cv2.countNonZero(dark_in_region)
            if dark_count > 0:
                num_labels, _, stats, _ = cv2.connectedComponentsWithStats(
                    dark_in_region, connectivity=8)
                if num_labels > 1:
                    component_areas = stats[1:, cv2.CC_STAT_AREA]
                    largest = int(max(component_areas))
                    if largest > inner_area * max_component_ratio:
                        continue

        candidates.append({
            "bbox": (x, y, x + bw, y + bh),
            "type": "speech_bubble",
            "area": area,
            "contour": cnt,
        })

    # Try to split merged bubbles (two overlapping bubbles detected as one)
    split_candidates = []
    for c in candidates:
        split = _try_split_merged(c["contour"], gray.shape)
        if split:
            log.info("Split merged bubble at (%d,%d)-(%d,%d) into %d parts",
                     *c["bbox"], len(split))
            for sub_cnt in split:
                sx, sy, sw, sh = cv2.boundingRect(sub_cnt)
                split_candidates.append({
                    "bbox": (sx, sy, sx + sw, sy + sh),
                    "type": "speech_bubble",
                    "area": cv2.contourArea(sub_cnt),
                    "contour": sub_cnt,
                })
        else:
            split_candidates.append(c)
    candidates = split_candidates

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
