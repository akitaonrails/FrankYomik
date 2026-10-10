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


def ocr_engine_provenance(engine: str) -> Provenance:
    """Return the selected manga OCR engine identity from the router stage."""
    identity = stage_identity("manga_translate", "ocr")
    try:
        engine_identity = identity["engines"][engine]
    except (KeyError, TypeError) as error:
        raise ValueError(f"missing OCR engine identity: {engine}") from error
    model_identity = engine_identity["model"]
    return Provenance(
        backend=str(engine_identity["backend"]),
        revision=str(engine_identity["implementation_revision"]),
        model=(f"{model_identity['id']}@"
               f"{model_identity['weights_revision']}"),
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
