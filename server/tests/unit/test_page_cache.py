"""Unit tests for worker.page_cache.PageCache (v2 cache)."""

from __future__ import annotations

import json

import pytest

from worker.page_cache import (
    CACHE_MANIFEST_SCHEMA_VERSION,
    LEGACY_PIPELINE_REVISION,
    PageCache,
)


def _png_like(seed: int, size: int = 128) -> bytes:
    # Deterministic bytes are enough for cache hashing tests.
    return (b"\x89PNG\r\n\x1a\n" + bytes([seed % 251]) * size)


def _page_document_payload(pipeline: str, source_hash: str,
                           revision: str) -> dict:
    return {
        "schema_version": 2,
        "page_id": f"{pipeline}:{source_hash[:12]}",
        "pipeline": pipeline,
        "pipeline_revision": revision,
        "source_hash": source_hash,
        "image": {"width": 1200, "height": 1800},
        "regions": [],
        "stage_provenance": {},
    }


def test_store_and_load_roundtrip(tmp_path):
    cache = PageCache(str(tmp_path))
    src = _png_like(1)
    rendered = _png_like(2)
    source_hash = cache._hash_bytes(src)
    metadata = {
        "schema_version": 1,
        "regions": [{"id": "r1", "bbox": [1, 2, 3, 4]}],
    }

    manifest = cache.store_page(
        pipeline="manga_furigana",
        source_hash=source_hash,
        source_image_bytes=src,
        rendered_image_bytes=rendered,
        metadata_payload=metadata,
        title="One Piece",
        chapter="1084",
        page_number="003",
    )

    assert manifest["source_hash"] == source_hash
    assert manifest["schema_version"] == CACHE_MANIFEST_SCHEMA_VERSION
    assert manifest["image_stale"] is False

    got_img = cache.load_output_image_by_hash("manga_furigana", source_hash)
    got_src = cache.load_source_image_by_hash("manga_furigana", source_hash)
    got_meta = cache.load_metadata_by_hash("manga_furigana", source_hash)

    assert got_img == rendered
    assert got_src == src
    assert got_meta == metadata

    resolved = cache.resolve_source_hash(
        "manga_furigana", "one-piece", "1084", "003")
    assert resolved == source_hash


def test_update_metadata_marks_stale(tmp_path):
    cache = PageCache(str(tmp_path))
    src = _png_like(3)
    rendered = _png_like(4)
    source_hash = cache._hash_bytes(src)

    first = cache.store_page(
        pipeline="webtoon",
        source_hash=source_hash,
        source_image_bytes=src,
        rendered_image_bytes=rendered,
        metadata_payload={"schema_version": 1, "regions": []},
    )

    old_content_hash = first["content_hash"]
    updated = cache.update_metadata_by_hash(
        pipeline="webtoon",
        source_hash=source_hash,
        metadata_payload={"schema_version": 1, "regions": [{"id": "u1"}]},
        base_content_hash=old_content_hash,
    )

    assert updated["image_stale"] is True
    assert updated["content_hash"] != old_content_hash
    # Stale image should not be served until rerender writes a fresh manifest.
    assert cache.load_output_image_by_hash("webtoon", source_hash) is None


def test_update_metadata_rejects_hash_conflict(tmp_path):
    cache = PageCache(str(tmp_path))
    src = _png_like(5)
    rendered = _png_like(6)
    source_hash = cache._hash_bytes(src)

    cache.store_page(
        pipeline="manga_translate",
        source_hash=source_hash,
        source_image_bytes=src,
        rendered_image_bytes=rendered,
        metadata_payload={"schema_version": 1, "regions": []},
    )

    with pytest.raises(ValueError, match="content hash mismatch"):
        cache.update_metadata_by_hash(
            pipeline="manga_translate",
            source_hash=source_hash,
            metadata_payload={"schema_version": 1, "regions": []},
            base_content_hash="deadbeef",
        )


def test_store_rejects_source_hash_mismatch(tmp_path):
    cache = PageCache(str(tmp_path))
    src = _png_like(9)
    rendered = _png_like(10)

    with pytest.raises(ValueError, match="source hash mismatch"):
        cache.store_page(
            pipeline="webtoon",
            source_hash="0" * 64,
            source_image_bytes=src,
            rendered_image_bytes=rendered,
            metadata_payload={"schema_version": 1, "regions": []},
        )


