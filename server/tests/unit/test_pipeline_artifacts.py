"""Raster artifact boundary tests."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from PIL import Image

from pipeline.artifacts import resolve_artifact, store_image_artifact, store_mask_artifact


def test_image_and_mask_artifacts_round_trip_with_exact_modes():
    artifacts: dict[str, bytes] = {}
    image = Image.new("RGB", (13, 17), (3, 4, 5))
    mask = np.zeros((17, 13), dtype=np.uint8)
    mask[2:6, 4:9] = 255

    image_ref = store_image_artifact(image, artifacts)
    mask_ref = store_mask_artifact(mask, artifacts)

    assert resolve_artifact(image_ref, artifacts, mode="RGB").tobytes() == image.tobytes()
    assert np.array_equal(
        np.asarray(resolve_artifact(mask_ref, artifacts, mode="L")), mask,
    )


def test_artifact_resolver_rejects_missing_tampered_and_wrong_dimensions():
    artifacts: dict[str, bytes] = {}
    reference = store_image_artifact(Image.new("RGB", (8, 9), "white"), artifacts)

    with pytest.raises(ValueError, match="missing artifact"):
        resolve_artifact(reference, {}, mode="RGB")
    with pytest.raises(ValueError, match="hash mismatch"):
        resolve_artifact(reference, {reference.sha256: b"tampered"}, mode="RGB")
    with pytest.raises(ValueError, match="dimensions"):
        resolve_artifact(replace(reference, width=7), artifacts, mode="RGB")


def test_artifact_resolver_checks_pixel_budget_before_decode():
    artifacts: dict[str, bytes] = {}
    reference = store_image_artifact(Image.new("RGB", (1, 1), "white"), artifacts)
    oversized = replace(reference, width=40_000_001, height=1)

    with pytest.raises(ValueError, match="decode budget"):
        resolve_artifact(oversized, artifacts, mode="RGB")
