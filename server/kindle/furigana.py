"""Kanji to furigana conversion using fugashi (MeCab).

Uses MeCab morphological analysis for context-aware readings.
Pykakasi was replaced because it lacks sentence context — e.g., it
reads standalone 人 as にん instead of ひと, 今年 as こんねん instead of
ことし.  MeCab correctly analyses sentence structure to pick the right
reading.
"""

import logging
import re
import threading

import fugashi

log = logging.getLogger(__name__)

_tagger = None
_init_lock = threading.Lock()
_convert_lock = threading.Lock()

# Katakana → hiragana offset (ア=0x30A1, あ=0x3041, diff=0x60)
_KATA_TO_HIRA = 0x60

# UniDic reading overrides for manga dialogue.
# UniDic returns formal/canonical readings that differ from natural
# manga speech — e.g. 私→ワタクシ (formal) instead of ワタシ (casual).
_READING_OVERRIDES = {
    "私": "ワタシ",
}


def _get_tagger() -> fugashi.Tagger:
    global _tagger
    if _tagger is None:
        with _init_lock:
            if _tagger is None:
                _tagger = fugashi.Tagger()
    return _tagger


def _has_kanji(text: str) -> bool:
    """Check if text contains any kanji characters."""
    return bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))


def _is_kana(ch: str) -> bool:
    """Check if a character is hiragana or katakana."""
    cp = ord(ch)
    return (0x3040 <= cp <= 0x309F or  # hiragana
            0x30A0 <= cp <= 0x30FF)    # katakana


def _kata_to_hira(text: str) -> str:
    """Convert katakana to hiragana."""
    result = []
    for ch in text:
        cp = ord(ch)
        if 0x30A1 <= cp <= 0x30F6:  # katakana range ァ-ヶ
            result.append(chr(cp - _KATA_TO_HIRA))
        else:
            result.append(ch)
    return "".join(result)


def _split_okurigana(surface: str, kana: str) -> list[dict]:
    """Split a morpheme into kanji-stem + okurigana suffix segments.

    Example: surface="食べる" kana="タベル"
      → [{"text":"食べ", "furigana":"たべ", "needs_furigana":True},
         {"text":"る", "furigana":None, "needs_furigana":False}]

    This matches pykakasi's splitting behaviour so the renderer can
    distribute furigana across only the kanji characters.
    """
    hira = _kata_to_hira(kana)

    # Find trailing kana in surface that matches trailing hiragana in reading
    suffix_len = 0
    s_len = len(surface)
    h_len = len(hira)
    while (suffix_len < s_len and suffix_len < h_len
           and _is_kana(surface[s_len - 1 - suffix_len])
           and _kata_to_hira(surface[s_len - 1 - suffix_len]) == hira[h_len - 1 - suffix_len]):
        suffix_len += 1

    if suffix_len == 0 or suffix_len == s_len:
        # No okurigana to split, or entirely kana
        return [{
            "text": surface,
            "furigana": hira if _has_kanji(surface) and hira != surface else None,
            "needs_furigana": _has_kanji(surface),
        }]

    # Split into kanji stem + kana suffix
    stem = surface[:s_len - suffix_len]
    stem_reading = hira[:h_len - suffix_len]
    suffix = surface[s_len - suffix_len:]

    segments = [{
        "text": stem,
        "furigana": stem_reading if _has_kanji(stem) and stem_reading != stem else None,
        "needs_furigana": _has_kanji(stem),
    }]
    if suffix:
        segments.append({
            "text": suffix,
            "furigana": None,
            "needs_furigana": False,
        })
    return segments


def annotate(text: str) -> list[dict]:
    """Convert Japanese text into annotated segments.

    Returns list of dicts:
        - text: original text segment
        - furigana: hiragana reading (or None if not needed)
        - needs_furigana: True if segment contains kanji
    """
    tagger = _get_tagger()
    with _convert_lock:
        words = tagger(text)

    segments = []
    for word in words:
        surface = word.surface
        kana = _READING_OVERRIDES.get(surface, word.feature.kana)

        if not surface.strip():
            continue

        if not kana or not _has_kanji(surface):
            # Pure kana or punctuation — no furigana needed
            segments.append({
                "text": surface,
                "furigana": None,
                "needs_furigana": False,
            })
        else:
            # Kanji-containing morpheme — split okurigana
            segments.extend(_split_okurigana(surface, kana))

    return segments
