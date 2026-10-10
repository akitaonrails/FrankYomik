"""Pinned provenance and policy metadata for current production adapters."""

from __future__ import annotations

from .document import Provenance
from .revision import stage_config_hash, stage_identity


def provenance_for(
    pipeline: str,
    stage: str,
    _pipeline_revision: str,
) -> Provenance:
    """Describe the stage itself, independently of the deployment fingerprint."""
    identity = stage_identity(pipeline, stage)
    implementation = identity["implementation"]
    model_identity = identity.get("model")
    model = None
    if model_identity is not None:
        model = (
            f"{model_identity['id']}@"
            f"{model_identity['weights_revision']}"
        )
    return Provenance(
        backend=str(identity["backend"]),
        revision=str(implementation["revision"]),
        model=model,
        config_hash=stage_config_hash(identity),
    )


def language_for(pipeline: str) -> str:
    return "ko" if pipeline == "webtoon" else "ja"


def cleanup_for(pipeline: str, kind: str) -> str | None:
    if pipeline == "webtoon":
        return "webtoon-policy"
    if kind == "artwork_text":
        return "region_fill"
    if pipeline.startswith("manga_"):
        return "stroke_clear"
    return None

