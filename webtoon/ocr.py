"""Korean OCR using EasyOCR for webtoon text detection and reading."""

import logging
import threading
from dataclasses import dataclass

import numpy as np
from PIL import Image

from .config import (
    EASYOCR_CONFIDENCE_THRESHOLD,
    EASYOCR_GPU,
    EASYOCR_INVERTED_CONFIDENCE,
    EASYOCR_LOW_TEXT,
    EASYOCR_TEXT_THRESHOLD,
)

log = logging.getLogger(__name__)

# Singleton EasyOCR reader with thread-safe initialization
_reader = None
_init_lock = threading.Lock()


def _get_reader():
    """Lazy-load EasyOCR Korean reader on first use."""
    global _reader
    if _reader is None:
        with _init_lock:
            if _reader is None:
                import easyocr
                log.info("Loading EasyOCR (Korean, gpu=%s)...", EASYOCR_GPU)
                _reader = easyocr.Reader(["ko"], gpu=EASYOCR_GPU)
                log.info("EasyOCR loaded")
    return _reader


@dataclass
class TextDetection:
    """A single text detection from EasyOCR."""
    bbox_poly: list[list[int]]       # 4-point polygon [[x,y], ...]
    text: str
    confidence: float
    bbox_rect: tuple[int, int, int, int]  # (x1, y1, x2, y2)


def detect_and_read(img: Image.Image | np.ndarray) -> list[TextDetection]:
    """Run EasyOCR on an image, return filtered text detections.

    Three-pass approach:
    1. Run on the original RGB image (best for normal text)
    2. Run on a contrast-enhanced grayscale version (catches stylized
       text with colored outlines, gradients, drop shadows)
    3. Run on an inverted + CLAHE version (catches bright text on dark
       backgrounds — gold-outlined text, white text on black panels)

    Results are merged with IoU-based deduplication.

    Args:
        img: Pillow Image or numpy array (RGB or BGR — BGR is auto-converted).

    Returns:
        List of TextDetection with confidence above threshold.
    """
    import cv2

    if isinstance(img, Image.Image):
        img_array = np.array(img)
    else:
        img_array = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    # Pass 1: original image
    pass1 = _run_ocr(img_array)

    # Pass 2: contrast-enhanced grayscale
    enhanced = _enhance_for_ocr(img_array)
    pass2 = _run_ocr(enhanced)

    # Pass 3: inverted + CLAHE (bright text on dark backgrounds)
    inverted = _enhance_for_ocr_inverted(img_array)
    pass3 = _run_ocr(inverted, min_confidence=EASYOCR_INVERTED_CONFIDENCE)

    # Merge, dedup by IoU
    merged = _merge_detections(pass1, pass2)
    merged = _merge_detections(merged, pass3)

    log.debug("OCR: pass1=%d, pass2=%d, pass3=%d, merged=%d",
              len(pass1), len(pass2), len(pass3), len(merged))
    return merged


def _run_ocr(img_array: np.ndarray,
             min_confidence: float = EASYOCR_CONFIDENCE_THRESHOLD,
             ) -> list[TextDetection]:
    """Run EasyOCR on a numpy RGB or grayscale array."""
    reader = _get_reader()

    results = reader.readtext(
        img_array,
        text_threshold=EASYOCR_TEXT_THRESHOLD,
        low_text=EASYOCR_LOW_TEXT,
    )

    detections = []
    for bbox_poly, text, confidence in results:
        if confidence < min_confidence:
            continue
        if not text.strip():
            continue

        xs = [pt[0] for pt in bbox_poly]
        ys = [pt[1] for pt in bbox_poly]
        bbox_rect = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))

        detections.append(TextDetection(
            bbox_poly=[[int(x), int(y)] for x, y in bbox_poly],
            text=text.strip(),
            confidence=confidence,
            bbox_rect=bbox_rect,
        ))

    return detections


