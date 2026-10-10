"""Typed OCR results and deterministic Japanese crop routing."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from PIL import Image

from .document import OcrAlternative, Provenance


class OcrRoute(str, Enum):
    PRIMARY_ORDINARY = "manga_primary_ordinary"
    STYLIZED_OR_SFX = "hayai_stylized_or_sfx"
    MULTILINGUAL = "hayai_multilingual"
    NUMERIC = "hayai_numeric"
    PUNCTUATION = "hayai_punctuation"
    PRIMARY_INVALID = "hayai_primary_invalid"
    CROP_INCONSISTENT = "hayai_crop_inconsistent"
    HORIZONTAL = "hayai_horizontal"


@dataclass(frozen=True, slots=True)
class OcrQuality:
    valid_japanese: bool
    crop_consistent: bool | None
    crop_overlap: float | None
    meaningful_characters: int
    confidence: float | None


@dataclass(frozen=True, slots=True)
class OcrEngineResult:
    text: str
    quality: OcrQuality
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class RoutedOcrResult:
    text: str
    valid: bool
    confidence: float | None
    quality: OcrQuality
    provenance: Provenance
    alternatives: tuple[OcrAlternative, ...]
    routing_reason: str
    degraded: bool = False


_LATIN_RE = re.compile(r"[A-Za-z]")
_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
_DIGIT_RE = re.compile(r"[0-9\uff10-\uff19]")


def meaningful_character_count(text: str) -> int:
    return sum(character.isalnum() for character in text)


def choose_route(
    text: str,
    *,
    is_artwork: bool,
    valid_japanese: bool,
    crop_consistent: bool | None,
    horizontal: bool,
) -> OcrRoute:
    """Return one route using fixed precedence for overlapping signals."""
    stripped = text.strip()
    if is_artwork:
        return OcrRoute.STYLIZED_OR_SFX
    if _LATIN_RE.search(stripped) or _HANGUL_RE.search(stripped):
        return OcrRoute.MULTILINGUAL
    if _DIGIT_RE.search(stripped):
        return OcrRoute.NUMERIC
    if stripped and meaningful_character_count(stripped) == 0:
        return OcrRoute.PUNCTUATION
    if not valid_japanese:
        return OcrRoute.PRIMARY_INVALID
    if crop_consistent is False:
        return OcrRoute.CROP_INCONSISTENT
    if horizontal:
        return OcrRoute.HORIZONTAL
    return OcrRoute.PRIMARY_ORDINARY


def fallback_text_is_usable(
    text: str, route: OcrRoute, validate_japanese: Callable[[str], bool],
) -> bool:
    """Apply strict Japanese validation except where the route is script aware."""
    stripped = text.strip()
    if not stripped:
        return False
    if route in {OcrRoute.MULTILINGUAL, OcrRoute.NUMERIC}:
        return meaningful_character_count(stripped) > 0
    if route is OcrRoute.PUNCTUATION:
        return meaningful_character_count(stripped) == 0 and len(stripped) >= 2
    if route is OcrRoute.STYLIZED_OR_SFX:
        return validate_japanese(stripped) or meaningful_character_count(stripped) > 0
    return validate_japanese(stripped)


def route_japanese_crop(
    crop: Image.Image,
    primary: OcrEngineResult,
    *,
    is_artwork: bool,
    fallback_enabled: bool,
    fallback: Callable[[Image.Image], str],
    fallback_provenance: Provenance,
    validate_japanese: Callable[[str], bool],
    transient_errors: tuple[type[BaseException], ...],
    horizontal: bool = False,
) -> RoutedOcrResult:
    """Select the OCR engine and retain the non-selected successful reading."""
    route = choose_route(
        primary.text,
        is_artwork=is_artwork,
        valid_japanese=primary.quality.valid_japanese,
        crop_consistent=primary.quality.crop_consistent,
        horizontal=horizontal,
    )
    primary_valid = (
        primary.quality.valid_japanese
        and primary.quality.crop_consistent is not False
    )
    if route is OcrRoute.PRIMARY_ORDINARY or not fallback_enabled:
        reason = route.value if fallback_enabled else f"{route.value}:hayai_disabled"
        return RoutedOcrResult(
            primary.text, primary_valid, primary.quality.confidence,
            primary.quality, primary.provenance, (), reason,
        )

    try:
        fallback_text = fallback(crop).strip()
    except transient_errors as error:
        failure = (
            type(error).__name__.removeprefix("Hayai").removesuffix("Error").lower()
        )
        return RoutedOcrResult(
            primary.text, primary_valid, primary.quality.confidence,
            primary.quality, primary.provenance, (),
            f"{route.value}:hayai_{failure}_fallback", True,
        )

    fallback_valid = fallback_text_is_usable(
        fallback_text, route, validate_japanese,
    )
    fallback_quality = OcrQuality(
        valid_japanese=validate_japanese(fallback_text),
        crop_consistent=None,
        crop_overlap=None,
        meaningful_characters=meaningful_character_count(fallback_text),
        confidence=None,
    )
    fallback_result = OcrEngineResult(
        fallback_text, fallback_quality, fallback_provenance,
    )
    if fallback_valid:
        alternative = OcrAlternative(
            primary.text, primary.quality.confidence, primary.provenance,
        )
        return RoutedOcrResult(
            fallback_result.text, True, fallback_quality.confidence,
            fallback_quality, fallback_provenance, (alternative,), route.value,
        )

    alternative = OcrAlternative(
        fallback_result.text, fallback_quality.confidence, fallback_provenance,
    )
    return RoutedOcrResult(
        primary.text, primary_valid, primary.quality.confidence,
        primary.quality, primary.provenance, (alternative,),
        f"{route.value}:hayai_rejected",
    )
