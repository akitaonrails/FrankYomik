"""Japanese to English translation using Ollama."""

import logging
import re

import requests

from pipeline.model_verification import (
    ollama_models_from_response,
    verify_ollama_tags,
)
from pipeline.revision import stage_identity

from .config import OLLAMA_BASE_URL, TRANSLATE_MODEL, TRANSLATE_OPTIONS, TRANSLATE_THINK

log = logging.getLogger(__name__)

LANG_MAP = {
    "en": ("en", "English"),
    "pt-br": ("pt", "Brazilian Portuguese"),
}


def verify_ollama_model(pipeline: str) -> None:
    """Verify the configured Ollama tag resolves to the pinned registry bytes."""
    identity = stage_identity(pipeline, "translation")["model"]
    response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10)
    response.raise_for_status()
    verify_ollama_tags(
        ollama_models_from_response(response.json()),
        TRANSLATE_MODEL,
        identity["id"],
        identity["weights_revision"],
    )


def translate(japanese_text: str, target_lang: str = "en") -> str:
    """Translate Japanese text to the target language using Ollama."""
    verify_ollama_model("manga_translate")
    _, lang_name = LANG_MAP.get(target_lang, ("en", "English"))
    prompt = (
        f"Translate this Japanese manga dialogue to natural, fluent {lang_name}.\n"
        "Guidelines:\n"
        "- Convey the MEANING and TONE, not a word-for-word literal translation.\n"
        f"- Use natural spoken {lang_name} that fits a manga speech bubble.\n"
        "- Keep Japanese names exactly as-is (e.g. Katsuki, Deku, Sensei).\n"
        "- Preserve Japanese nicknames and honorific-based names (e.g. Kacchan, "
        "Onee-chan) — do NOT translate or replace them.\n"
        "- Keep it concise — speech bubbles have limited space.\n"
        f"Output ONLY the {lang_name} translation, nothing else.\n"
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


def _clean_response(text: str) -> str:
    """Strip thinking tags and clean up translation output."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    # Remove quotes the model sometimes wraps around translations
    text = text.strip().strip('"').strip("'")
    return text.strip()
