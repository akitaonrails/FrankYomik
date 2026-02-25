"""Manga OCR wrapper using manga-ocr library."""

import logging
import os

from PIL import Image

log = logging.getLogger(__name__)

# Singleton manga-ocr instance
_mocr = None


def _get_ocr():
    """Lazy-load manga-ocr on first use. Runs on CPU to avoid VRAM conflicts."""
    global _mocr
    if _mocr is None:
        # Force CPU before importing manga_ocr (which imports torch)
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
        log.info("Loading manga-ocr model (CPU)...")
        from manga_ocr import MangaOcr
        _mocr = MangaOcr()
        log.info("manga-ocr loaded")
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
    """
    if not text or len(text.strip()) < 2:
        return False

    jp_chars = 0
    for ch in text:
        cp = ord(ch)
        # Hiragana, Katakana, CJK Unified Ideographs, common punctuation
        if (0x3040 <= cp <= 0x309F or   # Hiragana
            0x30A0 <= cp <= 0x30FF or   # Katakana
            0x4E00 <= cp <= 0x9FFF or   # CJK
            0x3000 <= cp <= 0x303F or   # CJK punctuation
            0xFF01 <= cp <= 0xFF60):    # Fullwidth forms
            jp_chars += 1

    ratio = jp_chars / len(text.strip())
    return ratio > 0.5
