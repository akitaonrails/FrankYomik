"""Worker-to-cache contract tests using injected, model-free stage fakes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PIL import Image

from pipeline.detection import SemanticDetection
from pipeline.document import BoundingBox, OcrAlternative, Polygon, Provenance
from pipeline.translation import PageTranslationResult, TranslationValue
from webtoon.processor import WebtoonPageResult, detect_bubbles_rtdetr
from webtoon.bubble_detector import WebtoonBubble
from webtoon.inpainter import build_effective_cleanup_mask, cleanup_bubble
from webtoon.ocr import TextDetection
from worker.consumer import STREAM_HIGH, Consumer
from worker.job import (
    PipelineDependencies,
    ProcessingJob,
    ProcessingResult,
    process_job,
    production_dependencies,
)

pytestmark = pytest.mark.integration


class _SolidCleanupBackend:
    def load(self):
        return True

    def inpaint(self, image, _mask):
        return Image.new("RGB", image.size, (7, 8, 9))

    def unload(self):
        return None


def test_production_cleanup_stage_preserves_pixels_outside_effective_mask():
    pixels = np.indices((80, 100)).sum(axis=0).astype(np.uint8)
    original_array = np.stack((pixels, 255 - pixels, pixels // 2), axis=2)
    original = Image.fromarray(original_array)
    target = original.copy()
    detection = TextDetection(
        bbox_poly=[[30, 25], [60, 25], [60, 45], [30, 45]],
        text="테스트", confidence=0.9, bbox_rect=(30, 25, 60, 45),
    )
    semantic_mask = np.zeros((80, 100), dtype=np.uint8)
    semantic_mask[10:70, 10:90] = 255
    bubble = WebtoonBubble(
        bbox=(10, 10, 90, 70), text_regions=[detection],
        has_bubble_boundary=True, bg_color=(255, 255, 255),
        bubble_mask=semantic_mask,
    )

    with patch("webtoon.inpainter.INPAINT_ENABLED", True):
        outcome = cleanup_bubble(
            original, bubble, backend=_SolidCleanupBackend(), target_img=target,
        )

    effective_mask = np.asarray(
        build_effective_cleanup_mask(original, bubble),
    ) > 0
    assert outcome.method == "lama_tiles"
    assert np.array_equal(
        np.asarray(target)[~effective_mask], original_array[~effective_mask],
    )


@dataclass
class BubbleResult:
    bbox: tuple[int, int, int, int]
    ocr_text: str = ""
    is_valid: bool = False
    transformed: object = None
    is_artwork_text: bool = False
    mask: object = None
    source_font_size: int | None = None
    ocr_confidence: float | None = None
    ocr_provenance: Provenance | None = None
    ocr_alternatives: tuple[OcrAlternative, ...] = ()
    ocr_routing_reason: str | None = None
    ocr_degraded: bool = False
    cleanup_mask: object = None
    cleanup_method: str | None = None
    cleanup_degraded: bool = False


def _png_bytes(width: int = 120, height: int = 160) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="PNG")
    return output.getvalue()


def _fake_dependencies(
    pipeline: str, calls: dict[str, int],
) -> PipelineDependencies:
    """Return deterministic adapters with no concrete model imports."""
    base = production_dependencies()

    def count(stage: str) -> None:
        calls[stage] = calls.get(stage, 0) + 1

    def decode(data: bytes):
        image = Image.open(BytesIO(data)).convert("RGB")
        return np.asarray(image)[:, :, ::-1].copy(), image

    def encode(image: Image.Image) -> bytes:
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def create_manga(img_cv, img_pil, name):
        return SimpleNamespace(
            name=name, img_cv=img_cv, img_pil=img_pil,
            bubbles_raw=[], bubble_results=[],
        )

    def detect_manga(page) -> None:
        count("detection")
        provenance = Provenance("fake-detector", "test-revision")
        semantic_mask = np.zeros((160, 120), dtype=np.uint8)
        semantic_mask[20:110, 15:90] = 255
        page.semantic_detections = [
            SemanticDetection(
                "balloon-1", "balloon", BoundingBox(15, 20, 90, 110),
                0.9, "bubble", provenance,
                polygon=Polygon(((15, 20), (90, 20), (90, 110), (15, 110))),
                mask=semantic_mask, reading_order=0,
            ),
            SemanticDetection(
                "text-1", "text", BoundingBox(25, 30, 80, 90),
                0.85, "text_bubble", provenance,
                parent_id="balloon-1", reading_order=1,
            ),
        ]
        page.bubbles_raw = [{
            "bbox": (15, 20, 90, 110),
            "is_artwork": False,
            "score": 0.9,
            "semantic_id": "balloon-1",
        }]

    def ocr_manga(_image, bubble) -> BubbleResult:
        count("ocr")
        selected = Provenance(
            "hayai-ocr", "hayai-test", "hayai-v2@test-weights",
        )
        primary = Provenance(
            "manga-ocr", "manga-test", "manga@test-weights",
        )
        return BubbleResult(
            bbox=bubble["bbox"], ocr_text="漢字です", is_valid=True,
            source_font_size=18,
            ocr_confidence=0.8,
            ocr_provenance=selected,
            ocr_alternatives=(OcrAlternative("漢字てす", 0.6, primary),),
            ocr_routing_reason="hayai_crop_inconsistent",
        )

    def translate_text(_text, _target_lang) -> str:
        count("translation")
        return "Isto e kanji"

    def translate_page(request) -> PageTranslationResult:
        count("translation")
        calls["translation_request"] = request
        values = []
        for index, item in enumerate(request.items, start=1):
            text = "ESTRONDO" if item.kind == "sfx" else f"Ola {index}"
            if request.source_language == "ja":
                text = "Isto e kanji"
            values.append(TranslationValue(item.id, text))
        return PageTranslationResult(tuple(values), batch_attempts=1)

    def add_furigana(result: BubbleResult) -> None:
        count("transform")
        result.transformed = [{
            "text": "漢字", "furigana": "かんじ", "needs_furigana": True,
        }]

    def create_webtoon(img_cv, img_pil, name):
        return SimpleNamespace(
            name=name, img_cv=img_cv, img_pil=img_pil,
            regions=[], sfx_detections=[], semantic_detections=[],
        )

    def detect_webtoon(page) -> None:
        count("detection")
        page.detected = True
        provenance = Provenance("fake-detector", "test-revision")
        first_mask = np.zeros((160, 120), dtype=np.uint8)
        first_mask[20:100, 10:100] = 255
        second_mask = np.zeros((160, 120), dtype=np.uint8)
        second_mask[102:150, 15:105] = 255
        page.semantic_detections = [
            SemanticDetection(
                "webtoon-panel", "panel", BoundingBox(0, 0, 120, 155),
                0.95, "panel", provenance, reading_order=0,
            ),
            SemanticDetection(
                "webtoon-balloon", "balloon", BoundingBox(10, 20, 100, 100),
                0.92, "bubble", provenance, mask=first_mask, reading_order=1,
            ),
            SemanticDetection(
                "webtoon-text", "text", BoundingBox(20, 30, 90, 80),
                0.88, "text_bubble", provenance,
                parent_id="webtoon-balloon", reading_order=2,
            ),
            SemanticDetection(
                "webtoon-balloon-2", "balloon", BoundingBox(15, 102, 105, 150),
                0.91, "bubble", provenance, mask=second_mask, reading_order=3,
            ),
            SemanticDetection(
                "webtoon-text-2", "text", BoundingBox(30, 108, 90, 132),
                0.86, "text_bubble", provenance,
                parent_id="webtoon-balloon-2", reading_order=4,
            ),
            SemanticDetection(
                "webtoon-sfx", "sfx", BoundingBox(80, 5, 115, 18),
                0.81, "text_free", provenance, reading_order=5,
            ),
        ]

    def ocr_webtoon(page) -> None:
        assert page.detected is True
        count("ocr")
        first_bubble = SimpleNamespace(
            bbox=(10, 20, 100, 100), combined_text="안녕하세요",
            semantic_id="webtoon-balloon")
        second_bubble = SimpleNamespace(
            bbox=(15, 102, 105, 150), combined_text="두 번째",
            semantic_id="webtoon-balloon-2")
        page.regions = [
            SimpleNamespace(bubble=first_bubble, is_valid=True, english=""),
            # A second render subgroup in the first balloon has no separate
            # semantic identity and must be ordered after all detector regions.
            SimpleNamespace(bubble=first_bubble, is_valid=True, english=""),
            SimpleNamespace(bubble=second_bubble, is_valid=True, english=""),
        ]
        page.sfx_detections = [SimpleNamespace(
            bbox_rect=(80, 5, 115, 18), text="쾅")]

    def translate_webtoon(page, *, parallel, target_lang) -> None:
        assert parallel is True
        assert target_lang == "pt-br"
        count("translation")
        for index, region in enumerate(page.regions, start=1):
            region.english = f"Ola {index}"

    def prepare_webtoon_translation(page) -> None:
        for region in page.regions:
            region.source_text = region.bubble.combined_text
        page.sfx_translations = ["" for _ in page.sfx_detections]
        page.sfx_translation_eligible = [True for _ in page.sfx_detections]

    def cleanup_manga(page, _mode) -> None:
        count("cleanup")
        page.output_img = page.img_pil.copy()
        page.render_plan = SimpleNamespace(
            base_font_size=18,
            font_sizes={index: 18 for index, _ in enumerate(page.bubble_results)},
        )
        for result in page.bubble_results:
            result.cleanup_mask = np.zeros(
                (page.img_pil.height, page.img_pil.width), dtype=np.uint8,
            )
            result.cleanup_method = "stroke_clear"

    def cleanup_webtoon(page) -> None:
        count("cleanup")
        page.output_img = page.img_pil.copy()
        for region in page.regions:
            region.bubble.cleanup_mask = np.zeros(
                (page.img_pil.height, page.img_pil.width), dtype=np.uint8,
            )
            region.bubble.cleanup_method = "uniform_fill"

    def render_document_region(target, _source, bbox, _text, **_kwargs) -> None:
        count("render")
        target.putpixel((bbox[0], bbox[1]), (1, 2, 3))
        return {"font_size": 12, "line_count": 1,
                "fit_status": "fit", "ellipsized": False}

    def render_document_sfx(target, _source, bbox, _text) -> None:
        count("render")
        target.putpixel((bbox[0], bbox[1]), (4, 5, 6))
        return {"font_size": 10, "line_count": 1,
                "fit_status": "fit", "ellipsized": False}

    def render_translation(*_args, **_kwargs):
        count("render")
        return {"font_size": 18, "line_count": 1,
                "fit_status": "fit", "ellipsized": False}

    def render_furigana(*_args, **_kwargs):
        count("render")
        return {"font_size": 16, "line_count": 2,
                "fit_status": "fit", "ellipsized": False}

    def render_artwork(*_args, **kwargs):
        count("render")
        calls["artwork_inpainted"] = kwargs.get("inpainted")
        return {"font_size": 18, "line_count": 1,
                "fit_status": "fit", "ellipsized": False}

    def analyze(_image):
        count("layout")
        column = SimpleNamespace(x0=80, y0=10, x1=110, y1=145)
        return SimpleNamespace(
            is_prose=pipeline == "book_furigana", columns=[column])

    def read_columns(_image, layout, **_kwargs):
        count("ocr")
        count("transform")
        ruby = SimpleNamespace(reading="かん", y0=20, y1=40)
        return [SimpleNamespace(
            index=0, column=layout.columns[0], text="漢字です", ruby=[ruby])]

    def config(name: str):
        return {
            "BOOK_RENDER_SCALE": 1,
            "MIN_FONT_SIZE": 10,
            "EN_PAGE_FONT_DIVISOR": 30,
        }[name]

    return replace(
        base,
        decode_image=decode,
        encode_image=encode,
        analyze_layout=analyze,
        read_columns=read_columns,
        render_book=lambda image, *_args, **_kwargs: (
            count("render") or image.copy()
        ),
        extract_manga_mask=lambda *_args, **_kwargs: None,
        annotate=lambda text: [{"text": text, "needs_furigana": False}],
        clear_region=lambda *_args, **_kwargs: None,
        clear_strokes=lambda *_args, **_kwargs: None,
        ocr_prose=lambda _image: "漢字です",
        create_manga_page=create_manga,
        detect_manga=detect_manga,
        ocr_manga=ocr_manga,
        cleanup_manga=cleanup_manga,
        render_manga=lambda page, _mode: (
            count("legacy_render") or encode(page.img_pil)
        ),
        transform_furigana=add_furigana,
        translate_text=translate_text,
        translate_page=translate_page,
        furigana_font_limits=lambda _values: (None, None, None),
        estimate_source_font=lambda *_args, **_kwargs: 18,
        render_translation=render_translation,
        render_artwork_translation=render_artwork,
        render_furigana=render_furigana,
        pipeline_mode=lambda name: name,
        config_value=config,
        create_webtoon_page=create_webtoon,
        detect_webtoon=detect_webtoon,
        ocr_webtoon=ocr_webtoon,
        translate_webtoon=translate_webtoon,
        prepare_webtoon_translation=prepare_webtoon_translation,
        cleanup_webtoon=cleanup_webtoon,
        render_webtoon=lambda page: count("legacy_render") or encode(page.img_pil),
        render_webtoon_document_region=render_document_region,
        render_webtoon_document_sfx=render_document_sfx,
        webtoon_document_geometry=lambda _page, region: (
            region.bubble.bbox, (region.bubble.bbox,),
        ),
    )


def test_degraded_uniform_cleanup_is_truthful_and_non_cacheable():
    calls: dict[str, int] = {}
    deps = _fake_dependencies("webtoon", calls)

    def cleanup(page) -> None:
        calls["cleanup"] = calls.get("cleanup", 0) + 1
        page.output_img = page.img_pil.copy()
        for region in page.regions:
            region.bubble.cleanup_method = "uniform_fill:degraded"
            region.bubble.cleanup_degraded = True
            region.bubble.cleanup_mask = np.zeros((160, 120), dtype=np.uint8)

    result = process_job(ProcessingJob(
        job_id="cleanup-degraded", pipeline="webtoon",
        image_bytes=_png_bytes(), source_hash="e" * 64,
        target_lang="pt-br",
    ), dependencies=replace(deps, cleanup_webtoon=cleanup))

    assert result.status == "completed", result.error
    assert result.cacheable is False
    cleanup_metadata = result.metadata_payload["regions"][1]["cleanup"]
    assert cleanup_metadata["method"] == "uniform_fill:degraded"
    assert cleanup_metadata["provenance"]["backend"] == (
        "deterministic-uniform-fill"
    )
    assert "model" not in cleanup_metadata["provenance"]
    assert cleanup_metadata["mask"]["encoding"] == "png-mask-l1"


def _message_fields(
    pipeline: str, source_hash: str, *, rerender: bool = False,
) -> dict[bytes, bytes]:
    fields = {
        b"job_id": f"contract-{pipeline}{'-rerender' if rerender else ''}".encode(),
        b"pipeline": pipeline.encode(),
        b"pipeline_revision": b"gate0-test",
        b"image_key": b"source-image",
        b"source_hash": source_hash.encode(),
        b"title": b"Contract Fixture",
        b"chapter": b"1",
        b"page_number": b"001",
        b"target_lang": b"pt-br",
    }
    if rerender:
        fields[b"rerender_from_metadata"] = b"1"
    return fields


def _process_through_consumer(
    tmp_path, pipeline: str, *, expected_regions: int,
) -> tuple[ProcessingResult, ProcessingResult, dict[str, int]]:
    source = _png_bytes()
    source_hash = hashlib.sha256(source).hexdigest()
    calls: dict[str, int] = {}
    dependencies = _fake_dependencies(pipeline, calls)
    consumer = Consumer(
        "redis://unused",
        cache_dir=str(tmp_path / "cache"),
        pipeline_revision="gate0-test",
        job_dependencies=dependencies,
    )
    consumer._rdb = MagicMock()
    consumer._rdb.get.side_effect = lambda key: (
        source if key == "source-image" else None)
    results: list[ProcessingResult] = []
    consumer._store_result = lambda result, **_kwargs: results.append(result)

    outcome = consumer._process_message(
        STREAM_HIGH, b"1-0", _message_fields(pipeline, source_hash))
    assert outcome == "processed"
    first = results[-1]
    assert first.status == "completed", first.error
    assert first.metadata_payload["schema_version"] == 2
    assert first.metadata_payload["pipeline"] == pipeline
    assert first.metadata_payload["pipeline_revision"] == "gate0-test"
    assert len(first.metadata_payload["regions"]) == expected_regions
    assert first.image_bytes.startswith(b"\x89PNG")
    with Image.open(BytesIO(first.image_bytes)) as rendered:
        assert rendered.size == (120, 160)
        assert rendered.mode == "RGB"
    if pipeline != "book_furigana":
        cleanup_base = first.metadata_payload["cleanup_base"]
        assert cleanup_base["reference"] == f"objects/{cleanup_base['sha256']}"
        assert cleanup_base["encoding"] == "png"
        assert cleanup_base["mode"] == "RGB"
        assert first.artifact_payloads[cleanup_base["sha256"]]
        assert calls.get("legacy_render", 0) == 0

    namespace = f"{pipeline}_pt-br"
    cached = consumer._page_cache.load_metadata_by_hash(
        namespace, source_hash, document_pipeline=pipeline)
    assert cached == first.metadata_payload
    assert consumer._page_cache.load_output_image_by_hash(
        namespace, source_hash) == first.image_bytes

    if pipeline == "book_furigana":
        return first, first, calls

    cached["regions"][0]["user"]["manual_translation"] = "Manual edit"
    consumer._page_cache.update_metadata_by_hash(
        pipeline=namespace,
        document_pipeline=pipeline,
        source_hash=source_hash,
        metadata_payload=cached,
    )
    model_calls = {
        stage: calls.get(stage, 0)
        for stage in ("layout", "detection", "ocr", "translation",
                      "transform", "cleanup")
    }
    outcome = consumer._process_message(
        STREAM_HIGH, b"2-0",
        _message_fields(pipeline, source_hash, rerender=True),
    )
    assert outcome == "processed"
    rerendered = results[-1]
    assert rerendered.status == "completed", rerendered.error
    assert rerendered.metadata_payload["regions"][0]["user"] == {
        "manual_translation": "Manual edit"}
    assert {
        stage: calls.get(stage, 0)
        for stage in ("layout", "detection", "ocr", "translation",
                      "transform", "cleanup")
    } == model_calls
    return first, rerendered, calls


@pytest.mark.parametrize(
    ("pipeline", "expected_regions"),
    [
        ("manga_translate", 2),
        ("manga_furigana", 2),
        ("book_furigana", 1),
        ("webtoon", 7),
    ],
)
def test_worker_message_cache_and_rerender_contract(
    tmp_path, pipeline, expected_regions,
):
    first, rerendered, calls = _process_through_consumer(
        tmp_path, pipeline, expected_regions=expected_regions)

    assert first.content_hash
    assert first.render_hash
    assert calls.get("layout", 0) == (0 if pipeline == "webtoon" else 1)
    assert calls.get("detection", 0) == (
        0 if pipeline == "book_furigana" else 1)
    assert calls.get("ocr", 0) == 1
    assert calls.get("translation", 0) == (
        1 if pipeline in {"manga_translate", "webtoon"} else 0)
    assert calls.get("transform", 0) == (
        1 if pipeline in {"manga_furigana", "book_furigana"} else 0)
    assert calls.get("cleanup", 0) == (
        0 if pipeline == "book_furigana" else 1)
    assert calls.get("render", 0) >= 1
    if pipeline == "book_furigana":
        assert rerendered is first
    else:
        assert rerendered.image_bytes.startswith(b"\x89PNG")
    if pipeline.startswith("manga_"):
        balloon, text = first.metadata_payload["regions"]
        assert balloon["kind"] == "balloon"
        assert balloon["detector_label"] == "bubble"
        assert balloon["polygon"] == [[15, 20], [90, 20], [90, 110], [15, 110]]
        assert balloon["mask"]["encoding"] == "png-mask-l1"
        assert balloon["ocr"]["provenance"]["backend"] == "hayai-ocr"
        assert balloon["ocr"]["confidence"] == 0.8
        assert balloon["ocr"]["routing_reason"] == "hayai_crop_inconsistent"
        assert balloon["ocr"]["alternatives"][0]["provenance"]["backend"] == (
            "manga-ocr"
        )
        assert text["kind"] == "text"
        assert text["parent_id"] == balloon["id"]
        if pipeline == "manga_furigana":
            for payload in (first.metadata_payload,
                            rerendered.metadata_payload):
                rendered_balloon = payload["regions"][0]
                assert rendered_balloon["render"]["font_size"] == 16
                assert rendered_balloon["render"]["line_count"] == 2
                assert rendered_balloon["render"]["fit_status"] == "fit"
                assert rendered_balloon["render"]["ellipsized"] is False
    if pipeline == "webtoon":
        request = calls["translation_request"]
        submitted_ids = {item.id for item in request.items}
        translated_regions = [
            region for region in first.metadata_payload["regions"]
            if region.get("translation") is not None
        ]
        assert submitted_ids == {region["id"] for region in translated_regions}
        assert [item.kind for item in request.items].count("sfx") == 1
        assert next(region for region in translated_regions
                    if region["kind"] == "sfx")["translation"]["text"] == "ESTRONDO"
        semantic_regions = {
            region["detector_label"] + ":" + ",".join(map(str, region["bbox"])):
            region
            for region in first.metadata_payload["regions"]
            if "detector_label" in region
        }
        panel = semantic_regions["panel:0,0,120,155"]
        balloon_1 = semantic_regions["bubble:10,20,100,100"]
        text_1 = semantic_regions["text_bubble:20,30,90,80"]
        balloon_2 = semantic_regions["bubble:15,102,105,150"]
        text_2 = semantic_regions["text_bubble:30,108,90,132"]
        sfx = semantic_regions["text_free:80,5,115,18"]
        assert [
            panel["reading_order"], balloon_1["reading_order"],
            text_1["reading_order"], balloon_2["reading_order"],
            text_2["reading_order"], sfx["reading_order"],
        ] == [0, 1, 2, 3, 4, 5]
        assert text_1["parent_id"] == balloon_1["id"]
        assert text_2["parent_id"] == balloon_2["id"]
        assert balloon_1["mask"]["encoding"] == "png-mask-l1"
        assert balloon_2["mask"]["encoding"] == "png-mask-l1"
        compatibility_regions = [
            region for region in first.metadata_payload["regions"]
            if "detector_label" not in region and region["kind"] == "bubble"
        ]
        assert [region["reading_order"] for region in compatibility_regions] == [6]


def test_manga_batch_uses_final_ids_and_excludes_invalid_preserved_text():
    calls: dict[str, object] = {}
    deps = _fake_dependencies("manga_translate", calls)  # type: ignore[arg-type]

    def detect(page) -> None:
        provenance = Provenance("fake-detector", "test-revision")
        detections = []
        raw = []
        sources = (("一番", True), ("二番", True), ("Kacchan", False), ("…", False))
        for index, (_source, _valid) in enumerate(sources):
            y1 = 10 + index * 30
            balloon_id = f"balloon-{index}"
            detections.extend((
                SemanticDetection(
                    balloon_id, "balloon", BoundingBox(10, y1, 100, y1 + 25),
                    0.9, "bubble", provenance, reading_order=index * 2,
                ),
                SemanticDetection(
                    f"text-{index}", "text", BoundingBox(20, y1 + 3, 90, y1 + 20),
                    0.8, "text_bubble", provenance, parent_id=balloon_id,
                    reading_order=index * 2 + 1,
                ),
            ))
            raw.append({
                "bbox": (10, y1, 100, y1 + 25), "semantic_id": balloon_id,
                "source": _source, "valid": _valid,
            })
        page.semantic_detections = detections
        page.bubbles_raw = raw

    def ocr(_image, raw) -> BubbleResult:
        return BubbleResult(
            bbox=raw["bbox"], ocr_text=raw["source"], is_valid=raw["valid"],
        )

    def page_translate(request) -> PageTranslationResult:
        calls["request"] = request
        return PageTranslationResult(tuple(
            TranslationValue(item.id, f"translated:{item.source_text}")
            for item in reversed(request.items)
        ), batch_attempts=1)

    result = process_job(ProcessingJob(
        job_id="stable-translation-ids", pipeline="manga_translate",
        image_bytes=_png_bytes(), source_hash="b" * 64, target_lang="en",
    ), dependencies=replace(
        deps, detect_manga=detect, ocr_manga=ocr, translate_page=page_translate,
    ))

    assert result.status == "completed", result.error
    request = calls["request"]
    assert [item.source_text for item in request.items] == ["一番", "二番"]
    regions = result.metadata_payload["regions"]
    translated = [region for region in regions if region.get("translation")]
    assert [item.id for item in request.items] == [region["id"] for region in translated]
    preserved = {region["ocr"]["text"]: region.get("translation")
                 for region in regions if region.get("ocr")}
    assert preserved["Kacchan"] is None
    assert preserved["…"] is None


def test_artwork_text_uses_cleanup_mask_and_specialized_document_renderer():
    calls: dict[str, object] = {}
    deps = _fake_dependencies("manga_translate", calls)  # type: ignore[arg-type]

    def detect(page) -> None:
        provenance = Provenance("fake-detector", "test-revision")
        page.semantic_detections = [SemanticDetection(
            "artwork", "text", BoundingBox(20, 30, 90, 100),
            0.9, "text_free", provenance, reading_order=0,
        )]
        page.bubbles_raw = [{
            "bbox": (20, 30, 90, 100), "semantic_id": "artwork",
            "is_artwork": True,
        }]

    def ocr(_image, raw) -> BubbleResult:
        return BubbleResult(
            bbox=raw["bbox"], ocr_text="ドン", is_valid=True,
            is_artwork_text=True,
        )

    def cleanup(page, _mode) -> None:
        page.output_img = page.img_pil.copy()
        page.output_img.paste("white", (20, 30, 90, 100))
        page.render_plan = SimpleNamespace(base_font_size=18, font_sizes={0: 18})
        mask = np.zeros((160, 120), dtype=np.uint8)
        mask[30:100, 20:90] = 255
        page.bubble_results[0].cleanup_mask = mask
        page.bubble_results[0].cleanup_method = "region_fill"

    result = process_job(ProcessingJob(
        job_id="artwork-document-render", pipeline="manga_translate",
        image_bytes=_png_bytes(), source_hash="f" * 64, target_lang="en",
    ), dependencies=replace(
        deps, detect_manga=detect, ocr_manga=ocr, cleanup_manga=cleanup,
    ))

    assert result.status == "completed", result.error
    assert calls["artwork_inpainted"] is True
    region = result.metadata_payload["regions"][0]
    assert region["kind"] == "artwork_text"
    assert region["cleanup"]["method"] == "region_fill"
    assert region["cleanup"]["mask"]["encoding"] == "png-mask-l1"
    assert region["render"]["fit_status"] == "fit"


def test_incomplete_page_result_fails_before_cleanup_or_render():
    calls: dict[str, int] = {}
    deps = _fake_dependencies("manga_translate", calls)
    result = process_job(ProcessingJob(
        job_id="incomplete-translation", pipeline="manga_translate",
        image_bytes=_png_bytes(), source_hash="c" * 64,
    ), dependencies=replace(
        deps,
        translate_page=lambda _request: PageTranslationResult((), 1),
    ))

    assert result.status == "failed"
    assert "incomplete IDs" in result.error
    assert calls.get("cleanup", 0) == 0
    assert calls.get("render", 0) == 0


def test_ineligible_webtoon_sfx_has_no_translation_or_provenance():
    calls: dict[str, object] = {}
    deps = _fake_dependencies("webtoon", calls)  # type: ignore[arg-type]
    original_ocr = deps.ocr_webtoon
    original_prepare = deps.prepare_webtoon_translation

    def ocr(page) -> None:
        original_ocr(page)
        page.sfx_detections[0].text = "..."

    def prepare(page) -> None:
        original_prepare(page)
        page.sfx_translation_eligible = [False]

    result = process_job(ProcessingJob(
        job_id="ineligible-sfx", pipeline="webtoon",
        image_bytes=_png_bytes(), source_hash="d" * 64, target_lang="pt-br",
    ), dependencies=replace(
        deps, ocr_webtoon=ocr, prepare_webtoon_translation=prepare,
    ))

    assert result.status == "completed", result.error
    request = calls["translation_request"]
    assert all(item.kind != "sfx" for item in request.items)
    sfx = next(region for region in result.metadata_payload["regions"]
               if region["kind"] == "sfx")
    assert sfx.get("translation") is None


def test_degraded_manga_ocr_result_is_returned_but_not_cached(tmp_path):
    source = _png_bytes()
    source_hash = hashlib.sha256(source).hexdigest()
    calls: dict[str, int] = {}
    deps = _fake_dependencies("manga_translate", calls)

    def degraded_ocr(_image, bubble):
        return BubbleResult(
            bbox=bubble["bbox"],
            ocr_text="第2話",
            is_valid=True,
            ocr_provenance=Provenance("manga-ocr", "manga-test"),
            ocr_routing_reason="hayai_numeric:hayai_timeout_fallback",
            ocr_degraded=True,
        )

    deps = replace(deps, ocr_manga=degraded_ocr)
    consumer = Consumer(
        "redis://unused",
        cache_dir=str(tmp_path / "cache"),
        pipeline_revision="gate0-test",
        job_dependencies=deps,
    )
    consumer._rdb = MagicMock()
    consumer._rdb.get.side_effect = lambda key: (
        source if key == "source-image" else None
    )
    results = []
    consumer._store_result = lambda result, **_kwargs: results.append(result)

    outcome = consumer._process_message(
        STREAM_HIGH,
        b"3-0",
        _message_fields("manga_translate", source_hash),
    )

    assert outcome == "processed"
    assert results[-1].status == "completed"
    assert results[-1].cacheable is False
    assert results[-1].metadata_payload["regions"][0]["ocr"][
        "routing_reason"
    ] == "hayai_numeric:hayai_timeout_fallback"
    assert consumer._page_cache.load_metadata_by_hash(
        "manga_translate_pt-br",
        source_hash,
        document_pipeline="manga_translate",
    ) is None


@pytest.mark.parametrize("pipeline", ["book_furigana", "webtoon"])
def test_non_manga_ocr_paths_never_invoke_hayai_stage(pipeline):
    calls: dict[str, int] = {}
    deps = _fake_dependencies(pipeline, calls)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("manga/Hayai OCR must not run")

    deps = replace(deps, ocr_manga=forbidden)
    result = process_job(ProcessingJob(
        job_id=f"no-hayai-{pipeline}",
        pipeline=pipeline,
        image_bytes=_png_bytes(),
        source_hash="a" * 64,
        target_lang="pt-br",
    ), dependencies=deps)

    assert result.status == "completed", result.error


def test_tall_webtoon_stitches_semantics_before_page_document_mapping():
    source = _png_bytes(width=120, height=4100)
    calls: dict[str, int] = {}
    deps = _fake_dependencies("webtoon", calls)

    def create_page(img_cv, img_pil, name):
        return WebtoonPageResult(
            image_path="", name=name, img_cv=img_cv, img_pil=img_pil,
        )

    def recognize(page):
        raw = page.raw_detections[0]
        bubble = SimpleNamespace(
            bbox=raw["bbox"], combined_text="안녕하세요",
            semantic_id=raw["semantic_id"],
        )
        page.regions = [SimpleNamespace(
            bubble=bubble, is_valid=True, english="Hello",
        )]
        page.sfx_detections = []

    deps = replace(
        deps,
        create_webtoon_page=create_page,
        detect_webtoon=detect_bubbles_rtdetr,
        ocr_webtoon=recognize,
    )
    provenance = Provenance("rtdetr-v2", "weights")
    strip_results = [
        [
            SemanticDetection(
                "balloon-low", "balloon", BoundingBox(10, 1950, 100, 1995),
                0.85, "bubble", provenance, reading_order=0,
            ),
            SemanticDetection(
                "text-low", "text", BoundingBox(20, 1960, 80, 1980),
                0.86, "text_bubble", provenance,
                polygon=Polygon(((20, 1960), (80, 1960), (80, 1980))),
                parent_id="balloon-low", reading_order=1,
            ),
        ],
        [
            SemanticDetection(
                "balloon-high", "balloon", BoundingBox(10, 50, 100, 95),
                0.95, "bubble", provenance, reading_order=0,
            ),
            SemanticDetection(
                "text-high", "text", BoundingBox(20, 60, 80, 80),
                0.96, "text_bubble", provenance,
                polygon=Polygon(((20, 60), (80, 60), (80, 80))),
                parent_id="balloon-high", reading_order=1,
            ),
        ],
        [],
    ]

    def mask_for_page(image, bbox):
        mask = np.zeros(image.shape[:2], dtype=np.uint8)
        x1, y1, x2, y2 = bbox
        mask[y1:y2, x1:x2] = 255
        return mask, (255, 255, 255), True

    with (
        patch("webtoon.processor.rtdetr_detect", side_effect=strip_results) as detector,
        patch("webtoon.processor.extract_bubble_mask", side_effect=mask_for_page),
    ):
            result = process_job(ProcessingJob(
                job_id="tall-webtoon", pipeline="webtoon", image_bytes=source,
                target_lang="pt-br", pipeline_revision="tall-test",
            ), dependencies=deps)

    assert result.status == "completed", result.error
    assert detector.call_count == 3
    assert [call.args[0].shape[0] for call in detector.call_args_list] == [2000, 2000, 300]
    regions = {
        region["kind"]: region for region in result.metadata_payload["regions"]
    }
    assert set(regions) == {"balloon", "text"}
    assert regions["balloon"]["bbox"] == [10, 1950, 100, 1995]
    assert regions["balloon"]["mask"]["reference"].startswith("objects/")
    assert regions["balloon"]["mask"]["encoding"] == "png-mask-l1"
    assert regions["balloon"]["mask"]["width"] == 120
    assert regions["balloon"]["mask"]["height"] == 4100
    assert regions["text"]["polygon"] == [
        [20, 1960], [80, 1960], [80, 1980],
    ]
    assert regions["text"]["parent_id"] == regions["balloon"]["id"]
    assert [regions["balloon"]["reading_order"],
            regions["text"]["reading_order"]] == [0, 1]