def _enhance_for_ocr(img_rgb: np.ndarray) -> np.ndarray:
    """Create a contrast-enhanced grayscale image for OCR pass 2.

    Converts to grayscale and applies CLAHE (Contrast Limited Adaptive
    Histogram Equalization) to make text strokes stand out against
    complex backgrounds — colored outlines, gradients, drop shadows.

    Tile size is adaptive: targets ~100px tiles so local contrast works
    on both small crops and tall full images (fixed 8x8 tiles on a 1600px
    image created 200px tiles, too coarse to catch styled text).
    """
    import cv2
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape[:2]
    tile_h = max(8, h // 100)
    tile_w = max(8, w // 100)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(tile_w, tile_h))
    return clahe.apply(gray)


def _enhance_for_ocr_inverted(img_rgb: np.ndarray) -> np.ndarray:
    """Create an inverted contrast-enhanced grayscale image for OCR pass 3.

    Targets bright text on dark backgrounds (gold-outlined Korean text,
    white text on black panels).  Inverts the grayscale image so bright
    text becomes dark strokes on a light background, then applies CLAHE.
    """
    import cv2
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
    inv = 255 - gray
    h, w = inv.shape[:2]
    tile_h = max(8, h // 100)
    tile_w = max(8, w // 100)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(tile_w, tile_h))
    return clahe.apply(inv)


def _iou(a: tuple[int, int, int, int],
         b: tuple[int, int, int, int]) -> float:
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


def _merge_detections(pass1: list[TextDetection],
                      pass2: list[TextDetection]) -> list[TextDetection]:
    """Merge two detection lists, deduplicating by IoU.

    Pass 1 (original image) detections are preferred when overlapping —
    UNLESS pass 2 has a significantly wider detection that subsumes the
    pass 1 detection (e.g., pass 1 caught the right half of a styled
    line while pass 2's CLAHE enhancement caught the full line).
    """
    merged = list(pass1)

    for det2 in pass2:
        overlap_idx = None
        for i, det1 in enumerate(merged):
            if _iou(det2.bbox_rect, det1.bbox_rect) > 0.3:
                overlap_idx = i
                break

        if overlap_idx is None:
            # No overlap — add pass 2 detection
            merged.append(det2)
        else:
            # Overlap found — check if pass 2 is wider and subsumes pass 1
            det1 = merged[overlap_idx]
            a1 = _bbox_area(det1.bbox_rect)
            a2 = _bbox_area(det2.bbox_rect)
            if a2 > a1 * 1.5:
                # Pass 2 covers significantly more area — replace
                merged[overlap_idx] = det2

    return merged


def _bbox_area(bbox: tuple[int, int, int, int]) -> int:
    """Area of a (x1, y1, x2, y2) rectangle."""
    return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])


def is_valid_korean(text: str) -> bool:
    """Check if OCR output contains meaningful Korean text.

    Same pattern as pipeline.ocr.is_valid_japanese(): counts content characters
    (Hangul syllables, Jamo) and requires >50% Korean among non-punctuation chars.
    Excludes CJK punctuation from the denominator.
    """
    stripped = text.strip()
    if not stripped or len(stripped) < 2:
        return False

    content_chars = 0
    other_chars = 0
    for ch in stripped:
        cp = ord(ch)
        if (0xAC00 <= cp <= 0xD7AF or     # Hangul Syllables (가-힣)
            0x1100 <= cp <= 0x11FF or     # Hangul Jamo
            0x3130 <= cp <= 0x318F):      # Hangul Compatibility Jamo (ㄱ-ㅎ, ㅏ-ㅣ)
            content_chars += 1
        elif (0x3000 <= cp <= 0x303F or    # CJK punctuation (、。「」…)
              0xFF01 <= cp <= 0xFF60 or    # Fullwidth forms (．！？，)
              cp in (0x2026, 0x2014, 0x2013) or  # Ellipsis, em/en dash
              ch in '.!?,;:…-~()[]{}"\' \t\n'):  # ASCII punctuation and whitespace
            pass  # Punctuation — don't count for or against
        else:
            other_chars += 1

    if content_chars < 2:
        return False

    meaningful = content_chars + other_chars
    if meaningful == 0:
        return False

    return content_chars / meaningful > 0.5
