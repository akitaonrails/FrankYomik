"""Japanese to English translation using Ollama."""

import logging
import json
import re

import requests

from pipeline.model_verification import (
    ollama_models_from_response,
    verify_ollama_tags,
)
from pipeline.revision import stage_identity
from pipeline.translation import (
    PageTranslationRequest,
    PageTranslationResult,
    MAX_TRANSLATION_ITEMS,
    TranslationValue,
    TranslationContextLimit,
    TranslationContractError,
    TranslationItem,
    build_page_prompt,
    response_json_schema,
    translate_with_recovery,
)

from .config import OLLAMA_BASE_URL, TRANSLATE_MODEL, TRANSLATE_OPTIONS, TRANSLATE_THINK

log = logging.getLogger(__name__)

MAX_HTTP_RESPONSE_BYTES = 512_000
MAX_HTTP_RESPONSE_CHARS = 256_000

LANG_MAP = {
    "en": ("en", "English"),
    "pt-br": ("pt", "Brazilian Portuguese"),
}

def verify_ollama_model(pipeline: str) -> None:
    """Verify the configured Ollama tag resolves to the pinned registry bytes."""
    identity = stage_identity(pipeline, "translation")["model"]
    response = requests.get(
        f"{OLLAMA_BASE_URL}/api/tags", timeout=10, stream=True,
    )
    try:
        body = _bounded_response_text(response)
        response.raise_for_status()
    finally:
        response.close()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise TranslationContractError("malformed Ollama tags response") from error
    if not isinstance(payload, dict):
        raise TranslationContractError("invalid Ollama tags response")
    verify_ollama_tags(
        ollama_models_from_response(payload),
        TRANSLATE_MODEL,
        identity["id"],
        identity["weights_revision"],
    )


def translate(japanese_text: str, target_lang: str = "en") -> str:
    """Translate Japanese text to the target language using Ollama."""
    verify_ollama_model("manga_translate")
    return _translate_verified(japanese_text, target_lang)


def _translate_verified(japanese_text: str, target_lang: str = "en") -> str:
    """Translate after the caller has verified the configured model once."""
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

    raw = _post_chat_content(payload)
    result = _clean_response(raw)
    if not result:
        raise RuntimeError("Ollama returned an empty translation")
    return result


def _bounded_response_text(response: requests.Response) -> str:
    """Read a streamed provider envelope without buffering unbounded bytes."""
    body = bytearray()
    for chunk in response.iter_content(chunk_size=8_192):
        if not chunk:
            continue
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        body.extend(chunk)
        if len(body) > MAX_HTTP_RESPONSE_BYTES:
            raise TranslationContractError("Ollama response body is too large")
    try:
        text = bytes(body).decode("utf-8")
    except UnicodeDecodeError as error:
        raise TranslationContractError("Ollama response is not UTF-8") from error
    if len(text) > MAX_HTTP_RESPONSE_CHARS:
        raise TranslationContractError("Ollama response text is too large")
    return text


def _post_chat_content(payload: dict[str, object]) -> str:
    """Post one verified chat request and return its bounded content field."""
    response = requests.post(
        f"{OLLAMA_BASE_URL}/api/chat", json=payload, timeout=120, stream=True,
    )
    try:
        body = _bounded_response_text(response)
        if response.status_code in {400, 413} and any(
            token in body.lower() for token in (
                "context", "token limit", "too large", "too long",
            )
        ):
            raise TranslationContextLimit("Ollama context limit exceeded")
        response.raise_for_status()
    finally:
        response.close()
    try:
        envelope = json.loads(body)
    except json.JSONDecodeError as error:
        raise TranslationContractError("malformed Ollama response") from error
    if not isinstance(envelope, dict):
        raise TranslationContractError("invalid Ollama response envelope")
    message = envelope.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise TranslationContractError("invalid Ollama response content")
    return message["content"]


def translate_page(request: PageTranslationRequest) -> PageTranslationResult:
    """Translate one page with strict structured output and bounded recovery."""
    pipeline = "webtoon" if request.source_language == "ko" else "manga_translate"
    verify_ollama_model(pipeline)

    def provider(batch: PageTranslationRequest) -> str:
        payload = {
            "model": TRANSLATE_MODEL,
            "messages": [{"role": "user", "content": build_page_prompt(batch)}],
            "format": response_json_schema([item.id for item in batch.items]),
            "stream": False,
            "options": TRANSLATE_OPTIONS,
        }
        if TRANSLATE_THINK is not None:
            payload["think"] = TRANSLATE_THINK
        raw = _post_chat_content(payload)
        return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    def single(item: TranslationItem, target_lang: str) -> str:
        if request.source_language == "ko":
            from webtoon.translator import (
                _translate_sfx_verified,
                _translate_verified as translate_korean_verified,
            )

            if item.kind == "sfx":
                return _translate_sfx_verified(item.source_text, target_lang)
            return translate_korean_verified(item.source_text, target_lang)
        return _translate_verified(item.source_text, target_lang)

    values: list[TranslationValue] = []
    fallback_ids: list[str] = []
    attempts = 0
    for start in range(0, len(request.items), MAX_TRANSLATION_ITEMS):
        batch = request.with_items(
            request.items[start:start + MAX_TRANSLATION_ITEMS],
        )
        result = translate_with_recovery(
            batch, provider, single, recoverable_errors=(requests.Timeout,),
        )
        values.extend(result.values)
        fallback_ids.extend(result.fallback_ids)
        attempts += result.batch_attempts
    return PageTranslationResult(
        tuple(values), attempts, tuple(fallback_ids),
    )


def _clean_response(text: str) -> str:
    """Strip thinking tags and clean up translation output."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    # Remove quotes the model sometimes wraps around translations
    text = text.strip().strip('"').strip("'")
    return text.strip()
