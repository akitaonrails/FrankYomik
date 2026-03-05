"""Speech bubble and text detection using RT-DETR-v2.

Uses the ogkalu/comic-text-and-bubble-detector model which detects three classes:
  - bubble: speech/text bubble outlines
  - text_bubble: text that is inside bubbles
  - text_free: text outside bubbles (narration, SFX, signs)

Replaces the previous OpenCV contour + heuristic filter approach.
"""

import logging
import threading

import numpy as np
import torch
from PIL import Image

log = logging.getLogger(__name__)

MODEL_ID = "ogkalu/comic-text-and-bubble-detector"
DEFAULT_CONFIDENCE = 0.35
# Artwork text (SFX, narration) needs higher confidence — false positives
# draw over artwork and are more damaging than missed speech bubbles.
ARTWORK_TEXT_MIN_CONFIDENCE = 0.6

# Lazy-loaded singleton model
_model = None
_processor = None
_device = None
_init_lock = threading.Lock()


def _get_model():
    """Lazy-load RT-DETR-v2 model on first use."""
    global _model, _processor, _device
    if _model is None:
        with _init_lock:
            if _model is None:
                from transformers import (
                    RTDetrImageProcessor,
                    RTDetrV2ForObjectDetection,
                )

                log.info("Loading RT-DETR-v2 model: %s", MODEL_ID)
                _processor = RTDetrImageProcessor.from_pretrained(MODEL_ID)
                _model = RTDetrV2ForObjectDetection.from_pretrained(MODEL_ID)
                _device = "cuda" if torch.cuda.is_available() else "cpu"
                _model = _model.to(_device)
                _model.eval()
                log.info("RT-DETR-v2 loaded on %s", _device)
    return _model, _processor, _device


def detect_bubbles(img_cv: np.ndarray,
                   confidence: float = DEFAULT_CONFIDENCE) -> list[dict]:
    """Detect speech bubbles and text regions in a manga page.

    Returns list of dicts with keys:
      - bbox: (x1, y1, x2, y2) pixel coordinates
      - type: "speech_bubble" or "artwork_text"
      - score: detection confidence
      - is_artwork: True if text_free detection (narration/SFX outside bubbles)
    """
    model, processor, device = _get_model()

    # Convert OpenCV BGR to PIL RGB
    img_rgb = img_cv[:, :, ::-1]
    img_pil = Image.fromarray(img_rgb)

    inputs = processor(images=img_pil, return_tensors="pt").to(device)
    with torch.no_grad():
        outputs = model(**inputs)

    target_sizes = torch.tensor(
        [(img_pil.height, img_pil.width)], device=device
    )
    results = processor.post_process_object_detection(
        outputs, target_sizes=target_sizes, threshold=confidence
    )

    detections = []
    for score, label_id, box in zip(
        results[0]["scores"], results[0]["labels"], results[0]["boxes"]
    ):
        x1, y1, x2, y2 = box.tolist()
        label = model.config.id2label[label_id.item()]

        # Map RT-DETR classes to pipeline types:
        #   bubble / text_bubble → speech_bubble (text inside a bubble)
        #   text_free → artwork_text (narration, SFX, signs)
        if label in ("bubble", "text_bubble"):
            det_type = "speech_bubble"
            is_artwork = False
        else:
            det_type = "artwork_text"
            is_artwork = True

        # Artwork text needs higher confidence to avoid false positives
        # that draw rectangles over artwork.
        conf = score.item()
        if is_artwork and conf < ARTWORK_TEXT_MIN_CONFIDENCE:
            continue

        detections.append({
            "bbox": (int(x1), int(y1), int(x2), int(y2)),
            "type": det_type,
            "score": conf,
            "is_artwork": is_artwork,
        })

    # Deduplicate overlapping detections: when both "bubble" and "text_bubble"
    # fire on the same region, keep the one with higher confidence.
    detections = _deduplicate(detections)

    # Sort by reading order (right-to-left, top-to-bottom for manga)
    detections.sort(key=lambda d: (d["bbox"][1], d["bbox"][0]))

    log.info("Detected %d regions (%d bubbles, %d artwork text)",
             len(detections),
             sum(1 for d in detections if not d["is_artwork"]),
             sum(1 for d in detections if d["is_artwork"]))

    return detections


