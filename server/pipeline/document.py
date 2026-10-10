"""Normalized, deterministic page document shared by pipeline stages."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from .revision import validate_pipeline_revision

PAGE_DOCUMENT_SCHEMA_VERSION = 2
VALID_PIPELINES = frozenset({
    "manga_translate",
    "manga_furigana",
    "book_furigana",
    "webtoon",
})
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_OBJECT_REFERENCE_RE = re.compile(r"^objects/([a-f0-9]{64})$")
_LEGACY_MASK_REFERENCE_RE = re.compile(r"^derived://source/[A-Za-z0-9._:-]+$")
_JSON_NUMBER_RE = re.compile(
    r"^(?P<sign>-?)(?P<int>0|[1-9][0-9]*)(?:\.(?P<frac>[0-9]+))?"
    r"(?:[eE](?P<exp>[+-]?[0-9]+))?$"
)


def _canonical_number(value: float | Decimal) -> str:
    """Normalize a finite JSON number without retaining lexical spelling."""
    if isinstance(value, bool):
        raise TypeError("boolean is not a JSON number")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        raw = repr(value)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("JSON numbers must be finite")
        binary = float(value)
        if not math.isfinite(binary):
            raise ValueError("JSON number exceeds binary64 range")
        if binary == 0.0 and not value.is_zero():
            raise ValueError("JSON number is below binary64 range")
        raw = repr(binary)
    elif isinstance(value, int):
        raw = str(value)
    else:
        raise TypeError("unsupported JSON number")

    match = _JSON_NUMBER_RE.fullmatch(raw)
    if match is None:
        raise ValueError(f"invalid JSON number: {raw}")
    sign = match.group("sign")
    fraction = match.group("frac") or ""
    digits = (match.group("int") + fraction).lstrip("0")
    if not digits:
        return "0"
    exponent = int(match.group("exp") or "0") - len(fraction)
    while digits.endswith("0"):
        digits = digits[:-1]
        exponent += 1

    decimal_point = len(digits) + exponent
    if decimal_point > 21 or decimal_point <= -6:
        coefficient = digits[0]
        if len(digits) > 1:
            coefficient += "." + digits[1:]
        rendered = f"{coefficient}e{decimal_point - 1}"
    elif decimal_point <= 0:
        rendered = "0." + ("0" * -decimal_point) + digits
    elif decimal_point >= len(digits):
        rendered = digits + ("0" * (decimal_point - len(digits)))
    else:
        rendered = digits[:decimal_point] + "." + digits[decimal_point:]
    return sign + rendered


def canonical_json(value: object) -> str:
    """Serialize JSON with the cross-runtime PageDocument normalization."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float, Decimal)):
        return _canonical_number(value)
    if isinstance(value, str):
        value.encode("utf-8")  # Reject unpaired surrogates.
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return "{" + ",".join(
            f"{canonical_json(key)}:{canonical_json(value[key])}"
            for key in sorted(value)
        ) + "}"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    raise TypeError(f"value of type {type(value).__name__} is not JSON serializable")


def canonical_json_bytes(value: object) -> bytes:
    return canonical_json(value).encode("utf-8")


