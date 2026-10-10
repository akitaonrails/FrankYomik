"""Typed page translation contract and bounded recovery policy."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

PAGE_TRANSLATION_SCHEMA_VERSION = 1
PAGE_TRANSLATION_PROMPT_REVISION = "page-translation-v1"
MAX_TRANSLATION_ITEMS = 64
MAX_PAGE_TRANSLATION_ITEMS = 512
MAX_CONTEXT_ITEMS = 4
MAX_CONTEXT_CHARS = 4_000
MAX_TERMS = 64
MAX_STYLE_RULES = 16
MAX_FIELD_CHARS = 2_000
MAX_BATCH_ATTEMPTS = 8
MAX_RESPONSE_CHARS = 256_000
LANGUAGE_NAMES = {
    "ja": "Japanese",
    "ko": "Korean",
    "en": "English",
    "pt-br": "Brazilian Portuguese",
}
MANGA_PAGE_STYLE = (
    "Natural spoken dialogue suitable for a manga speech balloon.",
    "Preserve Japanese names, nicknames, and honorific-based names.",
    "Keep each translation concise enough for its source region.",
)
WEBTOON_PAGE_STYLE = (
    "Natural spoken dialogue suitable for a manhwa or webtoon balloon.",
    "Keep each translation concise enough for its source region.",
    "Use a short uppercase sound word for SFX items.",
)


class TranslationContractError(ValueError):
    """The provider response did not satisfy the page translation schema."""


class TranslationContextLimit(RuntimeError):
    """The provider refused a request because its context was too large."""


@dataclass(frozen=True)
class TranslationItem:
    id: str
    reading_order: int
    kind: str
    source_text: str

    def __post_init__(self) -> None:
        if not self.id or len(self.id) > 256:
            raise ValueError("translation item id must be 1..256 characters")
        if self.reading_order < 0:
            raise ValueError("translation reading order must be non-negative")
        if not self.kind or len(self.kind) > 64:
            raise ValueError("translation kind must be 1..64 characters")
        if not self.source_text.strip():
            raise ValueError("translation source text must not be empty")
        if len(self.source_text) > MAX_FIELD_CHARS:
            raise ValueError("translation source text is too large")


@dataclass(frozen=True)
class PageTranslationRequest:
    source_language: str
    target_language: str
    items: tuple[TranslationItem, ...]
    context: tuple[str, ...] = ()
    terms: tuple[tuple[str, str], ...] = ()
    style: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.source_language not in {"ja", "ko"}:
            raise ValueError("unsupported translation source language")
        if self.target_language not in {"en", "pt-br"}:
            raise ValueError("unsupported translation target language")
        if not 0 < len(self.items) <= MAX_PAGE_TRANSLATION_ITEMS:
            raise ValueError("page translation requires 1..512 items")
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("page translation item ids must be unique")
        orders = [item.reading_order for item in self.items]
        if orders != sorted(orders):
            raise ValueError("page translation items must be in reading order")
        if len(self.context) > MAX_CONTEXT_ITEMS:
            raise ValueError("too many translation context entries")
        if sum(len(item) for item in self.context) > MAX_CONTEXT_CHARS:
            raise ValueError("translation context is too large")
        if len(self.terms) > MAX_TERMS:
            raise ValueError("too many translation terms")
        if len(self.style) > MAX_STYLE_RULES:
            raise ValueError("too many translation style rules")
        if any(not source or not target for source, target in self.terms):
            raise ValueError("translation terms must not be empty")
        if len({source for source, _ in self.terms}) != len(self.terms):
            raise ValueError("translation term sources must be unique")
        bounded = (*self.context, *(v for pair in self.terms for v in pair),
                   *self.style)
        if any(len(value) > MAX_FIELD_CHARS for value in bounded):
            raise ValueError("translation request field is too large")

    def with_items(
        self, items: Sequence[TranslationItem],
    ) -> PageTranslationRequest:
        return replace(self, items=tuple(items))


@dataclass(frozen=True)
class TranslationValue:
    id: str
    text: str


@dataclass(frozen=True)
class PageTranslationResult:
    values: tuple[TranslationValue, ...]
    batch_attempts: int
    fallback_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        ids = [value.id for value in self.values]
        if len(ids) != len(set(ids)):
            raise ValueError("page translation result ids must be unique")
        if any(not value.text.strip() for value in self.values):
            raise ValueError("page translation result text must not be empty")

    def by_id(self) -> dict[str, str]:
        return {value.id: value.text for value in self.values}


def response_json_schema(expected_ids: Sequence[str]) -> dict[str, object]:
    """Return the exact Ollama structured-output schema for one request."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "translations"],
        "properties": {
            "schema_version": {"const": PAGE_TRANSLATION_SCHEMA_VERSION},
            "translations": {
                "type": "object",
                "additionalProperties": False,
                "required": list(expected_ids),
                "properties": {
                    item_id: {"type": "string", "minLength": 1}
                    for item_id in expected_ids
                },
            },
        },
    }


