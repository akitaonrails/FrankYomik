import hashlib
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from pipeline import (
    PAGE_DOCUMENT_SCHEMA_VERSION,
    BoundingBox,
    CleanupMetadata,
    MaskReference,
    OcrAlternative,
    OcrResult,
    PageDocument,
    PageRegion,
    Polygon,
    Provenance,
    RenderMetadata,
    TransformResult,
    TranslationResult,
    UserEdit,
    canonical_json,
    make_region_id,
)

SOURCE_HASH = "b" * 64


def _document() -> PageDocument:
    detector = Provenance("semantic-detector", "postprocess-v2", "weights@abc")
    mask = MaskReference(
        "masks/r_1.rle", "coco-rle", 1200, 1800, "a" * 64
    )
    region = PageRegion(
        id="r_1",
        kind="text",
        bbox=BoundingBox(10, 20, 200, 180),
        polygon=Polygon(((10, 20), (200, 20), (200, 180), (10, 180))),
        mask=mask,
        confidence=0.95,
        reading_order=0,
        parent_id="r_balloon",
        detector_label="text_bubble",
        detection_provenance=detector,
        ocr=OcrResult(
            text="こんにちは",
            language="ja",
            valid=True,
            confidence=0.91,
            provenance=Provenance("manga-ocr", "0.1.14", "kha-white/manga-ocr-base"),
            alternatives=(
                OcrAlternative(
                    "こんにちは。", 0.87, Provenance("hayai", "git-123", "hayai-v1")
                ),
            ),
            routing_reason="short_text",
        ),
        translation=TranslationResult(
            "Hello", "en", Provenance("ollama", "prompt-v2", "qwen"), "ja"
        ),
        transform=TransformResult(
            "text", "Hello", Provenance("translation-adapter", "v1")
        ),
        cleanup=CleanupMetadata("uniform_fill", Provenance("fill", "v1"), mask),
        render=RenderMetadata(
            BoundingBox(14, 25, 196, 175),
            Provenance("manga-renderer", "v2"),
            "Comic Neue",
            24,
            False,
            "#111111",
        ),
        source_font_size=20,
        user=UserEdit("Editor override"),
    )
    balloon = PageRegion(
        id="r_balloon",
        kind="balloon",
        bbox=BoundingBox(5, 10, 210, 190),
        confidence=0.98,
        reading_order=1,
        mask=mask,
        detection_provenance=detector,
    )
    return PageDocument(
        page_id="page-001",
        pipeline="manga_translate",
        pipeline_revision="sha256:pipeline",
        source_hash=SOURCE_HASH,
        width=1200,
        height=1800,
        regions=(region, balloon),
        stage_provenance={"detection": detector},
    )


def test_full_document_round_trip_is_lossless_and_canonical():
    document = _document()

    encoded = document.to_json()
    restored = PageDocument.from_json(encoded)

    assert restored == document
    assert restored.to_json() == encoded
    assert json.loads(encoded)["schema_version"] == PAGE_DOCUMENT_SCHEMA_VERSION
    assert "こんにちは" in encoded


def test_serialization_and_content_hash_ignore_mapping_insertion_order():
    first = _document()
    second = PageDocument(
        page_id=first.page_id,
        pipeline=first.pipeline,
        pipeline_revision=first.pipeline_revision,
        source_hash=first.source_hash,
        width=first.width,
        height=first.height,
        regions=first.regions,
        stage_provenance={
            "translation": Provenance("translator", "v1"),
            "detection": first.stage_provenance["detection"],
        },
    )
    third = PageDocument(
        page_id=second.page_id,
        pipeline=second.pipeline,
        pipeline_revision=second.pipeline_revision,
        source_hash=second.source_hash,
        width=second.width,
        height=second.height,
        regions=second.regions,
        stage_provenance={
            "detection": first.stage_provenance["detection"],
            "translation": Provenance("translator", "v1"),
        },
    )

    assert second.to_json() == third.to_json()
    assert second.content_hash() == third.content_hash()


