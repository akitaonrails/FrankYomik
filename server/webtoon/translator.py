"""Korean to English translation using Ollama."""

import logging
import re

import requests

from kindle.translator import LANG_MAP, _clean_response, verify_ollama_model

from .config import OLLAMA_BASE_URL, TRANSLATE_MODEL, TRANSLATE_OPTIONS, TRANSLATE_THINK

log = logging.getLogger(__name__)


def translate(korean_text: str, target_lang: str = "en") -> str:
    """Translate Korean text to the target language using Ollama."""
    verify_ollama_model("webtoon")
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

    resp = requests.post(
        f"{OLLAMA_BASE_URL}/api/chat",
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    raw = resp.json().get("message", {}).get("content", "")
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

    resp = requests.post(
        f"{OLLAMA_BASE_URL}/api/chat",
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    raw = resp.json().get("message", {}).get("content", "")
    result = _clean_response(raw).strip().upper()
    result = re.sub(r'^(SFX\s*[:：]\s*)', '', result).strip()
    if not result:
        raise RuntimeError("Ollama returned an empty SFX translation")
    return result