def _reject_unknown_fields(
    value: Mapping[str, object], allowed: frozenset[str], name: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{name} contains unknown field: {unknown[0]}")


def _default_if_null(
    value: Mapping[str, object], key: str, default: object,
) -> object:
    """Treat a missing or explicit-null defaulted member identically."""
    item = value.get(key)
    return default if item is None else item


def _require_non_empty(name: str, value: object) -> str:
    value = _require_string(name, value)
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


def _require_string(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _optional_string(name: str, value: object) -> str | None:
    if value is None:
        return None
    return _require_string(name, value)


def _require_integer(name: str, value: object) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    return value


def _optional_integer(name: str, value: object) -> int | None:
    if value is None:
        return None
    return _require_integer(name, value)


def _optional_number(name: str, value: object) -> float | Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError(f"{name} must be a number")
    if ((isinstance(value, Decimal) and not value.is_finite())
            or (not isinstance(value, Decimal) and not math.isfinite(value))):
        raise ValueError(f"{name} must be finite")
    if isinstance(value, Decimal):
        binary = float(value)
        if not math.isfinite(binary):
            raise ValueError(f"{name} exceeds binary64 range")
        if binary == 0.0 and not value.is_zero():
            raise ValueError(f"{name} is below binary64 range")
        return binary
    return value


def _require_boolean(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a boolean")
    return value


def _validate_confidence(
    value: float | Decimal | None, name: str = "confidence",
) -> None:
    value = _optional_number(name, value)
    if value is not None and not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """Half-open pixel bounds: left/top inclusive, right/bottom exclusive."""

    x1: int
    y1: int
    x2: int
    y2: int

    def __post_init__(self) -> None:
        for name, coordinate in zip(
            ("x1", "y1", "x2", "y2"),
            (self.x1, self.y1, self.x2, self.y2),
            strict=True,
        ):
            _require_integer(f"bounding box {name}", coordinate)
        if min(self.x1, self.y1) < 0:
            raise ValueError("bounding box coordinates must be non-negative")
        if self.x2 <= self.x1 or self.y2 <= self.y1:
            raise ValueError("bounding box must have positive width and height")

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    def as_list(self) -> list[int]:
        return [self.x1, self.y1, self.x2, self.y2]

    @classmethod
    def from_value(cls, value: Sequence[object]) -> BoundingBox:
        if len(value) != 4:
            raise ValueError("bounding box must contain four coordinates")
        return cls(*(
            _require_integer("bounding box coordinate", coordinate)
            for coordinate in value
        ))


@dataclass(frozen=True, slots=True)
class Polygon:
    """Pixel-space polygon associated with a detected region."""

    points: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        if len(self.points) < 3:
            raise ValueError("polygon must contain at least three points")
        for point in self.points:
            if len(point) != 2:
                raise ValueError("polygon point must contain two coordinates")
            x = _require_integer("polygon x coordinate", point[0])
            y = _require_integer("polygon y coordinate", point[1])
            if x < 0 or y < 0:
                raise ValueError("polygon coordinates must be non-negative")

    def as_list(self) -> list[list[int]]:
        return [[x, y] for x, y in self.points]

    @classmethod
    def from_value(cls, value: Sequence[Sequence[object]]) -> Polygon:
        points: list[tuple[int, int]] = []
        for raw_point in value:
            point = _sequence(raw_point)
            if len(point) != 2:
                raise ValueError("polygon point must contain two coordinates")
            points.append((
                _require_integer("polygon x coordinate", point[0]),
                _require_integer("polygon y coordinate", point[1]),
            ))
        return cls(tuple(points))


@dataclass(frozen=True, slots=True)
class Provenance:
    """Identity of the implementation that produced a stage result."""

    backend: str
    revision: str
    model: str | None = None
    config_hash: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty("backend", self.backend)
        _require_non_empty("revision", self.revision)
        _optional_string("model", self.model)
        _optional_string("config hash", self.config_hash)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "backend": self.backend,
            "revision": self.revision,
        }
        if self.model is not None:
            result["model"] = self.model
        if self.config_hash is not None:
            result["config_hash"] = self.config_hash
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Provenance:
        _reject_unknown_fields(
            value, frozenset({"backend", "revision", "model", "config_hash"}),
            "provenance",
        )
        return cls(
            backend=_require_non_empty("backend", value["backend"]),
            revision=_require_non_empty("revision", value["revision"]),
            model=_optional_string("model", value.get("model")),
            config_hash=_optional_string("config hash", value.get("config_hash")),
        )


@dataclass(frozen=True, slots=True)
class MaskReference:
    """Reference to a binary mask stored outside the JSON document."""

    reference: str
    encoding: str
    width: int
    height: int
    sha256: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty("mask reference", self.reference)
        _require_non_empty("mask encoding", self.encoding)
        _require_integer("mask width", self.width)
        _require_integer("mask height", self.height)
        if self.width <= 0 or self.height <= 0:
            raise ValueError("mask dimensions must be positive")
        if self.sha256 is not None:
            digest = _require_non_empty("mask sha256", self.sha256)
            if not _SHA256_RE.fullmatch(digest):
                raise ValueError("mask sha256 must be a 64-character hexadecimal digest")
        match = _OBJECT_REFERENCE_RE.fullmatch(self.reference)
        if match is not None and self.sha256 != match.group(1):
            raise ValueError("mask object reference must match its sha256")
        if match is not None and self.encoding != "png-mask-l1":
            raise ValueError("mask object encoding must be png-mask-l1")
        if match is None and _LEGACY_MASK_REFERENCE_RE.fullmatch(self.reference) is None:
            raise ValueError("mask reference must be a safe object or legacy reference")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "reference": self.reference,
            "encoding": self.encoding,
            "width": self.width,
            "height": self.height,
        }
        if self.sha256 is not None:
            result["sha256"] = self.sha256
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> MaskReference:
        _reject_unknown_fields(
            value,
            frozenset({"reference", "encoding", "width", "height", "sha256"}),
            "mask",
        )
        return cls(
            reference=_require_non_empty("mask reference", value["reference"]),
            encoding=_require_non_empty("mask encoding", value["encoding"]),
            width=_require_integer("mask width", value["width"]),
            height=_require_integer("mask height", value["height"]),
            sha256=_optional_string("mask sha256", value.get("sha256")),
        )


@dataclass(frozen=True, slots=True)
class ImageReference:
    """Content-addressed raster artifact used as an exact render base."""

    reference: str
    encoding: str
    width: int
    height: int
    mode: str
    sha256: str

    def __post_init__(self) -> None:
        match = _OBJECT_REFERENCE_RE.fullmatch(
            _require_non_empty("image reference", self.reference),
        )
        digest = _require_non_empty("image sha256", self.sha256)
        if match is None or not _SHA256_RE.fullmatch(digest):
            raise ValueError("image reference must be a content-addressed object")
        if match.group(1) != digest:
            raise ValueError("image object reference must match its sha256")
        _require_non_empty("image encoding", self.encoding)
        _require_integer("image width", self.width)
        _require_integer("image height", self.height)
        _require_non_empty("image mode", self.mode)
        if self.width <= 0 or self.height <= 0:
            raise ValueError("image dimensions must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "reference": self.reference,
            "encoding": self.encoding,
            "width": self.width,
            "height": self.height,
            "mode": self.mode,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ImageReference:
        _reject_unknown_fields(
            value,
            frozenset({
                "reference", "encoding", "width", "height", "mode", "sha256",
            }),
            "image artifact",
        )
        return cls(
            reference=_require_non_empty("image reference", value["reference"]),
            encoding=_require_non_empty("image encoding", value["encoding"]),
            width=_require_integer("image width", value["width"]),
            height=_require_integer("image height", value["height"]),
            mode=_require_non_empty("image mode", value["mode"]),
            sha256=_require_non_empty("image sha256", value["sha256"]),
        )
@dataclass(frozen=True, slots=True)
class OcrAlternative:
    text: str
    confidence: float | None
    provenance: Provenance

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("OCR alternative text must be a string")
        _validate_confidence(self.confidence)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "text": self.text,
            "provenance": self.provenance.to_dict(),
        }
        if self.confidence is not None:
            result["confidence"] = self.confidence
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> OcrAlternative:
        _reject_unknown_fields(
            value, frozenset({"text", "confidence", "provenance"}),
            "OCR alternative",
        )
        return cls(
            text=_require_string("OCR alternative text", value["text"]),
            confidence=_optional_number("OCR alternative confidence",
                                        value.get("confidence")),
            provenance=Provenance.from_dict(_mapping(value["provenance"])),
        )


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    language: str
    valid: bool
    confidence: float | None
    provenance: Provenance
    alternatives: tuple[OcrAlternative, ...] = ()
    routing_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("OCR text must be a string")
        _require_non_empty("OCR language", self.language)
        _require_boolean("OCR valid", self.valid)
        _validate_confidence(self.confidence)
        _optional_string("OCR routing reason", self.routing_reason)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "text": self.text,
            "language": self.language,
            "valid": self.valid,
            "provenance": self.provenance.to_dict(),
            "alternatives": [item.to_dict() for item in self.alternatives],
        }
        if self.confidence is not None:
            result["confidence"] = self.confidence
        if self.routing_reason is not None:
            result["routing_reason"] = self.routing_reason
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> OcrResult:
        _reject_unknown_fields(
            value,
            frozenset({
                "text", "language", "valid", "confidence", "provenance",
                "alternatives", "routing_reason",
            }),
            "OCR result",
        )
        alternatives = _default_if_null(value, "alternatives", ())
        return cls(
            text=_require_string("OCR text", value["text"]),
            language=_require_non_empty("OCR language", value["language"]),
            valid=_require_boolean("OCR valid", value["valid"]),
            confidence=_optional_number("OCR confidence", value.get("confidence")),
            provenance=Provenance.from_dict(_mapping(value["provenance"])),
            alternatives=tuple(
                OcrAlternative.from_dict(_mapping(item))
                for item in _sequence(alternatives)
            ),
            routing_reason=_optional_string(
                "OCR routing reason", value.get("routing_reason")),
        )


