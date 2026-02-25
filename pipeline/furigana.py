"""Kanji to furigana conversion using pykakasi."""

import logging
import re
import threading

import pykakasi

log = logging.getLogger(__name__)

_kakasi = None
_init_lock = threading.Lock()
_convert_lock = threading.Lock()


def _get_kakasi():
    global _kakasi
    if _kakasi is None:
        with _init_lock:
            if _kakasi is None:
                _kakasi = pykakasi.kakasi()
    return _kakasi


def _has_kanji(text: str) -> bool:
    """Check if text contains any kanji characters."""
    return bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))


def annotate(text: str) -> list[dict]:
    """Convert Japanese text into annotated segments.

    Returns list of dicts:
        - text: original text segment
        - furigana: hiragana reading (or None if not needed)
        - needs_furigana: True if segment contains kanji
    """
    kakasi = _get_kakasi()
    with _convert_lock:
        result = kakasi.convert(text)

    segments = []
    for item in result:
        orig = item["orig"]
        hira = item["hira"]
        needs = _has_kanji(orig)

        # Only add furigana if the reading differs from the original
        # (pure kana segments would have identical orig and hira)
        segments.append({
            "text": orig,
            "furigana": hira if needs and hira != orig else None,
            "needs_furigana": needs,
        })

    return segments
