"""Runtime checks that executed model bytes match stage provenance."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def normalize_sha256(value: str) -> str:
    """Return a lowercase SHA-256 digest without its optional algorithm prefix."""
    digest = value.removeprefix("sha256:").lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("invalid SHA-256 model identity")
    return digest


def verify_file_sha256(path: str | Path, expected: str) -> Path:
    """Fail closed unless a local model file has the pinned byte digest."""
    model_path = Path(path)
    try:
        digest = hashlib.sha256()
        with model_path.open("rb") as model_file:
            for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
    except OSError as error:
        raise RuntimeError(f"model weights unavailable: {model_path}") from error
    if actual != normalize_sha256(expected):
        raise RuntimeError(
            f"model weights digest mismatch for {model_path}: "
            f"expected {normalize_sha256(expected)}, got {actual}"
        )
    return model_path


def verify_ollama_tags(
    models: object,
    configured_model: str,
    expected_model: str,
    expected_digest: str,
) -> None:
    """Validate an Ollama ``/api/tags`` payload against the pinned identity."""
    if configured_model != expected_model:
        raise RuntimeError(
            f"configured Ollama model {configured_model!r} does not match "
            f"pinned model {expected_model!r}"
        )
    if not isinstance(models, list):
        raise TypeError("invalid Ollama /api/tags response")
    for value in models:
        if not isinstance(value, dict):
            continue
        name = value.get("name")
        model = value.get("model")
        if configured_model not in {name, model}:
            continue
        digest = value.get("digest")
        if not isinstance(digest, str):
            raise TypeError("Ollama model has no digest")
        if normalize_sha256(digest) != normalize_sha256(expected_digest):
            raise RuntimeError(
                f"Ollama model digest mismatch for {configured_model!r}"
            )
        return
    raise RuntimeError(f"pinned Ollama model is not installed: {configured_model}")


def ollama_models_from_response(payload: Any) -> object:
    """Extract the model list while keeping response validation fail closed."""
    if not isinstance(payload, dict):
        raise TypeError("invalid Ollama /api/tags response")
    return payload.get("models")