@dataclass(frozen=True, slots=True)
class TranslationResult:
    text: str
    target_language: str
    provenance: Provenance
    source_language: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("translation text must be a string")
        _require_non_empty("target language", self.target_language)
        _optional_string("source language", self.source_language)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "text": self.text,
            "target_language": self.target_language,
            "provenance": self.provenance.to_dict(),
        }
        if self.source_language is not None:
            result["source_language"] = self.source_language
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> TranslationResult:
        _reject_unknown_fields(
            value,
            frozenset({"text", "target_language", "source_language", "provenance"}),
            "translation",
        )
        return cls(
            text=_require_string("translation text", value["text"]),
            target_language=_require_non_empty(
                "target language", value["target_language"]),
            source_language=_optional_string(
                "source language", value.get("source_language")),
            provenance=Provenance.from_dict(_mapping(value["provenance"])),
        )


@dataclass(frozen=True, slots=True)
class TransformResult:
    """Non-translation text transformation, such as furigana segments."""

    kind: str
    value: object
    provenance: Provenance

    def __post_init__(self) -> None:
        _require_non_empty("transform kind", self.kind)
        # Reject values that cannot be persisted before they reach the cache.
        try:
            canonical_json(self.value)
        except (TypeError, ValueError, UnicodeError) as error:
            raise ValueError("transform value must be JSON serializable") from error
        object.__setattr__(self, "value", deepcopy(self.value))

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "value": deepcopy(self.value),
            "provenance": self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> TransformResult:
        _reject_unknown_fields(
            value, frozenset({"kind", "value", "provenance"}), "transform",
        )
        return cls(
            kind=_require_non_empty("transform kind", value["kind"]),
            value=value.get("value"),
            provenance=Provenance.from_dict(_mapping(value["provenance"])),
        )