def test_metadata_canonicalization_stable(tmp_path):
    cache = PageCache(str(tmp_path))
    a = {"b": 2, "a": 1}
    b = {"a": 1, "b": 2}
    assert cache._canonical_json_bytes(a) == cache._canonical_json_bytes(b)
    assert json.loads(cache._canonical_json_bytes(a).decode("utf-8")) == a


def test_store_rejects_invalid_pipeline(tmp_path):
    cache = PageCache(str(tmp_path))
    src = _png_like(11)
    rendered = _png_like(12)

    with pytest.raises(ValueError, match="invalid cache key"):
        cache.store_page(
            pipeline="../escape",
            source_hash=cache._hash_bytes(src),
            source_image_bytes=src,
            rendered_image_bytes=rendered,
            metadata_payload={"schema_version": 1, "regions": []},
        )


def test_resolve_source_hash_rejects_unsafe_components(tmp_path):
    cache = PageCache(str(tmp_path))
    assert cache.resolve_source_hash(
        "manga_translate",
        "one-piece",
        "../../etc",
        "003",
    ) is None


def test_store_writes_current_metadata_schema_and_pipeline_revision(tmp_path):
    cache = PageCache(str(tmp_path), pipeline_revision="detector-a.ocr-b")
    src = _png_like(13)
    source_hash = cache._hash_bytes(src)

    manifest = cache.store_page(
        pipeline="manga_translate",
        source_hash=source_hash,
        source_image_bytes=src,
        rendered_image_bytes=_png_like(14),
        metadata_payload=_page_document_payload(
            "manga_translate", source_hash, "detector-a.ocr-b"),
    )

    assert manifest["pipeline_revision"] == "detector-a.ocr-b"
    assert cache.load_metadata_by_hash(
        "manga_translate", source_hash
    ) == _page_document_payload(
        "manga_translate", source_hash, "detector-a.ocr-b")


def test_changed_pipeline_revision_is_a_cache_miss(tmp_path):
    old_cache = PageCache(str(tmp_path), pipeline_revision="rev-a")
    src = _png_like(15)
    source_hash = old_cache._hash_bytes(src)
    old_cache.store_page(
        pipeline="webtoon",
        source_hash=source_hash,
        source_image_bytes=src,
        rendered_image_bytes=_png_like(16),
        metadata_payload={"schema_version": 1, "regions": []},
    )

    new_cache = PageCache(str(tmp_path), pipeline_revision="rev-b")
    assert new_cache.load_manifest_by_hash("webtoon", source_hash) is None
    assert new_cache.load_output_image_by_hash("webtoon", source_hash) is None
    assert new_cache.load_metadata_by_hash("webtoon", source_hash) is None


