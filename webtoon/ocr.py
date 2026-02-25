"""Korean OCR using EasyOCR for webtoon text detection and reading."""

import logging
import threading
from dataclasses import dataclass

import numpy as np
from PIL import Image

from .config import (
    EASYOCR_CONFIDENCE_THRESHOLD,
    EASYOCR_GPU,
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

    Args:
        img: Pillow Image or numpy array (RGB or BGR).

    Returns:
        List of TextDetection with confidence above threshold.
    """
    reader = _get_reader()

    if isinstance(img, Image.Image):
        img_array = np.array(img)
    else:
        img_array = img

    results = reader.readtext(
        img_array,
        text_threshold=EASYOCR_TEXT_THRESHOLD,
        low_text=EASYOCR_LOW_TEXT,
    )

    detections = []
    for bbox_poly, text, confidence in results:
        if confidence < EASYOCR_CONFIDENCE_THRESHOLD:
            continue
        if not text.strip():
            continue

        # Convert polygon to bounding rect
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