def test_region_id_is_stable_when_reading_order_changes():
    bbox = BoundingBox(1, 2, 30, 40)
    region_id = make_region_id("page", "text", bbox)

    assert region_id == make_region_id("page", "text", bbox)
    assert region_id != make_region_id("page", "sfx", bbox)
    assert region_id != make_region_id("page", "text", bbox, "duplicate-2")


@pytest.mark.parametrize(
    "regions, message",
    [
        (
            (
                PageRegion("same", "text", BoundingBox(0, 0, 10, 10), 0),
                PageRegion("same", "text", BoundingBox(10, 0, 20, 10), 1),
            ),
            "ids",
        ),
        (
            (
                PageRegion("one", "text", BoundingBox(0, 0, 10, 10), 0),
                PageRegion("two", "text", BoundingBox(10, 0, 20, 10), 0),
            ),
            "reading orders",
        ),
        (
            (PageRegion("one", "text", BoundingBox(0, 0, 10, 10), 0,
                        parent_id="missing"),),
            "unknown parent",
        ),
    ],
)
def test_document_rejects_ambiguous_region_graph(regions, message):
    with pytest.raises(ValueError, match=message):
        PageDocument(
            "p", "manga_translate", "v1", SOURCE_HASH, 100, 100, regions)


def test_document_rejects_region_outside_image():
    region = PageRegion("one", "text", BoundingBox(0, 0, 101, 10), 0)

    with pytest.raises(ValueError, match="outside"):
        PageDocument(
            "p", "manga_translate", "v1", SOURCE_HASH, 100, 100, (region,))


def test_unknown_schema_is_rejected_before_use():
    payload = _document().to_dict()
    payload["schema_version"] = PAGE_DOCUMENT_SCHEMA_VERSION + 1

    with pytest.raises(ValueError, match="unsupported page document schema"):
        PageDocument.from_dict(payload)


def _set_path(payload, path, value):
    target = payload
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = value


@pytest.mark.parametrize(
    "path,value,message",
    [
        (("schema_version",), True, "schema version must be an integer"),
        (("page_id",), 7, "page id must be a string"),
        (("pipeline_revision",), 7, "invalid pipeline revision"),
        (("image", "width"), 1200.5, "image width must be an integer"),
        (("image", "height"), True, "image height must be an integer"),
        (("regions", 0, "bbox", 0), 10.5,
         "bounding box coordinate must be an integer"),
        (("regions", 0, "reading_order"), False,
         "reading order must be an integer"),
        (("regions", 0, "confidence"), True,
         "region confidence must be a number"),
        (("regions", 0, "mask", "width"), 1200.0,
         "mask width must be an integer"),
        (("regions", 0, "ocr", "valid"), "true",
         "OCR valid must be a boolean"),
        (("regions", 0, "render", "font_size"), 24.5,
         "font size must be an integer"),
        (("regions", 0, "render", "vertical"), 1,
         "vertical must be a boolean"),
        (("stage_provenance", "detection", "backend"), 3,
         "backend must be a string"),
    ],
)
def test_wire_parser_rejects_coercive_types(path, value, message):
    payload = deepcopy(_document().to_dict())
    _set_path(payload, path, value)

    with pytest.raises((TypeError, ValueError), match=message):
        PageDocument.from_dict(payload)


@pytest.mark.parametrize("source_hash", ["", "abc", "A" * 64, "g" * 64])
def test_document_rejects_invalid_source_hash(source_hash):
    payload = _document().to_dict()
    payload["source_hash"] = source_hash

    with pytest.raises(ValueError, match="source hash"):
        PageDocument.from_dict(payload)


def test_document_rejects_unknown_pipeline():
    payload = _document().to_dict()
    payload["pipeline"] = "manga"

    with pytest.raises(ValueError, match="unknown pipeline"):
        PageDocument.from_dict(payload)


