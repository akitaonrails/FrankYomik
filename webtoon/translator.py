"""Korean to English translation using Ollama."""

import logging
import re

import requests

from .config import OLLAMA_BASE_URL, TRANSLATE_MODEL, TRANSLATE_OPTIONS, TRANSLATE_THINK
from pipeline.translator import _clean_response

log = logging.getLogger(__name__)


def translate(korean_text: str) -> str:
    """Translate Korean text to English using Ollama."""
    prompt = (
        "Translate this Korean manhwa/webtoon dialogue to natural English.\n"
        "Keep it concise and suitable for a speech bubble.\n"
        "Output ONLY the English translation, nothing else.\n"
        f"\nKorean: {korean_text}"
    )

    payload = {
        "model": TRANSLATE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": TRANSLATE_OPTIONS,
    }
    if TRANSLATE_THINK is not None:
        payload["think"] = TRANSLATE_THINK

    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        raw = resp.json().get("message", {}).get("content", "")
        result = _clean_response(raw)
        if result:
            return result
    except Exception as e:
        log.warning("Ollama translation failed: %s, trying fallback", e)

    return _fallback_translate(korean_text)


def translate_sfx(korean_text: str) -> str:
    """Translate a Korean sound effect to an English SFX word.

    Uses a specialized prompt that produces short uppercase onomatopoeia.
    Falls back to uppercased Google Translate result.
    """
    prompt = (
        "This is a Korean sound effect (SFX/onomatopoeia) from a webtoon comic.\n"
        "Translate it to a short English sound effect word.\n"
        "Examples: 꽈양→CRASH, 쾅→BOOM, 슈우→WHOOSH, 두근→THUMP, 콰광→KABOOM\n"
        "Output ONLY the English SFX word in uppercase, nothing else.\n"
        f"\nKorean SFX: {korean_text}"
    )

    payload = {
        "model": TRANSLATE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": TRANSLATE_OPTIONS,
    }
    if TRANSLATE_THINK is not None:
        payload["think"] = TRANSLATE_THINK

    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        raw = resp.json().get("message", {}).get("content", "")
        result = _clean_response(raw).strip().upper()
        # Strip prefixes the LLM sometimes adds (e.g. "SFX: SHUSH")
        result = re.sub(r'^(SFX\s*[:：]\s*)', '', result).strip()
        if result:
            return result
    except Exception as e:
        log.warning("Ollama SFX translation failed: %s, trying fallback", e)

    return _fallback_translate_sfx(korean_text)


def _fallback_translate_sfx(korean_text: str) -> str:
    """Fallback SFX translation using Google Translate, uppercased."""
    try:
        from deep_translator import GoogleTranslator
        result = GoogleTranslator(source="ko", target="en").translate(korean_text)
        return (result or korean_text).strip().upper()
    except Exception as e:
        log.error("Fallback SFX translation also failed: %s", e)
        return korean_text.upper()


def _fallback_translate(korean_text: str) -> str:
    """Fallback using deep-translator (Google Translate) for Korean."""
    try:
        from deep_translator import GoogleTranslator
        result = GoogleTranslator(source="ko", target="en").translate(korean_text)
        return result or korean_text
    except Exception as e:
        log.error("Fallback translation also failed: %s", e)
        return korean_text