@dataclass(frozen=True, slots=True)
class UserEdit:
    """Reader-owned overrides preserved across automatic reruns."""

    manual_translation: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"manual_translation": self.manual_translation}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> UserEdit:
        _reject_unknown_fields(
            value, frozenset({"manual_translation"}), "user edit",
        )
        manual = _default_if_null(value, "manual_translation", "")
        if not isinstance(manual, str):
            raise TypeError("manual translation must be a string")
        return cls(manual_translation=manual)


@dataclass(frozen=True, slots=True)
class CleanupMetadata:
    method: str
    provenance: Provenance
    mask: MaskReference | None = None

    def __post_init__(self) -> None:
        _require_non_empty("cleanup method", self.method)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "method": self.method,
            "provenance": self.provenance.to_dict(),
        }
        if self.mask is not None:
            result["mask"] = self.mask.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CleanupMetadata:
        _reject_unknown_fields(
            value, frozenset({"method", "provenance", "mask"}), "cleanup",
        )
        mask = value.get("mask")
        return cls(
            method=_require_non_empty("cleanup method", value["method"]),
            provenance=Provenance.from_dict(_mapping(value["provenance"])),
            mask=None if mask is None else MaskReference.from_dict(_mapping(mask)),
        )


@dataclass(frozen=True, slots=True)
class RenderMetadata:
    bbox: BoundingBox
    provenance: Provenance
    font_family: str | None = None
    font_size: int | None = None
    vertical: bool = False
    text_color: str | None = None
    orientation: str | None = None
    source_boxes: tuple[BoundingBox, ...] = ()
    line_count: int | None = None
    fit_status: str | None = None
    ellipsized: bool = False

    def __post_init__(self) -> None:
        _optional_string("font family", self.font_family)
        _optional_integer("font size", self.font_size)
        _require_boolean("vertical", self.vertical)
        _optional_string("text color", self.text_color)
        _optional_string("render orientation", self.orientation)
        object.__setattr__(self, "source_boxes", tuple(self.source_boxes))
        _optional_integer("render line count", self.line_count)
        _optional_string("render fit status", self.fit_status)
        if self.font_size is not None and self.font_size <= 0:
            raise ValueError("font size must be positive")
        if self.line_count is not None and self.line_count <= 0:
            raise ValueError("render line count must be positive")
        if self.fit_status is not None and self.fit_status not in {
            "fit", "ellipsized", "overflow",
        }:
            raise ValueError("invalid render fit status")
        _require_boolean("render ellipsized", self.ellipsized)
        if self.ellipsized and self.fit_status != "ellipsized":
            raise ValueError("ellipsized render must have ellipsized fit status")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "bbox": self.bbox.as_list(),
            "provenance": self.provenance.to_dict(),
            "vertical": self.vertical,
            "ellipsized": self.ellipsized,
        }
        if self.font_family is not None:
            result["font_family"] = self.font_family
        if self.font_size is not None:
            result["font_size"] = self.font_size
        if self.text_color is not None:
            result["text_color"] = self.text_color
        if self.orientation is not None:
            result["orientation"] = self.orientation
        if self.source_boxes:
            result["source_boxes"] = [box.as_list() for box in self.source_boxes]
        if self.line_count is not None:
            result["line_count"] = self.line_count
        if self.fit_status is not None:
            result["fit_status"] = self.fit_status
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RenderMetadata:
        _reject_unknown_fields(
            value,
            frozenset({
                "bbox", "provenance", "font_family", "font_size", "vertical",
                "text_color", "orientation", "source_boxes",
                "line_count", "fit_status", "ellipsized",
            }),
            "render",
        )
        return cls(
            bbox=BoundingBox.from_value(_sequence(value["bbox"])),
            provenance=Provenance.from_dict(_mapping(value["provenance"])),
            font_family=_optional_string("font family", value.get("font_family")),
            font_size=_optional_integer("font size", value.get("font_size")),
            vertical=_require_boolean(
                "vertical", _default_if_null(value, "vertical", False)),
            text_color=_optional_string("text color", value.get("text_color")),
            orientation=_optional_string(
                "render orientation", value.get("orientation")),
            source_boxes=tuple(
                BoundingBox.from_value(_sequence(item))
                for item in _sequence(_default_if_null(value, "source_boxes", ()))
            ),
            line_count=_optional_integer(
                "render line count", value.get("line_count")),
            fit_status=_optional_string(
                "render fit status", value.get("fit_status")),
            ellipsized=_require_boolean(
                "render ellipsized",
                _default_if_null(value, "ellipsized", False),
            ),
        )


