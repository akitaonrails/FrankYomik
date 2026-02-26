"""Manga OCR wrapper using manga-ocr library."""

import logging
import os
import threading

from PIL import Image

log = logging.getLogger(__name__)

# Singleton manga-ocr instance with thread-safe initialization
_mocr = None
_init_lock = threading.Lock()


def _get_ocr():
    """Lazy-load manga-ocr on first use. Device controlled by config.yaml."""
    global _mocr
    if _mocr is None:
        with _init_lock:
            if _mocr is None:
                from .config import MANGA_OCR_DEVICE
                force_cpu = MANGA_OCR_DEVICE != "cuda"
                if force_cpu:
                    os.environ["CUDA_VISIBLE_DEVICES"] = ""
                log.info("Loading manga-ocr model (%s)...", MANGA_OCR_DEVICE)
                from manga_ocr import MangaOcr
                _mocr = MangaOcr(force_cpu=force_cpu)
                log.info("manga-ocr loaded (%s)", MANGA_OCR_DEVICE)
    return _mocr


def extract_text(img: Image.Image) -> str:
    """Extract Japanese text from a cropped bubble image.

    Args:
        img: Pillow Image of a cropped speech bubble.

    Returns:
        Extracted Japanese text string.
    """
    ocr = _get_ocr()
    text = ocr(img)
    return text.strip()


def extract_text_from_region(full_img: Image.Image,
                             bbox: tuple[int, int, int, int]) -> str:
    """Crop a region from the full image and run OCR on it."""
    cropped = full_img.crop(bbox)
    return extract_text(cropped)


def is_valid_japanese(text: str) -> bool:
    """Check if OCR output looks like real Japanese dialogue.

    Returns False for gibberish / noise that manga-ocr produces from
    non-text regions (faces, clothing, backgrounds).

    Counts content characters (hiragana, katakana, CJK ideographs) and
    computes ratio against non-punctuation characters only. Japanese
    punctuation (。、「」．！？ etc.) is excluded from the denominator so
    common manga ellipsis (．．．) doesn't penalize the ratio.
    """
    stripped = text.strip()
    if not stripped or len(stripped) < 2:
        return False

    content_chars = 0
    other_chars = 0
    for ch in stripped:
        cp = ord(ch)
        if (0x3040 <= cp <= 0x309F or   # Hiragana
            0x30A0 <= cp <= 0x30FF or   # Katakana
            0x4E00 <= cp <= 0x9FFF):    # CJK Ideographs
            content_chars += 1
        elif (0x3000 <= cp <= 0x303F or  # CJK punctuation (、。「」…)
              0xFF01 <= cp <= 0xFF60):   # Fullwidth forms (．！？，)
            pass  # Japanese punctuation — don't count for or against
        else:
            other_chars += 1

    if content_chars < 2:
        return False

    meaningful = content_chars + other_chars
    if meaningful == 0:
        return False

    return content_chars / meaningful > 0.5
