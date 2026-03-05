"""Unit tests for worker.page_cache.PageCache (v2 cache)."""

from __future__ import annotations

import json

import pytest

from worker.page_cache import PageCache


def _png_like(seed: int, size: int = 128) -> bytes:
    # Deterministic bytes are enough for cache hashing tests.
    return (b"\x89PNG\r\n\x1a\n" + bytes([seed % 251]) * size)


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
