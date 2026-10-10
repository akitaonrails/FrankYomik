#!/usr/bin/env python3
"""Model-free structural checks against production document rendering."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

REPOSITORY = Path(__file__).resolve().parents[3]
SERVER = REPOSITORY / "server"
sys.path.insert(0, str(SERVER))

from kindle.book_layout import PageLayout, TextColumn  # noqa: E402
from kindle.book_processor import ColumnReading, Ruby  # noqa: E402
from kindle.book_renderer import render as render_book  # noqa: E402
from pipeline.artifacts import (  # noqa: E402
    encode_png, resolve_artifact, store_image_artifact, store_mask_artifact,
)
from pipeline.document import (  # noqa: E402
    BoundingBox, CleanupMetadata, PageDocument, PageRegion, Provenance,
    RenderMetadata, TransformResult, TranslationResult, UserEdit,
)
from pipeline.revision import DEFAULT_PIPELINE_REVISION  # noqa: E402
from worker.job import ProcessingJob, process_job, production_dependencies  # noqa: E402


WIDTH, HEIGHT = 320, 240
PROVENANCE = Provenance("phase5-fixture", "1")


def png_observation(payload: bytes) -> tuple[Image.Image, dict[str, object]]:
    with Image.open(BytesIO(payload)) as loaded:
        loaded.load()
        image = loaded.convert("RGB")
        source_mode = loaded.mode
    return image, {
        "dimensions": list(image.size), "mode": source_mode,
        "png_signature": payload.startswith(b"\x89PNG\r\n\x1a\n"),
        "pixel_sha256_observation": hashlib.sha256(
            np.asarray(image).tobytes()).hexdigest(),
    }


def source_image() -> Image.Image:
    yy, xx = np.indices((HEIGHT, WIDTH), dtype=np.uint16)
    pixels = np.stack((xx % 251, yy % 241, (xx + yy) % 239), axis=2)
    return Image.fromarray(pixels.astype(np.uint8), "RGB")


def rectangle_mask(bbox: tuple[int, int, int, int]) -> np.ndarray:
    mask = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    x1, y1, x2, y2 = bbox
    mask[y1:y2, x1:x2] = 255
    return mask


def forbidden(name: str):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"metadata rerender invoked forbidden stage: {name}")
    return fail


def dependencies(order: list[str]):
    base = production_dependencies()

    def manga_render(*args, **kwargs):
        order.append(str(args[2]))
        return base.render_translation(*args, **kwargs)

    def webtoon_render(*args, **kwargs):
        order.append(str(args[3]))
        return base.render_webtoon_document_region(*args, **kwargs)

    blocked = {
        name: forbidden(name) for name in (
            "analyze_layout", "read_columns", "render_book", "extract_manga_mask",
            "annotate", "clear_region", "clear_strokes", "ocr_prose",
            "create_manga_page", "detect_manga", "ocr_manga", "cleanup_manga",
            "render_manga", "transform_furigana", "translate_text",
            "translate_page", "estimate_source_font", "pipeline_mode",
            "create_webtoon_page", "detect_webtoon", "ocr_webtoon",
            "translate_webtoon", "prepare_webtoon_translation",
            "cleanup_webtoon", "render_webtoon",
        )
    }
    return replace(
        base, **blocked, render_translation=manga_render,
        render_webtoon_document_region=webtoon_render,
    )


def document_fixture(pipeline: str) -> tuple[bytes, dict[str, bytes], PageDocument]:
    source = source_image()
    source_bytes = encode_png(source, "RGB")
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    artifacts: dict[str, bytes] = {}
    cleanup_base = source.copy()
    boxes = ((35, 35, 145, 115), (170, 125, 290, 210))
    regions = []
    for index, bbox in enumerate(boxes):
        mask = rectangle_mask(bbox)
        # The cleanup base is allowed to differ from source only through the
        # stored cleanup masks. This exercises the artifact integrity boundary.
        crop = np.asarray(source.crop(bbox), dtype=np.uint8)
        surface = tuple(int(value) for value in np.median(crop, axis=(0, 1)))
        cleanup_base.paste(surface, bbox)
        mask_reference = store_mask_artifact(mask, artifacts)
        regions.append(PageRegion(
            id=f"region-{index}", kind="balloon", bbox=BoundingBox(*bbox),
            # Deliberately reverse tuple order below; renderer must sort these.
            reading_order=index, mask=mask_reference,
            translation=TranslationResult(
                f"{'First' if index == 0 else 'Second'} region",
                "en", PROVENANCE, source_language="ja" if pipeline.startswith("manga") else "ko",
            ),
            cleanup=CleanupMetadata("fixture", PROVENANCE, mask_reference),
            render=RenderMetadata(BoundingBox(*bbox), PROVENANCE, font_size=22),
        ))
    cleanup_reference = store_image_artifact(cleanup_base, artifacts)
    document = PageDocument(
        page_id=f"phase5-{pipeline}", pipeline=pipeline,
        pipeline_revision=DEFAULT_PIPELINE_REVISION, source_hash=source_hash,
        width=WIDTH, height=HEIGHT, cleanup_base=cleanup_reference,
        regions=tuple(reversed(regions)), stage_provenance={},
    )
    return source_bytes, artifacts, document


def run_document(
    pipeline: str, source_bytes: bytes, artifacts: dict[str, bytes],
    document: PageDocument,
) -> tuple[Image.Image, dict[str, object], list[str], dict[str, object]]:
    order: list[str] = []
    result = process_job(ProcessingJob(
        job_id=f"phase5-{pipeline}", pipeline=pipeline, image_bytes=source_bytes,
        source_hash=document.source_hash, rerender_from_metadata=True,
        metadata_payload=document.to_dict(), artifact_payloads=artifacts,
        pipeline_revision=DEFAULT_PIPELINE_REVISION,
    ), dependencies=dependencies(order))
    if result.status != "completed" or result.image_bytes is None:
        raise AssertionError(result.error or "document rerender failed")
    if result.metadata_payload is None:
        raise AssertionError("document rerender omitted updated metadata")
    image, observation = png_observation(result.image_bytes)
    observation["bubble_count"] = result.bubble_count
    return image, observation, order, result.metadata_payload


def document_case(
    pipeline: str, output_dir: Path | None = None,
) -> dict[str, object]:
    source_bytes, artifacts, document = document_fixture(pipeline)
    first, observation, first_order, _ = run_document(
        pipeline, source_bytes, artifacts, document)
    second, _, second_order, _ = run_document(
        pipeline, source_bytes, artifacts, document)
    assert np.array_equal(np.asarray(first), np.asarray(second))
    assert first_order == ["First region", "Second region"]
    assert second_order == first_order

    source, _ = png_observation(source_bytes)
    allowed = np.zeros((HEIGHT, WIDTH), dtype=bool)
    for region in document.regions:
        x1, y1, x2, y2 = region.bbox.as_list()
        allowed[y1:y2, x1:x2] = True
    source_delta = np.any(np.asarray(first) != np.asarray(source), axis=2)
    assert source_delta.any() and not source_delta[~allowed].any()

    selected = next(region for region in document.regions if region.id == "region-0")
    edited_regions = tuple(
        replace(region, user=UserEdit("Edited region"))
        if region.id == selected.id else region
        for region in document.regions
    )
    edited, _, edit_order, _ = run_document(
        pipeline, source_bytes, artifacts, replace(document, regions=edited_regions))
    edit_delta = np.any(np.asarray(first) != np.asarray(edited), axis=2)
    selected_mask = rectangle_mask(tuple(selected.bbox.as_list())) > 0
    assert edit_delta.any() and not edit_delta[~selected_mask].any()
    assert edit_order == ["Edited region", "Second region"]
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        source.save(output_dir / f"{pipeline}-source.png", format="PNG")
        first.save(output_dir / f"{pipeline}-render.png", format="PNG")
        edited.save(output_dir / f"{pipeline}-edited.png", format="PNG")

    observation.update({
        "changed_pixels": int(source_delta.sum()),
        "changed_outside_regions": int(source_delta[~allowed].sum()),
        "deterministic_rerender": True,
        "reading_order": first_order,
        "manual_edit_order": edit_order,
        "manual_edit_changed_pixels": int(edit_delta.sum()),
        "manual_edit_changed_outside_selected_region": int(
            edit_delta[~selected_mask].sum()),
        "forbidden_stage_calls": 0,
    })
    return observation


def fit_metadata_case(pipeline: str) -> dict[str, object]:
    source_bytes, artifacts, document = document_fixture(pipeline)
    long_text = " ".join(["deliberately long translated dialogue"] * 35)
    regions = tuple(
        replace(
            region,
            translation=replace(region.translation, text=long_text),
        ) if region.id == "region-0" else region
        for region in document.regions
    )
    image, _, _, payload = run_document(
        pipeline, source_bytes, artifacts, replace(document, regions=regions))
    rendered = next(
        region["render"] for region in payload["regions"]
        if region["id"] == "region-0"
    )
    expected = "ellipsized" if pipeline == "webtoon" else "overflow"
    assert rendered["fit_status"] == expected
    assert rendered["ellipsized"] is (pipeline == "webtoon")
    assert rendered["font_size"] > 0 and rendered["line_count"] > 0
    cleanup_base = resolve_artifact(
        document.cleanup_base, artifacts, mode="RGB")
    delta = np.any(np.asarray(image) != np.asarray(cleanup_base), axis=2)
    allowed = np.zeros((HEIGHT, WIDTH), dtype=bool)
    for region in document.regions:
        x1, y1, x2, y2 = region.bbox.as_list()
        allowed[y1:y2, x1:x2] = True
    assert not delta[~allowed].any()
    return {
        "fit_status": rendered["fit_status"],
        "ellipsized": rendered["ellipsized"],
        "font_size": rendered["font_size"],
        "line_count": rendered["line_count"],
        "changed_outside_regions": int(delta[~allowed].sum()),
    }


def furigana_containment_case() -> dict[str, object]:
    source_bytes, artifacts, document = document_fixture("manga_furigana")
    long_text = "長い日本語文章" * 12
    regions = tuple(
        replace(
            region,
            translation=None,
            transform=TransformResult(
                "furigana",
                [{
                    "text": long_text if region.id == "region-0" else "日本語",
                    "furigana": (
                        "ながいにほんごぶんしょう" * 12
                        if region.id == "region-0" else "にほんご"
                    ),
                    "needs_furigana": True,
                }],
                PROVENANCE,
            ),
            source_font_size=16,
            render=replace(region.render, vertical=True, orientation="vertical-rl"),
        )
        for region in document.regions
    )
    document = replace(document, regions=regions)
    image, _, _, payload = run_document(
        "manga_furigana", source_bytes, artifacts, document)
    cleanup_base = resolve_artifact(document.cleanup_base, artifacts, mode="RGB")
    delta = np.any(np.asarray(image) != np.asarray(cleanup_base), axis=2)
    allowed = np.zeros((HEIGHT, WIDTH), dtype=bool)
    for region in document.regions:
        x1, y1, x2, y2 = region.bbox.as_list()
        allowed[y1:y2, x1:x2] = True
    assert delta.any() and not delta[~allowed].any()
    rendered = next(
        region["render"] for region in payload["regions"]
        if region["id"] == "region-0"
    )
    assert rendered["fit_status"] == "overflow"
    assert rendered["ellipsized"] is False
    assert rendered["font_size"] > 0 and rendered["line_count"] > 0
    return {
        "fit_status": rendered["fit_status"],
        "ellipsized": rendered["ellipsized"],
        "font_size": rendered["font_size"],
        "line_count": rendered["line_count"],
        "changed_pixels": int(delta.sum()),
        "changed_outside_regions": int(delta[~allowed].sum()),
        "forbidden_stage_calls": 0,
    }


def book_case(output_dir: Path | None = None) -> dict[str, object]:
    original = Image.new("RGB", (300, 400), (0, 0, 0))
    draw = ImageDraw.Draw(original)
    for index in range(8):
        draw.rectangle((100, 20 + index * 40, 130, 50 + index * 40),
                       fill=(200, 200, 200))
    column = TextColumn(100, 130, 20, 340, runs=((20, 340),))
    layout = PageLayout(
        columns=(column,), dark_background=True, ink_color=(200, 200, 200),
        width=300, height=400,
    )
    readings = (ColumnReading(
        index=0, column=column, chunks=(), text="",
        ruby=(Ruby("かぜ", 20, 60),),
    ),)
    first = render_book(original, layout, readings)
    second = render_book(original, layout, readings)
    delta = np.any(np.asarray(original) != np.asarray(first), axis=2)
    assert delta.any() and not delta[:, :100].any() and not delta[:, 175:].any()
    assert np.array_equal(np.asarray(original.crop((100, 20, 130, 340))),
                          np.asarray(first.crop((100, 20, 130, 340))))
    assert np.array_equal(np.asarray(first), np.asarray(second))
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        original.save(output_dir / "book-source.png", format="PNG")
        first.save(output_dir / "book-render.png", format="PNG")
    payload = encode_png(first, "RGB")
    _, observation = png_observation(payload)
    observation.update({
        "changed_pixels": int(delta.sum()), "base_text_column_unchanged": True,
        "changes_confined_to_gutter_band": True, "deterministic_rerender": True,
    })
    return observation


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--visual-output-dir", type=Path,
        help="optional ignored directory for human-review PNGs",
    )
    args = parser.parse_args()
    print(json.dumps({
        "schema_version": 1,
        "pipeline_revision": DEFAULT_PIPELINE_REVISION,
        "method": "production PageDocument rerender structural invariants",
        "cross_platform_pixel_hash_golden": False,
        "cases": {
            "manga": document_case("manga_translate", args.visual_output_dir),
            "webtoon": document_case("webtoon", args.visual_output_dir),
            "book": book_case(args.visual_output_dir),
        },
        "forced_fit_metadata": {
            "manga": fit_metadata_case("manga_translate"),
            "webtoon": fit_metadata_case("webtoon"),
            "furigana": furigana_containment_case(),
        },
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