def build_page_prompt(request: PageTranslationRequest) -> str:
    """Serialize a deterministic, data-only page request for the model."""
    data = {
        "schema_version": PAGE_TRANSLATION_SCHEMA_VERSION,
        "source_language": request.source_language,
        "source_language_name": LANGUAGE_NAMES[request.source_language],
        "target_language": request.target_language,
        "target_language_name": LANGUAGE_NAMES[request.target_language],
        "context": list(request.context),
        "terms": [
            {"source": source, "target": target}
            for source, target in request.terms
        ],
        "style": list(request.style),
        "items": [
            {
                "id": item.id,
                "reading_order": item.reading_order,
                "kind": item.kind,
                "source": item.source_text,
            }
            for item in request.items
        ],
    }
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (
        f"Page translation protocol {PAGE_TRANSLATION_PROMPT_REVISION}.\n"
        "Translate every item into target_language. Use context only to resolve "
        "continuity, names, and pronouns. Apply terms exactly. Keep dialogue "
        "concise. Render SFX items as short uppercase onomatopoeia. Return only "
        "the schema-valid JSON object keyed by the submitted IDs.\n"
        f"REQUEST={serialized}"
    )


def parse_page_response(
    raw: str, expected_ids: Sequence[str], *, allow_missing: bool = True,
) -> dict[str, str]:
    """Parse without losing duplicate JSON keys, then enforce exact IDs."""
    if not isinstance(raw, str) or len(raw) > MAX_RESPONSE_CHARS:
        raise TranslationContractError("translation response is too large")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise TranslationContractError(f"duplicate response key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(raw, object_pairs_hook=reject_duplicates)
    except TranslationContractError:
        raise
    except (TypeError, json.JSONDecodeError) as error:
        raise TranslationContractError("malformed translation JSON") from error
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "translations",
    }:
        raise TranslationContractError("invalid translation response envelope")
    if (type(payload["schema_version"]) is not int
            or payload["schema_version"] != PAGE_TRANSLATION_SCHEMA_VERSION):
        raise TranslationContractError("unsupported translation schema version")
    translations = payload["translations"]
    if not isinstance(translations, dict):
        raise TranslationContractError("translations must be keyed by ID")
    expected = set(expected_ids)
    unknown = set(translations) - expected
    if unknown:
        raise TranslationContractError("translation response has unknown IDs")
    missing = expected - set(translations)
    if missing and not allow_missing:
        raise TranslationContractError("translation response has missing IDs")
    for item_id, text in translations.items():
        if not isinstance(text, str) or not text.strip():
            raise TranslationContractError(
                f"translation for {item_id} must be non-empty text"
            )
    return {item_id: translations[item_id].strip()
            for item_id in expected_ids if item_id in translations}


def translate_with_recovery(
    request: PageTranslationRequest,
    provider: Callable[[PageTranslationRequest], str],
    single_translate: Callable[[TranslationItem, str], str],
    *,
    recoverable_errors: tuple[type[BaseException], ...] = (TimeoutError,),
    max_batch_attempts: int = MAX_BATCH_ATTEMPTS,
) -> PageTranslationResult:
    """Translate atomically with bounded batch splitting and single fallback."""
    if max_batch_attempts < 1:
        raise ValueError("max_batch_attempts must be positive")
    translated: dict[str, str] = {}
    fallback_ids: list[str] = []
    attempts = 0

    def fallback(items: Sequence[TranslationItem]) -> None:
        for item in items:
            text = single_translate(item, request.target_language)
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError(
                    f"single translation returned no text for {item.id}"
                )
            translated[item.id] = text.strip()
            fallback_ids.append(item.id)

    def resolve(items: tuple[TranslationItem, ...]) -> None:
        nonlocal attempts
        if not items:
            return
        if attempts >= max_batch_attempts:
            fallback(items)
            return
        attempts += 1
        try:
            raw = provider(request.with_items(items))
            parsed = parse_page_response(raw, [item.id for item in items])
        except (TranslationContractError, TranslationContextLimit) + recoverable_errors:
            if len(items) == 1:
                fallback(items)
                return
            midpoint = len(items) // 2
            resolve(items[:midpoint])
            resolve(items[midpoint:])
            return
        translated.update(parsed)
        missing = tuple(item for item in items if item.id not in parsed)
        if missing:
            resolve(missing)

    resolve(request.items)
    values = tuple(
        TranslationValue(item.id, translated[item.id]) for item in request.items
    )
    return PageTranslationResult(values, attempts, tuple(fallback_ids))