def test_document_rejects_parent_cycle():
    payload = _document().to_dict()
    payload["regions"][0]["parent_id"] = "r_balloon"
    payload["regions"][1]["parent_id"] = "r_1"

    with pytest.raises(ValueError, match="cycle"):
        PageDocument.from_dict(payload)


@pytest.mark.parametrize(
    "path,value,message",
    [
        (("regions", 0, "polygon", 0, 0), 1200, "polygon lies outside"),
        (("regions", 0, "render", "bbox", 2), 1201,
         "render box lies outside"),
        (("regions", 0, "mask", "width"), 1199,
         "mask dimensions do not match"),
        (("regions", 0, "cleanup", "mask", "height"), 1799,
         "mask dimensions do not match"),
    ],
)
def test_document_rejects_nested_geometry_outside_page(path, value, message):
    payload = deepcopy(_document().to_dict())
    _set_path(payload, path, value)

    with pytest.raises(ValueError, match=message):
        PageDocument.from_dict(payload)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_json_parser_rejects_non_finite_numbers(constant):
    payload = _document().to_json().replace("0.95", constant, 1)

    with pytest.raises(ValueError, match="invalid JSON number"):
        PageDocument.from_json(payload)


def test_cross_runtime_canonical_fixture():
    fixture_path = (
        Path(__file__).parents[1] / "fixtures" / "page_document_v2_canonical.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    document = PageDocument.from_dict(fixture["input"])
    canonical = document.to_json()

    assert canonical == fixture["canonical"]
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == fixture["sha256"]


def test_cross_runtime_canonical_normalization_cases():
    fixture_path = (
        Path(__file__).parents[1] / "fixtures" / "canonical_json_normalization.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    for case in fixture["cases"]:
        value = json.loads(case["input"], parse_float=Decimal)
        assert canonical_json(value) == case["canonical"]


def _normal_form_fixture():
    path = Path(__file__).parents[1] / "fixtures" / "page_document_v2_normal_form.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _set_pointer(payload, pointer, value, *, delete=False):
    parts = pointer.removeprefix("/").split("/")
    target = payload
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    final = parts[-1]
    if isinstance(target, list):
        if delete:
            target.pop(int(final))
        else:
            target[int(final)] = value
    elif delete:
        target.pop(final, None)
    else:
        target[final] = value


@pytest.mark.parametrize("pointer", _normal_form_fixture()["nullable_fields"])
def test_nullable_fields_have_one_normal_form(pointer):
    base = _normal_form_fixture()["base"]
    missing = deepcopy(base)
    explicit_null = deepcopy(base)
    _set_pointer(missing, pointer, None, delete=True)
    _set_pointer(explicit_null, pointer, None)

    assert PageDocument.from_dict(missing).to_json() == PageDocument.from_dict(
        explicit_null
    ).to_json()


@pytest.mark.parametrize("case", _normal_form_fixture()["defaulted_fields"])
def test_defaulted_fields_have_one_normal_form(case):
    results = []
    for mode in ("missing", "null", "explicit"):
        payload = deepcopy(_normal_form_fixture()["base"])
        _set_pointer(
            payload,
            case["path"],
            None if mode == "null" else deepcopy(case["default"]),
            delete=mode == "missing",
        )
        results.append(PageDocument.from_dict(payload).to_json())

    assert results[0] == results[1] == results[2]


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("image",),
        ("regions", 0),
        ("regions", 0, "ocr"),
        ("regions", 0, "translation"),
        ("regions", 0, "render"),
        ("stage_provenance", "detection"),
    ],
)
def test_page_document_rejects_unknown_fields(path):
    payload = deepcopy(_document().to_dict())
    target = payload
    for component in path:
        target = target[component]
    target["future_field"] = "must not be ignored"

    with pytest.raises(ValueError, match="unknown field"):
        PageDocument.from_dict(payload)
