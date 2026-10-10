"""Korean to English translation using Ollama."""

import logging
import re

from kindle.translator import (
    LANG_MAP,
    _clean_response,
    _post_chat_content,
    verify_ollama_model,
)
from kindle.translator import translate_page as translate_page

from .config import TRANSLATE_MODEL, TRANSLATE_OPTIONS, TRANSLATE_THINK

log = logging.getLogger(__name__)


def translate(korean_text: str, target_lang: str = "en") -> str:
    """Translate Korean text to the target language using Ollama."""
    verify_ollama_model("webtoon")
    return _translate_verified(korean_text, target_lang)


def _translate_verified(korean_text: str, target_lang: str = "en") -> str:
    """Translate dialogue after the configured model has been verified."""
    _, lang_name = LANG_MAP.get(target_lang, ("en", "English"))
    prompt = (
        f"Translate this Korean manhwa/webtoon dialogue to natural {lang_name}.\n"
        "Keep it concise and suitable for a speech bubble.\n"
        f"Output ONLY the {lang_name} translation, nothing else.\n"
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

    raw = _post_chat_content(payload)
    result = _clean_response(raw)
    if not result:
        raise RuntimeError("Ollama returned an empty translation")
    return result


def translate_sfx(korean_text: str, target_lang: str = "en") -> str:
    """Translate a Korean sound effect to an SFX word in the target language.

    Uses a specialized prompt that produces short uppercase onomatopoeia.
    Verification, request, or empty-response failures propagate to the job.
    """
    verify_ollama_model("webtoon")
    return _translate_sfx_verified(korean_text, target_lang)


def _translate_sfx_verified(korean_text: str, target_lang: str = "en") -> str:
    """Translate SFX after the configured model has been verified."""
    _, lang_name = LANG_MAP.get(target_lang, ("en", "English"))
    prompt = (
        "This is a Korean sound effect (SFX/onomatopoeia) from a webtoon comic.\n"
        f"Translate it to a short {lang_name} sound effect word.\n"
        "Examples: 꽈양→CRASH, 쾅→BOOM, 슈우→WHOOSH, 두근→THUMP, 콰광→KABOOM\n"
        f"Output ONLY the {lang_name} SFX word in uppercase, nothing else.\n"
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

    raw = _post_chat_content(payload)
    result = _clean_response(raw).strip().upper()
    result = re.sub(r'^(SFX\s*[:：]\s*)', '', result).strip()
    if not result:
        raise RuntimeError("Ollama returned an empty SFX translation")
    return result