def test_pipeline_revision_rejects_mismatched_environment_name(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_REVISION", "current-env")

    with pytest.raises(ValueError, match="PIPELINE_REVISION mismatch"):
        PageCache(str(tmp_path))


def test_legacy_manifest_and_metadata_remain_readable(tmp_path):
    cache = PageCache(
        str(tmp_path), pipeline_revision=LEGACY_PIPELINE_REVISION)
    src = _png_like(17)
    source_hash = cache._hash_bytes(src)
    cache.store_page(
        pipeline="manga_furigana",
        source_hash=source_hash,
        source_image_bytes=src,
        rendered_image_bytes=_png_like(18),
        metadata_payload={"schema_version": 1, "regions": [{"id": "r1"}]},
    )

    manifest_path = cache._manifest_path("manga_furigana", source_hash)
    with open(manifest_path, encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
    manifest.pop("pipeline_revision")
    manifest["schema_version"] = 2
    metadata_sha = manifest["metadata_object_sha256"]
    legacy_metadata = {
        "schema_version": 1,
        "pipeline": "manga_furigana",
        "source_hash": source_hash,
        "regions": [{"id": "r1"}],
    }
    legacy_bytes = cache._canonical_json_bytes(legacy_metadata)
    legacy_sha, legacy_size = cache._store_object(legacy_bytes)
    manifest["metadata_object_sha256"] = legacy_sha
    manifest["metadata_bytes"] = legacy_size
    cache._atomic_write(manifest_path, json.dumps(manifest).encode("utf-8"))

    assert cache.load_manifest_by_hash("manga_furigana", source_hash) is not None
    loaded = cache.load_metadata_by_hash("manga_furigana", source_hash)
    assert loaded == legacy_metadata
    assert metadata_sha != legacy_sha


def test_current_reader_rejects_future_metadata_schema(tmp_path):
    cache = PageCache(str(tmp_path), pipeline_revision="rev-a")
    src = _png_like(19)

    with pytest.raises(ValueError, match="unsupported metadata schema version"):
        cache.store_page(
            pipeline="webtoon",
            source_hash=cache._hash_bytes(src),
            source_image_bytes=src,
            rendered_image_bytes=_png_like(20),
            metadata_payload={"schema_version": 3, "regions": []},
        )


def test_metadata_cannot_claim_a_different_revision(tmp_path):
    cache = PageCache(str(tmp_path), pipeline_revision="rev-a")
    src = _png_like(21)

    with pytest.raises(ValueError, match="metadata pipeline revision mismatch"):
        cache.store_page(
            pipeline="webtoon",
            source_hash=cache._hash_bytes(src),
            source_image_bytes=src,
            rendered_image_bytes=_png_like(22),
            metadata_payload=_page_document_payload(
                "webtoon", cache._hash_bytes(src), "rev-b"),
        )


def test_legacy_metadata_cannot_claim_a_different_revision(tmp_path):
    cache = PageCache(str(tmp_path), pipeline_revision="rev-a")
    src = _png_like(27)

    with pytest.raises(ValueError, match="metadata pipeline revision mismatch"):
        cache.store_page(
            pipeline="webtoon",
            source_hash=cache._hash_bytes(src),
            source_image_bytes=src,
            rendered_image_bytes=_png_like(28),
            metadata_payload={
                "schema_version": 1,
                "pipeline_revision": "rev-b",
                "regions": [],
            },
        )


@pytest.mark.parametrize("revision", ["", "../rev", "rev with spaces"])
def test_rejects_invalid_pipeline_revision(tmp_path, revision):
    with pytest.raises(ValueError, match="invalid pipeline revision"):
        PageCache(str(tmp_path), pipeline_revision=revision)


def test_metadata_update_promotes_legacy_manifest(tmp_path):
    cache = PageCache(
        str(tmp_path), pipeline_revision=LEGACY_PIPELINE_REVISION)
    src = _png_like(23)
    source_hash = cache._hash_bytes(src)
    cache.store_page(
        pipeline="webtoon",
        source_hash=source_hash,
        source_image_bytes=src,
        rendered_image_bytes=_png_like(24),
        metadata_payload={"schema_version": 1, "regions": []},
    )
    manifest_path = cache._manifest_path("webtoon", source_hash)
    with open(manifest_path, encoding="utf-8") as manifest_file:
        legacy_manifest = json.load(manifest_file)
    legacy_manifest["schema_version"] = 2
    legacy_manifest.pop("pipeline_revision")
    cache._atomic_write(
        manifest_path, json.dumps(legacy_manifest).encode("utf-8"))

    updated = cache.update_metadata_by_hash(
        pipeline="webtoon",
        source_hash=source_hash,
        metadata_payload={"schema_version": 1, "regions": [{"id": "r1"}]},
    )

    assert updated["schema_version"] == CACHE_MANIFEST_SCHEMA_VERSION
    assert updated["pipeline_revision"] == LEGACY_PIPELINE_REVISION


def test_rejects_unknown_manifest_schema(tmp_path):
    cache = PageCache(str(tmp_path))
    src = _png_like(25)
    source_hash = cache._hash_bytes(src)
    cache.store_page(
        pipeline="webtoon",
        source_hash=source_hash,
        source_image_bytes=src,
        rendered_image_bytes=_png_like(26),
        metadata_payload={"schema_version": 1, "regions": []},
    )
    manifest_path = cache._manifest_path("webtoon", source_hash)
    with open(manifest_path, encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
    manifest["schema_version"] = 99
    cache._atomic_write(manifest_path, json.dumps(manifest).encode("utf-8"))

    assert cache.load_manifest_by_hash("webtoon", source_hash) is None
