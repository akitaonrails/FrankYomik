"""Deterministic pipeline identity derived from the pinned stage manifest."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

LEGACY_PIPELINE_REVISION = "legacy-v1"
PIPELINE_REVISION_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"
PIPELINE_REVISION_RE = re.compile(PIPELINE_REVISION_PATTERN)
_MANIFEST_PATH = Path(__file__).with_name("stage_manifest.json")
_SERVER_ROOT = _MANIFEST_PATH.parent.parent


def validate_pipeline_revision(value: object) -> str:
    """Return a valid revision string or reject the wire/config value."""
    if not isinstance(value, str) or not PIPELINE_REVISION_RE.fullmatch(value):
        raise ValueError("invalid pipeline revision")
    return value


def load_stage_manifest() -> dict[str, Any]:
    """Load an independent copy of the checked-in stage identity manifest."""
    with _MANIFEST_PATH.open(encoding="utf-8") as manifest_file:
        manifest = json.load(
            manifest_file,
            parse_float=_parse_normal_manifest_float,
            parse_int=_parse_normal_manifest_int,
        )
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("unsupported pipeline stage manifest")
    return manifest


def fingerprint_stage_manifest(manifest: dict[str, Any]) -> str:
    """Hash every pinned identity using one stable JSON representation."""
    _validate_manifest_normal_form(manifest)
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _parse_normal_manifest_float(raw: str) -> float:
    if "e" in raw.lower() or raw.endswith("0"):
        raise ValueError(f"non-canonical stage manifest number: {raw}")
    return float(raw)


def _parse_normal_manifest_int(raw: str) -> int:
    if raw == "-0":
        raise ValueError("non-canonical stage manifest number: -0")
    return int(raw)


def _validate_manifest_normal_form(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_manifest_normal_form(key)
            _validate_manifest_normal_form(child)
    elif isinstance(value, list):
        for child in value:
            _validate_manifest_normal_form(child)
    elif isinstance(value, str):
        if "\u2028" in value or "\u2029" in value:
            raise ValueError("stage manifest contains a non-canonical string")
    elif isinstance(value, float):
        spelling = repr(value).lower()
        if (
            not math.isfinite(value)
            or "e" in spelling
            or spelling.endswith(".0")
        ):
            raise ValueError("stage manifest contains a non-canonical number")


def verify_stage_manifest_artifacts(manifest: dict[str, Any]) -> None:
    """Fail when checked-in code, config, or font bytes drift from the manifest."""
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("pipeline stage manifest has no artifacts")
    for name, identity in artifacts.items():
        if not isinstance(identity, dict):
            raise TypeError(f"invalid pipeline artifact {name}")
        relative_path = identity.get("path")
        expected = identity.get("sha256")
        if not isinstance(relative_path, str) or not isinstance(expected, str):
            raise TypeError(f"invalid pipeline artifact {name}")
        path = (_SERVER_ROOT / relative_path).resolve()
        if _SERVER_ROOT.resolve() not in path.parents:
            raise ValueError(f"pipeline artifact escapes server root: {name}")
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise ValueError(f"pipeline artifact unavailable: {name}") from error
        if actual != expected:
            raise ValueError(
                f"pipeline artifact fingerprint mismatch: {name}"
            )


@lru_cache(maxsize=1)
def derived_pipeline_revision() -> str:
    manifest = load_stage_manifest()
    verify_stage_manifest_artifacts(manifest)
    return fingerprint_stage_manifest(manifest)


DEFAULT_PIPELINE_REVISION = derived_pipeline_revision()


def get_pipeline_revision() -> str:
    """Return the derived revision and reject a mismatched expected value."""
    derived = derived_pipeline_revision()
    expected = os.environ.get("PIPELINE_REVISION", "").strip()
    if not expected:
        return derived
    validate_pipeline_revision(expected)
    if expected == LEGACY_PIPELINE_REVISION:
        if os.environ.get("ALLOW_LEGACY_PIPELINE_REVISION") == "1":
            return expected
        raise ValueError(
            "legacy pipeline revision requires ALLOW_LEGACY_PIPELINE_REVISION=1"
        )
    if expected != derived:
        raise ValueError(
            f"PIPELINE_REVISION mismatch: expected {expected}, derived {derived}"
        )
    return derived


def stage_identity(pipeline: str, stage: str) -> dict[str, Any]:
    """Return the pinned identity for one executable pipeline stage."""
    manifest = load_stage_manifest()
    pipelines = manifest.get("pipelines", {})
    stages = manifest.get("stages", {})
    try:
        identity_name = pipelines[pipeline][stage]
        identity = stages[identity_name]
    except (KeyError, TypeError) as error:
        raise ValueError(f"missing stage identity for {pipeline}/{stage}") from error
    if not isinstance(identity, dict):
        raise TypeError(f"invalid stage identity for {pipeline}/{stage}")
    return deepcopy(identity)


def stage_config_hash(identity: dict[str, Any]) -> str:
    """Hash prompt and output settings separately from implementation identity."""
    values = {
        "prompt": identity.get("prompt"),
        "output_config": identity.get("output_config", {}),
    }
    canonical = json.dumps(
        values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"
