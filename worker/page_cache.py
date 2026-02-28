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
from typing import Any


_SLUG_RE = re.compile(r"[^a-z0-9\-]")


class PageCache:
    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        self.v2_root = os.path.join(root_dir, "v2")

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
    def _canonical_json_bytes(payload: Any) -> bytes:
        if payload is None:
            payload = {}
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        if isinstance(payload, str):
            payload = json.loads(payload) if payload.strip() else {}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _object_path(self, sha256_hex: str) -> str:
        return os.path.join(self.v2_root, "objects", sha256_hex[:2], sha256_hex[2:4], sha256_hex)

    def _manifest_path(self, pipeline: str, source_hash: str) -> str:
        return os.path.join(self.v2_root, "pages", "by-hash", pipeline, source_hash, "manifest.json")

    def _ref_path(self, pipeline: str, title: str, chapter: str,
                  page_number: str) -> str:
        if not (pipeline and title and chapter and page_number):
            return ""
        return os.path.join(
            self.v2_root,
            "pages",
            "by-ref",
            pipeline,
            self._slugify(title),
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
        try:
            with open(path, "rb") as f:
                manifest = json.load(f)
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

    def load_metadata_by_hash(self, pipeline: str,
                              source_hash: str) -> dict[str, Any] | None:
        manifest = self.load_manifest_by_hash(pipeline, source_hash)
        if not manifest:
            return None
        sha = manifest.get("metadata_object_sha256")
        if not isinstance(sha, str):
            return None
        try:
            payload = self._read_object_verified(sha)
            return json.loads(payload)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return None

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
        title: str = "",
        chapter: str = "",
        page_number: str = "",
    ) -> dict[str, Any]:
        if not pipeline:
            raise ValueError("missing pipeline")
        if self._hash_bytes(source_image_bytes) != source_hash:
            raise ValueError("source hash mismatch")

        metadata_bytes = self._canonical_json_bytes(metadata_payload)
        source_obj_sha, source_obj_size = self._store_object(source_image_bytes)
        image_obj_sha, image_obj_size = self._store_object(rendered_image_bytes)
        meta_obj_sha, meta_obj_size = self._store_object(metadata_bytes)

        content_hash = self._hash_bytes(metadata_bytes)
        render_hash = self._hash_bytes(
            f"v2|{content_hash}|{image_obj_sha}".encode("utf-8"))
        now_ms = int(time.time() * 1000)

        prev = self.load_manifest_by_hash(pipeline, source_hash) or {}
        manifest = {
            "schema_version": 2,
            "pipeline": pipeline,
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
    ) -> dict[str, Any]:
        manifest = self.load_manifest_by_hash(pipeline, source_hash)
        if not manifest:
            raise FileNotFoundError("manifest not found")
        if base_content_hash and base_content_hash != manifest.get("content_hash"):
            raise ValueError("content hash mismatch")

        metadata_bytes = self._canonical_json_bytes(metadata_payload)
        meta_obj_sha, meta_obj_size = self._store_object(metadata_bytes)
        manifest["metadata_object_sha256"] = meta_obj_sha
        manifest["metadata_bytes"] = meta_obj_size
        manifest["content_hash"] = self._hash_bytes(metadata_bytes)
        manifest["render_hash"] = self._hash_bytes(
            f"v2|{manifest['content_hash']}|{manifest['image_object_sha256']}".encode("utf-8"))
        manifest["image_stale"] = True
        manifest["updated_at_unix_ms"] = int(time.time() * 1000)
        self._atomic_write(
            self._manifest_path(pipeline, source_hash),
            json.dumps(manifest, indent=2).encode("utf-8"),
        )
        return manifest
