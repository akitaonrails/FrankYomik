from unittest.mock import Mock

import pytest
from PIL import Image

from pipeline.document import Provenance
from pipeline.ocr import (
    OcrEngineResult,
    OcrQuality,
    OcrRoute,
    choose_route,
    route_japanese_crop,
)


MANGA = Provenance("manga-ocr", "manga-v1", "manga-model")
HAYAI = Provenance("hayai-ocr", "hayai-v2", "hayai-model")


def _primary(
    text: str, *, valid: bool = True, consistent: bool | None = True,
) -> OcrEngineResult:
    return OcrEngineResult(
        text,
        OcrQuality(valid, consistent, 0.8, len(text), 0.8),
        MANGA,
    )


@pytest.mark.parametrize(
    ("text", "artwork", "valid", "consistent", "expected"),
    [
        ("第2話ABC", True, False, False, OcrRoute.STYLIZED_OR_SFX),
        ("第2話ABC", False, False, False, OcrRoute.MULTILINGUAL),
        ("第2話", False, False, False, OcrRoute.NUMERIC),
        ("……", False, False, False, OcrRoute.PUNCTUATION),
        ("English", False, False, False, OcrRoute.MULTILINGUAL),
        ("", False, False, False, OcrRoute.PRIMARY_INVALID),
        ("短い台詞", False, True, False, OcrRoute.CROP_INCONSISTENT),
        ("短い台詞", False, True, True, OcrRoute.PRIMARY_ORDINARY),
        ("これは十分に長い普通の日本語台詞です", False, True, True,
         OcrRoute.PRIMARY_ORDINARY),
    ],
)
def test_route_precedence_is_pure_and_deterministic(
    text, artwork, valid, consistent, expected,
):
    assert choose_route(
        text,
        is_artwork=artwork,
        valid_japanese=valid,
        crop_consistent=consistent,
        horizontal=False,
    ) is expected


def test_explicit_horizontal_route_does_not_depend_on_short_text():
    assert choose_route(
        "これは十分に長い普通の日本語台詞です",
        is_artwork=False,
        valid_japanese=True,
        crop_consistent=True,
        horizontal=True,
    ) is OcrRoute.HORIZONTAL


@pytest.mark.parametrize(
    ("primary", "fallback"),
    [("第2話", "2026"), ("……", "?!"), ("日本ABC", "ABC 2026")],
)
def test_script_aware_hayai_result_can_win_strict_japanese_validation(
    primary, fallback,
):
    result = route_japanese_crop(
        Image.new("RGB", (20, 20)),
        _primary(primary, valid=False),
        is_artwork=False,
        fallback_enabled=True,
        fallback=lambda _crop: fallback,
        fallback_provenance=HAYAI,
        validate_japanese=lambda text: "日" in text,
        transient_errors=(TimeoutError,),
    )

    assert result.text == fallback
    assert result.valid is True
    assert result.provenance == HAYAI
    assert result.alternatives[0].provenance == MANGA


def test_ordinary_japanese_never_loads_hayai():
    fallback = Mock()
    result = route_japanese_crop(
        Image.new("RGB", (20, 20)),
        _primary("これは十分に長い普通の日本語台詞です"),
        is_artwork=False,
        fallback_enabled=True,
        fallback=fallback,
        fallback_provenance=HAYAI,
        validate_japanese=lambda _text: True,
        transient_errors=(TimeoutError,),
    )

    assert result.provenance == MANGA
    assert result.routing_reason == OcrRoute.PRIMARY_ORDINARY.value
    fallback.assert_not_called()


@pytest.mark.parametrize(
    ("failure_type", "failure_name"),
    [
        (TimeoutError, "timeout"),
        (MemoryError, "memory"),
        (ConnectionError, "connection"),
    ],
)
def test_expected_hayai_operational_failure_is_explicitly_degraded(
    failure_type, failure_name,
):
    def unavailable(_crop):
        raise failure_type("operational failure")

    result = route_japanese_crop(
        Image.new("RGB", (20, 20)),
        _primary("第2話"),
        is_artwork=False,
        fallback_enabled=True,
        fallback=unavailable,
        fallback_provenance=HAYAI,
        validate_japanese=lambda _text: True,
        transient_errors=(TimeoutError, MemoryError, ConnectionError),
    )

    assert result.degraded is True
    assert result.provenance == MANGA
    assert result.routing_reason == (
        f"hayai_numeric:hayai_{failure_name}_fallback"
    )


def test_unexpected_hayai_error_is_not_hidden():
    def broken(_crop):
        raise ValueError("bad protocol")

    with pytest.raises(ValueError, match="bad protocol"):
        route_japanese_crop(
            Image.new("RGB", (20, 20)),
            _primary("第2話"),
            is_artwork=False,
            fallback_enabled=True,
            fallback=broken,
            fallback_provenance=HAYAI,
            validate_japanese=lambda _text: True,
            transient_errors=(TimeoutError,),
        )