@dataclass(frozen=True, slots=True)
class PageRegion:
    id: str
    kind: str
    bbox: BoundingBox
    reading_order: int
    confidence: float | None = None
    polygon: Polygon | None = None
    mask: MaskReference | None = None
    parent_id: str | None = None
    detector_label: str | None = None
    detection_provenance: Provenance | None = None
    ocr: OcrResult | None = None
    translation: TranslationResult | None = None
    transform: TransformResult | None = None
    cleanup: CleanupMetadata | None = None
    render: RenderMetadata | None = None
    source_font_size: int | None = None
    user: UserEdit = field(default_factory=UserEdit)

    def __post_init__(self) -> None:
        _require_non_empty("region id", self.id)
        _require_non_empty("region kind", self.kind)
        _require_integer("reading order", self.reading_order)
        if self.reading_order < 0:
            raise ValueError("reading order must be non-negative")
        _validate_confidence(self.confidence)
        if self.parent_id == self.id:
            raise ValueError("a region cannot be its own parent")
        _optional_string("parent id", self.parent_id)
        _optional_string("detector label", self.detector_label)
        _optional_integer("source font size", self.source_font_size)
        if self.source_font_size is not None and self.source_font_size <= 0:
            raise ValueError("source font size must be positive")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.id,
            "kind": self.kind,
            "bbox": self.bbox.as_list(),
            "reading_order": self.reading_order,
        }
        optional = {
            "confidence": self.confidence,
            "polygon": None if self.polygon is None else self.polygon.as_list(),
            "mask": None if self.mask is None else self.mask.to_dict(),
            "parent_id": self.parent_id,
            "detector_label": self.detector_label,
            "detection_provenance": (None if self.detection_provenance is None
                                     else self.detection_provenance.to_dict()),
            "ocr": None if self.ocr is None else self.ocr.to_dict(),
            "translation": (None if self.translation is None
                            else self.translation.to_dict()),
            "transform": None if self.transform is None else self.transform.to_dict(),
            "cleanup": None if self.cleanup is None else self.cleanup.to_dict(),
            "render": None if self.render is None else self.render.to_dict(),
            "source_font_size": self.source_font_size,
        }
        result.update((key, value) for key, value in optional.items() if value is not None)
        result["user"] = self.user.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PageRegion:
        _reject_unknown_fields(
            value,
            frozenset({
                "id", "kind", "bbox", "reading_order", "confidence", "polygon",
                "mask", "parent_id", "detector_label", "detection_provenance",
                "ocr", "translation", "transform", "cleanup", "render",
                "source_font_size", "user",
            }),
            "region",
        )
        polygon = value.get("polygon")
        mask = value.get("mask")
        detection_provenance = value.get("detection_provenance")
        ocr = value.get("ocr")
        translation = value.get("translation")
        transform = value.get("transform")
        cleanup = value.get("cleanup")
        render = value.get("render")
        user = _default_if_null(value, "user", {})
        return cls(
            id=_require_non_empty("region id", value["id"]),
            kind=_require_non_empty("region kind", value["kind"]),
            bbox=BoundingBox.from_value(_sequence(value["bbox"])),
            reading_order=_require_integer("reading order", value["reading_order"]),
            confidence=_optional_number("region confidence", value.get("confidence")),
            polygon=None if polygon is None else Polygon.from_value(_sequence(polygon)),
            mask=None if mask is None else MaskReference.from_dict(_mapping(mask)),
            parent_id=_optional_string("parent id", value.get("parent_id")),
            detector_label=_optional_string(
                "detector label", value.get("detector_label")),
            detection_provenance=(None if detection_provenance is None
                                  else Provenance.from_dict(_mapping(detection_provenance))),
            ocr=None if ocr is None else OcrResult.from_dict(_mapping(ocr)),
            translation=(None if translation is None
                         else TranslationResult.from_dict(_mapping(translation))),
            transform=(None if transform is None
                       else TransformResult.from_dict(_mapping(transform))),
            cleanup=(None if cleanup is None
                     else CleanupMetadata.from_dict(_mapping(cleanup))),
            render=None if render is None else RenderMetadata.from_dict(_mapping(render)),
            source_font_size=_optional_integer(
                "source font size", value.get("source_font_size")),
            user=UserEdit.from_dict(_mapping(user)),
        )


