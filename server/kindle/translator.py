"""Japanese to English translation using Ollama."""

import logging
import re

import requests

from .config import OLLAMA_BASE_URL, TRANSLATE_MODEL, TRANSLATE_OPTIONS, TRANSLATE_THINK

log = logging.getLogger(__name__)


def translate(japanese_text: str) -> str:
    """Translate Japanese text to English using Ollama."""
    prompt = (
        "Translate this Japanese manga dialogue to natural English.\n"
        "Keep it concise and suitable for a speech bubble.\n"
        "Output ONLY the English translation, nothing else.\n"
        f"\nJapanese: {japanese_text}"
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

    return _fallback_translate(japanese_text)


def _clean_response(text: str) -> str:
    """Strip thinking tags and clean up translation output."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    # Remove quotes the model sometimes wraps around translations
    text = text.strip().strip('"').strip("'")
    return text.strip()


def _fallback_translate(japanese_text: str) -> str:
    """Fallback using deep-translator (Google Translate)."""
    try:
        from deep_translator import GoogleTranslator
        result = GoogleTranslator(source="ja", target="en").translate(japanese_text)
        return result or japanese_text
    except Exception as e:
        log.error("Fallback translation also failed: %s", e)
        return japanese_text