def _bbox_fallback_mask(h: int, w: int,
                        bbox: tuple[int, int, int, int]) -> np.ndarray:
    """Create a rectangular mask from the bbox as a last resort.

    RT-DETR-v2 detections are trusted — if contour extraction fails, the
    bbox itself is the mask.
    """
    import cv2
    x1, y1, x2, y2 = bbox
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.rectangle(mask, (x1, y1), (x2, y2), 255, -1)
    return mask


def extract_bubble_mask_manga(img_cv: np.ndarray,
                              bbox: tuple[int, int, int, int]) -> np.ndarray:
    """Extract the actual bubble contour mask from a known bbox region.

    Uses connected components analysis to find the largest bright region
    inside the bbox — more robust than flood fill when text is dense.

    Since RT-DETR-v2 detections are trusted, this always returns a mask —
    falling back to the bbox rectangle when contour extraction fails.

    Args:
        img_cv: OpenCV BGR image (full page).
        bbox: (x1, y1, x2, y2) bounding box of the detected bubble.

    Returns:
        Full-image-sized binary mask (uint8, 0/255). Never None.
    """
    import cv2

    h, w = img_cv.shape[:2]
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    if bw < 10 or bh < 10:
        return _bbox_fallback_mask(h, w, bbox)

    # Pad and clamp
    pad = 10
    px1 = max(0, x1 - pad)
    py1 = max(0, y1 - pad)
    px2 = min(w, x2 + pad)
    py2 = min(h, y2 + pad)

    roi = img_cv[py1:py2, px1:px2]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if len(roi.shape) == 3 else roi

    # Threshold: white/bright bubble interior
    _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)

    # Use connected components to find the largest bright region.
    # This is more robust than flood fill when text is dense and the
    # center pixel lands on a stroke.
    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        binary, connectivity=8)

    bbox_area = bw * bh
    center_x = (x1 + x2) // 2 - px1
    center_y = (y1 + y2) // 2 - py1

    # Find the best component: largest area that overlaps the bbox center region
    best_label = -1
    best_area = 0
    for i in range(1, n_labels):  # skip background (0)
        area = stats[i, cv2.CC_STAT_AREA]
        if area < bbox_area * 0.3:
            continue
        # Prefer the component containing the center
        if labels[center_y, center_x] == i:
            best_label = i
            best_area = area
            break
        if area > best_area:
            best_label = i
            best_area = area

    if best_label < 0 or best_area > bbox_area * 2.0:
        log.debug("No suitable component for bbox %s, using bbox fallback", bbox)
        return _bbox_fallback_mask(h, w, bbox)

    # Create mask from the selected component
    comp_mask = (labels == best_label).astype(np.uint8) * 255

    # Find contour of the component for smooth edges
    contours, _ = cv2.findContours(comp_mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return _bbox_fallback_mask(h, w, bbox)

    best_cnt = max(contours, key=cv2.contourArea)

    # Map contour back to full image coordinates
    best_cnt = best_cnt + np.array([px1, py1])

    # Create full-image-sized mask
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.drawContours(mask, [best_cnt], -1, 255, -1)

    return mask


def _overlap_ratio(a: tuple, b: tuple) -> float:
    """Fraction of bbox 'a' that overlaps with bbox 'b'."""
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    return inter / area_a if area_a > 0 else 0.0


def _deduplicate(detections: list[dict], iou_threshold: float = 0.5) -> list[dict]:
    """Remove overlapping detections, keeping higher confidence ones."""
    if len(detections) <= 1:
        return detections

    # Sort by confidence descending
    sorted_dets = sorted(detections, key=lambda d: d["score"], reverse=True)
    kept = []

    for det in sorted_dets:
        is_dup = False
        for existing in kept:
            overlap_ab = _overlap_ratio(det["bbox"], existing["bbox"])
            overlap_ba = _overlap_ratio(existing["bbox"], det["bbox"])
            if overlap_ab > iou_threshold or overlap_ba > iou_threshold:
                is_dup = True
                break
        if not is_dup:
            kept.append(det)

    return kept
