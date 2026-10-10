"""Robust filesystem page cache (v2): content-addressed objects + manifests.

Layout:
  <cache>/v2/objects/aa/bb/<sha256>
  <cache>/v2/pages/by-hash/<pipeline>/<source_hash>/manifest.json
  <cache>/v2/pages/by-ref/<pipeline>/<title_slug>/<chapter>/<page>.json

Manifest is authoritative. Objects are hash-verified on read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from copy import deepcopy
from decimal import Decimal
from typing import Any

from pipeline.document import (
    PAGE_DOCUMENT_SCHEMA_VERSION,
    PageDocument,
    canonical_json_bytes,
)
from pipeline.revision import (
    LEGACY_PIPELINE_REVISION,
    get_pipeline_revision,
    validate_pipeline_revision,
)

_SLUG_RE = re.compile(r"[^a-z0-9\-]")
_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

CACHE_MANIFEST_SCHEMA_VERSION = 3
METADATA_SCHEMA_VERSION = PAGE_DOCUMENT_SCHEMA_VERSION


class PageCache:
    def __init__(self, root_dir: str, pipeline_revision: str | None = None):
        self.root_dir = root_dir
        self.v2_root = os.path.join(root_dir, "v2")
        self.pipeline_revision = (
            get_pipeline_revision()
            if pipeline_revision is None
            else validate_pipeline_revision(pipeline_revision)
        )

    @staticmethod
    def _slugify(s: str) -> str:
        s = (s or "").lower().strip().replace(" ", "-")
        s = _SLUG_RE.sub("", s)
        while "--" in s:
            s = s.replace("--", "-")
        return s.strip("-")

    @staticmethod
    def _hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _safe_component(value: str) -> str:
        value = (value or "").strip()
        if not value or value in {".", ".."}:
            return ""
        if "/" in value or "\\" in value:
            return ""
        if not _PATH_COMPONENT_RE.fullmatch(value):
            return ""
        return value

    @staticmethod
    def _canonical_json_bytes(payload: Any) -> bytes:
        if payload is None:
            payload = {}
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        if isinstance(payload, str):
            payload = (
                json.loads(
                    payload,
                    parse_float=Decimal,
                    parse_constant=lambda constant: (_ for _ in ()).throw(
                        ValueError(f"invalid JSON number: {constant}")),
                )
                if payload.strip() else {}
            )
        return canonical_json_bytes(payload)

    def _object_path(self, sha256_hex: str) -> str:
        return os.path.join(self.v2_root, "objects", sha256_hex[:2], sha256_hex[2:4], sha256_hex)

    def _manifest_path(self, pipeline: str, source_hash: str) -> str:
        pipeline = self._safe_component(pipeline)
        if not pipeline or not _SHA256_RE.fullmatch(source_hash or ""):
            return ""
        return os.path.join(self.v2_root, "pages", "by-hash", pipeline, source_hash, "manifest.json")

    def _ref_path(self, pipeline: str, title: str, chapter: str,
                  page_number: str) -> str:
        if not (pipeline and title and chapter and page_number):
            return ""
        pipeline = self._safe_component(pipeline)
        title_slug = self._safe_component(self._slugify(title))
        chapter = self._safe_component(chapter)
        page_number = self._safe_component(page_number)
        if not (pipeline and title_slug and chapter and page_number):
            return ""
        return os.path.join(
            self.v2_root,
            "pages",
            "by-ref",
            pipeline,
            title_slug,
            chapter,
            f"{page_number}.json",
        )

    @staticmethod
    def _atomic_write(path: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = os.path.join(
            os.path.dirname(path),
            f".{os.path.basename(path)}.tmp-{os.getpid()}-{time.time_ns()}",
        )
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _store_object(self, data: bytes) -> tuple[str, int]:
        sha = self._hash_bytes(data)
        path = self._object_path(sha)
        try:
            with open(path, "rb") as f:
                existing = f.read()
            if self._hash_bytes(existing) != sha:
                raise ValueError(f"Corrupted object on disk: {sha}")
            return sha, len(data)
        except FileNotFoundError:
            self._atomic_write(path, data)
            return sha, len(data)

    def _read_object_verified(self, sha: str) -> bytes:
        with open(self._object_path(sha), "rb") as f:
            data = f.read()
        if self._hash_bytes(data) != sha:
            raise ValueError(f"Object hash mismatch: {sha}")
        return data

    def load_manifest_by_hash(self, pipeline: str,
                              source_hash: str) -> dict[str, Any] | None:
        path = self._manifest_path(pipeline, source_hash)
        if not path:
            return None
        try:
            with open(path, "rb") as f:
                manifest = json.load(f)
            if not isinstance(manifest, dict):
                return None
            schema_version = manifest.get("schema_version")
            if (not isinstance(schema_version, int)
                    or isinstance(schema_version, bool)
                    or schema_version not in {2, CACHE_MANIFEST_SCHEMA_VERSION}):
                return None
            if (manifest.get("pipeline") != pipeline
                    or manifest.get("source_hash") != source_hash):
                return None
            # Manifests written before pipeline revisioning belong to the
            # explicitly named legacy revision. This keeps existing caches
            # readable while making every later revision a cache miss.
            manifest_revision = manifest.get("pipeline_revision")
            if schema_version == 2:
                if manifest_revision is not None:
                    return None
                manifest_revision = LEGACY_PIPELINE_REVISION
            if not isinstance(manifest_revision, str):
                return None
            if manifest_revision != self.pipeline_revision:
                return None
            return manifest
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def resolve_source_hash(self, pipeline: str, title: str, chapter: str,
                            page_number: str) -> str | None:
        path = self._ref_path(pipeline, title, chapter, page_number)
        if not path:
            return None
        try:
            with open(path, "rb") as f:
                ref = json.load(f)
            source_hash = ref.get("source_hash")
            if isinstance(source_hash, str) and len(source_hash) == 64:
                return source_hash
            return None
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def load_output_image_by_hash(self, pipeline: str,
                                  source_hash: str) -> bytes | None:
        manifest = self.load_manifest_by_hash(pipeline, source_hash)
        if not manifest or manifest.get("image_stale"):
            return None
        sha = manifest.get("image_object_sha256")
        if not isinstance(sha, str):
            return None
        try:
            return self._read_object_verified(sha)
        except (FileNotFoundError, ValueError):
            return None

    def load_object(self, sha256_hex: str) -> bytes | None:
        """Load a raw object by its SHA256 hex hash, without needing a manifest."""
        try:
            return self._read_object_verified(sha256_hex)
        except (FileNotFoundError, ValueError):
            return None

    def load_source_image_by_hash(self, pipeline: str,
                                  source_hash: str) -> bytes | None:
        manifest = self.load_manifest_by_hash(pipeline, source_hash)
        if not manifest:
            return None
        sha = manifest.get("source_object_sha256")
        if not isinstance(sha, str):
            return None
        try:
            return self._read_object_verified(sha)
        except (FileNotFoundError, ValueError):
            return None

    def load_metadata_by_hash(
        self,
        pipeline: str,
        source_hash: str,
        *,
        document_pipeline: str | None = None,
    ) -> dict[str, Any] | None:
        manifest = self.load_manifest_by_hash(pipeline, source_hash)
        if not manifest:
            return None
        sha = manifest.get("metadata_object_sha256")
        if not isinstance(sha, str):
            return None
        try:
            payload = self._read_object_verified(sha)
            metadata = json.loads(
                payload,
                parse_float=Decimal,
                parse_constant=lambda constant: (_ for _ in ()).throw(
                    ValueError(f"invalid JSON number: {constant}")),
            )
            return self._normalize_metadata(
                metadata, pipeline=document_pipeline or pipeline,
                source_hash=source_hash)
        except (
            FileNotFoundError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return None

    def _normalize_metadata(
        self,
        payload: Any,
        *,
        pipeline: str,
        source_hash: str,
    ) -> dict[str, Any]:
        """Validate metadata without mutating or falsely upgrading the input.

        Schema 1 is the legacy editable payload and remains schema 1. Schema 2
        is a complete PageDocument and must pass its full contract validation.
        Unknown future schemas are rejected instead of being silently rewritten
        by an older worker.
        """
        if not isinstance(payload, dict):
            raise TypeError("metadata payload must be an object")

        normalized = deepcopy(payload)
        schema_version = normalized.get("schema_version", 1)
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise TypeError("invalid metadata schema version")
        if schema_version not in {1, METADATA_SCHEMA_VERSION}:
            raise ValueError("unsupported metadata schema version")

        stored_pipeline = normalized.get("pipeline")
        if stored_pipeline not in {None, "", pipeline}:
            raise ValueError("metadata pipeline mismatch")
        stored_source_hash = normalized.get("source_hash")
        if stored_source_hash not in {None, "", source_hash}:
            raise ValueError("metadata source hash mismatch")

        if schema_version == 1:
            stored_revision = normalized.get("pipeline_revision")
            if stored_revision not in {None, "", self.pipeline_revision}:
                raise ValueError("metadata pipeline revision mismatch")
            return normalized

        document = PageDocument.from_dict(normalized)
        if document.pipeline_revision != self.pipeline_revision:
            raise ValueError("metadata pipeline revision mismatch")
        return document.to_dict()

    def _write_ref(self, pipeline: str, title: str, chapter: str,
                   page_number: str, source_hash: str) -> None:
        ref_path = self._ref_path(pipeline, title, chapter, page_number)
        if not ref_path:
            return
        payload = {
            "schema_version": 2,
            "pipeline": pipeline,
            "title_slug": self._slugify(title),
            "chapter": chapter,
            "page": page_number,
            "source_hash": source_hash,
            "updated_at_unix_ms": int(time.time() * 1000),
        }
        self._atomic_write(ref_path, json.dumps(payload, indent=2).encode("utf-8"))

    def store_page(
        self,
        *,
        pipeline: str,
        source_hash: str,
        source_image_bytes: bytes,
        rendered_image_bytes: bytes,
        metadata_payload: Any,
        document_pipeline: str | None = None,
        title: str = "",
        chapter: str = "",
        page_number: str = "",
    ) -> dict[str, Any]:
        if not pipeline:
            raise ValueError("missing pipeline")
        if self._hash_bytes(source_image_bytes) != source_hash:
            raise ValueError("source hash mismatch")
        if not self._manifest_path(pipeline, source_hash):
            raise ValueError("invalid cache key")

        metadata_payload = self._normalize_metadata(
            metadata_payload, pipeline=document_pipeline or pipeline,
            source_hash=source_hash)
        metadata_bytes = self._canonical_json_bytes(metadata_payload)
        source_obj_sha, source_obj_size = self._store_object(source_image_bytes)
        image_obj_sha, image_obj_size = self._store_object(rendered_image_bytes)
        meta_obj_sha, meta_obj_size = self._store_object(metadata_bytes)

        content_hash = self._hash_bytes(metadata_bytes)
        render_hash = self._hash_bytes(
            f"v2|{content_hash}|{image_obj_sha}".encode())
        now_ms = int(time.time() * 1000)

        prev = self.load_manifest_by_hash(pipeline, source_hash) or {}
        manifest = {
            "schema_version": CACHE_MANIFEST_SCHEMA_VERSION,
            "pipeline": pipeline,
            "pipeline_revision": self.pipeline_revision,
            "source_hash": source_hash,
            "title_slug": self._slugify(title or prev.get("title_slug", "")),
            "chapter": chapter or prev.get("chapter", ""),
            "page": page_number or prev.get("page", ""),
            "source_object_sha256": source_obj_sha,
            "source_bytes": source_obj_size,
            "image_object_sha256": image_obj_sha,
            "image_bytes": image_obj_size,
            "metadata_object_sha256": meta_obj_sha,
            "metadata_bytes": meta_obj_size,
            "content_hash": content_hash,
            "render_hash": render_hash,
            "image_stale": False,
            "created_at_unix_ms": prev.get("created_at_unix_ms", now_ms),
            "updated_at_unix_ms": now_ms,
        }

        self._atomic_write(
            self._manifest_path(pipeline, source_hash),
            json.dumps(manifest, indent=2).encode("utf-8"),
        )
        self._write_ref(
            pipeline,
            manifest.get("title_slug", ""),
            manifest.get("chapter", ""),
            manifest.get("page", ""),
            source_hash,
        )
        return manifest

    def update_metadata_by_hash(
        self,
        *,
        pipeline: str,
        source_hash: str,
        metadata_payload: Any,
        base_content_hash: str = "",
        document_pipeline: str | None = None,
    ) -> dict[str, Any]:
        manifest = self.load_manifest_by_hash(pipeline, source_hash)
        if not manifest:
            raise FileNotFoundError("manifest not found")
        if base_content_hash and base_content_hash != manifest.get("content_hash"):
            raise ValueError("content hash mismatch")

        metadata_payload = self._normalize_metadata(
            metadata_payload, pipeline=document_pipeline or pipeline,
            source_hash=source_hash)
        metadata_bytes = self._canonical_json_bytes(metadata_payload)
        meta_obj_sha, meta_obj_size = self._store_object(metadata_bytes)
        manifest["schema_version"] = CACHE_MANIFEST_SCHEMA_VERSION
        manifest["pipeline_revision"] = self.pipeline_revision
        manifest["metadata_object_sha256"] = meta_obj_sha
        manifest["metadata_bytes"] = meta_obj_size
        manifest["content_hash"] = self._hash_bytes(metadata_bytes)
        manifest["render_hash"] = self._hash_bytes(
            f"v2|{manifest['content_hash']}|{manifest['image_object_sha256']}".encode())
        manifest["image_stale"] = True
        manifest["updated_at_unix_ms"] = int(time.time() * 1000)
        self._atomic_write(
            self._manifest_path(pipeline, source_hash),
            json.dumps(manifest, indent=2).encode("utf-8"),
        )
        return manifest
