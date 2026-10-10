"""Content-addressed raster artifacts used by document-driven rendering."""

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Mapping

import numpy as np
from PIL import Image

from .document import ImageReference, MaskReference

_OBJECT_RE = re.compile(r"^objects/([a-f0-9]{64})$")
MAX_ARTIFACT_PIXELS = 40_000_000
MAX_ARTIFACT_BYTES = 160_000_000


def encode_png(image: Image.Image, mode: str) -> bytes:
    output = io.BytesIO()
    image.convert(mode).save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def store_image_artifact(
    image: Image.Image,
    artifacts: dict[str, bytes],
) -> ImageReference:
    normalized = image.convert("RGB")
    payload = encode_png(normalized, "RGB")
    digest = hashlib.sha256(payload).hexdigest()
    artifacts[digest] = payload
    return ImageReference(
        reference=f"objects/{digest}", encoding="png",
        width=normalized.width, height=normalized.height,
        mode="RGB", sha256=digest,
    )


def store_mask_artifact(
    mask: Image.Image | np.ndarray,
    artifacts: dict[str, bytes],
) -> MaskReference:
    normalized = Image.fromarray(np.asarray(mask, dtype=np.uint8)).convert("L")
    normalized = normalized.point(lambda value: 255 if value else 0, mode="L")
    payload = encode_png(normalized, "L")
    digest = hashlib.sha256(payload).hexdigest()
    artifacts[digest] = payload
    return MaskReference(
        reference=f"objects/{digest}", encoding="png-mask-l1",
        width=normalized.width, height=normalized.height, sha256=digest,
    )


def resolve_artifact(
    reference: ImageReference | MaskReference,
    artifacts: Mapping[str, bytes],
    *,
    mode: str,
) -> Image.Image:
    match = _OBJECT_RE.fullmatch(reference.reference)
    if match is None or reference.sha256 is None or match.group(1) != reference.sha256:
        raise ValueError("unsafe or inconsistent artifact reference")
    payload = artifacts.get(reference.sha256)
    if payload is None:
        raise ValueError(f"missing artifact: {reference.sha256}")
    if hashlib.sha256(payload).hexdigest() != reference.sha256:
        raise ValueError(f"artifact hash mismatch: {reference.sha256}")
    if (reference.width * reference.height > MAX_ARTIFACT_PIXELS
            or len(payload) > MAX_ARTIFACT_BYTES):
        raise ValueError("artifact exceeds decode budget")
    if isinstance(reference, ImageReference):
        if reference.encoding != "png" or mode != reference.mode:
            raise ValueError("image artifact encoding mismatch")
    elif reference.encoding != "png-mask-l1" or mode != "L":
        raise ValueError("mask artifact encoding mismatch")
    try:
        with Image.open(io.BytesIO(payload)) as loaded:
            if loaded.format != "PNG":
                raise ValueError("artifact encoding mismatch")
            if loaded.size != (reference.width, reference.height):
                raise ValueError("artifact dimensions do not match reference")
            if loaded.width * loaded.height > MAX_ARTIFACT_PIXELS:
                raise ValueError("artifact exceeds decode budget")
            loaded.load()
            image = loaded.convert(mode)
    except OSError as error:
        raise ValueError("invalid PNG artifact") from error
    return image


def changed_pixel_mask(
    source: Image.Image,
    cleaned: Image.Image,
    bbox: tuple[int, int, int, int],
) -> Image.Image:
    if source.size != cleaned.size:
        raise ValueError("cleanup base dimensions changed")
    source_pixels = np.asarray(source.convert("RGB"))
    cleaned_pixels = np.asarray(cleaned.convert("RGB"))
    changed = np.any(source_pixels != cleaned_pixels, axis=2)
    x1, y1, x2, y2 = bbox
    bounded = np.zeros(changed.shape, dtype=np.uint8)
    bounded[y1:y2, x1:x2] = np.where(changed[y1:y2, x1:x2], 255, 0)
    return Image.fromarray(bounded, mode="L")