@dataclass(frozen=True, slots=True)
class PageDocument:
    """Serializable state passed between processing stages for one page."""

    page_id: str
    pipeline: str
    pipeline_revision: str
    source_hash: str
    width: int
    height: int
    regions: tuple[PageRegion, ...] = ()
    stage_provenance: Mapping[str, Provenance] = field(default_factory=dict)
    schema_version: int = PAGE_DOCUMENT_SCHEMA_VERSION
    cleanup_base: ImageReference | None = None

    def __post_init__(self) -> None:
        _require_non_empty("page id", self.page_id)
        pipeline = _require_non_empty("pipeline", self.pipeline)
        if pipeline not in VALID_PIPELINES:
            raise ValueError(f"unknown pipeline: {pipeline}")
        validate_pipeline_revision(self.pipeline_revision)
        source_hash = _require_non_empty("source hash", self.source_hash)
        if not _SHA256_RE.fullmatch(source_hash):
            raise ValueError("source hash must be a 64-character hexadecimal digest")
        _require_integer("schema version", self.schema_version)
        if self.schema_version != PAGE_DOCUMENT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported page document schema {self.schema_version}; "
                f"expected {PAGE_DOCUMENT_SCHEMA_VERSION}"
            )
        _require_integer("image width", self.width)
        _require_integer("image height", self.height)
        if self.width <= 0 or self.height <= 0:
            raise ValueError("page dimensions must be positive")
        if self.cleanup_base is not None and (
            self.cleanup_base.width != self.width
            or self.cleanup_base.height != self.height
            or self.cleanup_base.mode != "RGB"
            or self.cleanup_base.encoding != "png"
        ):
            raise ValueError("cleanup base must be an RGB PNG matching the page")

        object.__setattr__(self, "regions", tuple(self.regions))
        ids = {region.id for region in self.regions}
        if len(ids) != len(self.regions):
            raise ValueError("region ids must be unique")
        orders = {region.reading_order for region in self.regions}
        if len(orders) != len(self.regions):
            raise ValueError("region reading orders must be unique")
        for region in self.regions:
            if region.bbox.x2 > self.width or region.bbox.y2 > self.height:
                raise ValueError(f"region {region.id!r} lies outside the page")
            if region.parent_id is not None and region.parent_id not in ids:
                raise ValueError(f"region {region.id!r} has an unknown parent")
            if region.polygon is not None and any(
                x >= self.width or y >= self.height
                for x, y in region.polygon.points
            ):
                raise ValueError(f"region {region.id!r} polygon lies outside the page")
            for mask in (
                region.mask,
                None if region.cleanup is None else region.cleanup.mask,
            ):
                if mask is not None and (
                    mask.width != self.width or mask.height != self.height
                ):
                    raise ValueError(
                        f"region {region.id!r} mask dimensions do not match the page"
                    )
            if region.render is not None and (
                region.render.bbox.x2 > self.width
                or region.render.bbox.y2 > self.height
            ):
                raise ValueError(f"region {region.id!r} render box lies outside the page")
            if region.render is not None:
                render_box = region.render.bbox
                if (
                    render_box.x1 < region.bbox.x1
                    or render_box.y1 < region.bbox.y1
                    or render_box.x2 > region.bbox.x2
                    or render_box.y2 > region.bbox.y2
                ):
                    raise ValueError(
                        f"region {region.id!r} render box escapes region bounds"
                    )
                for source_box in region.render.source_boxes:
                    if (
                        source_box.x1 < region.bbox.x1
                        or source_box.y1 < region.bbox.y1
                        or source_box.x2 > region.bbox.x2
                        or source_box.y2 > region.bbox.y2
                    ):
                        raise ValueError(
                            f"region {region.id!r} source box escapes region bounds"
                        )

        parents = {region.id: region.parent_id for region in self.regions}
        for region_id in parents:
            seen: set[str] = set()
            current: str | None = region_id
            while current is not None:
                if current in seen:
                    raise ValueError("region parent graph contains a cycle")
                seen.add(current)
                current = parents[current]

        # Freeze caller-owned mappings so the document stays stable after creation.
        object.__setattr__(
            self, "stage_provenance", MappingProxyType(dict(self.stage_provenance))
        )

    def to_dict(self) -> dict[str, object]:
        result = {
            "schema_version": self.schema_version,
            "page_id": self.page_id,
            "pipeline": self.pipeline,
            "pipeline_revision": self.pipeline_revision,
            "source_hash": self.source_hash,
            "image": {"width": self.width, "height": self.height},
            "regions": [region.to_dict() for region in self.regions],
            "stage_provenance": {
                name: provenance.to_dict()
                for name, provenance in sorted(self.stage_provenance.items())
            },
        }
        if self.cleanup_base is not None:
            result["cleanup_base"] = self.cleanup_base.to_dict()
        return result

    def to_json(self) -> str:
        """Return canonical UTF-8-safe JSON suitable for hashes and caches."""
        return canonical_json(self.to_dict())

    def content_hash(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PageDocument:
        _reject_unknown_fields(
            value,
            frozenset({
                "schema_version", "page_id", "pipeline", "pipeline_revision",
                "source_hash", "image", "cleanup_base", "regions",
                "stage_provenance",
            }),
            "page document",
        )
        image = _mapping(value["image"])
        _reject_unknown_fields(image, frozenset({"width", "height"}), "image")
        regions = _sequence(_default_if_null(value, "regions", ()))
        provenance = _mapping(_default_if_null(value, "stage_provenance", {}))
        cleanup_base = value.get("cleanup_base")
        return cls(
            schema_version=_require_integer("schema version", value["schema_version"]),
            page_id=_require_non_empty("page id", value["page_id"]),
            pipeline=_require_non_empty("pipeline", value["pipeline"]),
            pipeline_revision=validate_pipeline_revision(
                value["pipeline_revision"]),
            source_hash=_require_non_empty("source hash", value["source_hash"]),
            width=_require_integer("image width", image["width"]),
            height=_require_integer("image height", image["height"]),
            cleanup_base=(None if cleanup_base is None else
                          ImageReference.from_dict(_mapping(cleanup_base))),
            regions=tuple(PageRegion.from_dict(_mapping(item)) for item in regions),
            stage_provenance={
                _require_non_empty("stage name", name):
                Provenance.from_dict(_mapping(item))
                for name, item in provenance.items()
            },
        )

    @classmethod
    def from_json(cls, value: str | bytes) -> PageDocument:
        decoded = json.loads(
            value,
            parse_float=Decimal,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"invalid JSON number: {constant}")),
        )
        return cls.from_dict(_mapping(decoded))


def make_region_id(page_id: str, kind: str, bbox: BoundingBox,
                   discriminator: str = "") -> str:
    """Create an opaque ID stable within one pipeline revision's detections.

    Reading-order changes do not affect the ID. A detector or pipeline revision
    may change geometry and therefore IDs; callers must not treat them as
    permanent identities across detector upgrades.
    """
    _require_non_empty("page id", page_id)
    _require_non_empty("region kind", kind)
    material = json.dumps(
        [page_id, kind, bbox.as_list(), discriminator],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"r_{hashlib.sha256(material).hexdigest()[:16]}"


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("expected an object")
    return value


def _sequence(value: object) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("expected an array")
    return value
