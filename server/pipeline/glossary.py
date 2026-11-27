"""Per-series furigana reading overrides and translation term glossaries."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pipeline.translation import MAX_FIELD_CHARS, MAX_TERMS

MAX_READING_OVERRIDES = 256
_ALLOWED_SERIES_KEYS = frozenset({"reading_overrides", "terms"})
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _normalize_title(value: str) -> str:
    return " ".join(value.strip().split()).casefold()


def _slugify_title(value: str) -> str:
    return _SLUG_RE.sub("-", value.strip().lower()).strip("-")


@dataclass(frozen=True)
class SeriesGlossary:
    """Optional reading overrides and translation terms for one series."""

    reading_overrides: tuple[tuple[str, str], ...] = ()
    terms: tuple[tuple[str, str], ...] = ()

    def reading_overrides_dict(self) -> dict[str, str]:
        return dict(self.reading_overrides)

    def matching_terms(self, texts: Sequence[str]) -> tuple[tuple[str, str], ...]:
        """Return glossary terms whose source appears in at least one text."""
        if not self.terms or not texts:
            return ()
        matched = [
            (source, target)
            for source, target in self.terms
            if any(source in text for text in texts)
        ]
        return tuple(matched[:MAX_TERMS])


EMPTY_SERIES_GLOSSARY = SeriesGlossary()


@dataclass(frozen=True)
class GlossaryCatalog:
    """Validated per-series glossary lookup table."""

    entries: tuple[tuple[str, SeriesGlossary], ...] = ()

    def for_series(self, title: str) -> SeriesGlossary:
        if not isinstance(title, str) or not title.strip() or not self.entries:
            return EMPTY_SERIES_GLOSSARY
        normalized = _normalize_title(title)
        slug = _slugify_title(title)
        for key, glossary in self.entries:
            if key == normalized or (slug and _slugify_title(key) == slug):
                return glossary
        return EMPTY_SERIES_GLOSSARY


EMPTY_GLOSSARY_CATALOG = GlossaryCatalog()


def _parse_string_pairs(
    raw: object, *, field_name: str, max_entries: int,
) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    if len(raw) > max_entries:
        raise ValueError(f"{field_name} exceeds maximum of {max_entries} entries")
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_key, raw_value in raw.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            raise ValueError(f"{field_name} keys and values must be strings")
        key = raw_key.strip()
        value = raw_value.strip()
        if not key or not value:
            raise ValueError(f"{field_name} keys and values must not be empty")
        if len(key) > MAX_FIELD_CHARS or len(value) > MAX_FIELD_CHARS:
            raise ValueError(f"{field_name} entry is too large")
        if key in seen:
            raise ValueError(f"duplicate {field_name} key: {key}")
        seen.add(key)
        pairs.append((key, value))
    return tuple(pairs)


def parse_glossaries(raw: object) -> GlossaryCatalog:
    """Validate and normalize the optional ``glossaries`` config section."""
    if raw is None:
        return EMPTY_GLOSSARY_CATALOG
    if not isinstance(raw, Mapping):
        raise ValueError("glossaries must be a mapping of series title to rules")
    entries: list[tuple[str, SeriesGlossary]] = []
    seen_keys: set[str] = set()
    for raw_series, raw_spec in raw.items():
        if not isinstance(raw_series, str) or not raw_series.strip():
            raise ValueError("glossary series key must be a non-empty string")
        normalized = _normalize_title(raw_series)
        if normalized in seen_keys:
            raise ValueError(f"duplicate glossary series key: {raw_series}")
        if not isinstance(raw_spec, Mapping):
            raise ValueError(f"glossary for {raw_series!r} must be a mapping")
        unknown = set(raw_spec) - _ALLOWED_SERIES_KEYS
        if unknown:
            raise ValueError(
                f"unsupported glossary keys for {raw_series!r}: "
                f"{', '.join(sorted(str(k) for k in unknown))}"
            )
        reading_overrides = _parse_string_pairs(
            raw_spec.get("reading_overrides"),
            field_name="reading_overrides",
            max_entries=MAX_READING_OVERRIDES,
        )
        terms = _parse_string_pairs(
            raw_spec.get("terms"),
            field_name="terms",
            max_entries=MAX_TERMS,
        )
        seen_keys.add(normalized)
        entries.append((
            normalized,
            SeriesGlossary(reading_overrides=reading_overrides, terms=terms),
        ))
    return GlossaryCatalog(tuple(entries))


def load_glossaries_from_yaml(raw_config: Mapping[str, object]) -> GlossaryCatalog:
    """Extract and validate ``glossaries`` from a loaded config.yaml dict."""
    if not isinstance(raw_config, Mapping):
        return EMPTY_GLOSSARY_CATALOG
    return parse_glossaries(raw_config.get("glossaries"))
