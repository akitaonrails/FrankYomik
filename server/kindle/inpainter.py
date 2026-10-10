"""AI inpainting for manga artwork text removal.

Thin wrapper around the LaMa backend from webtoon/inpainter.py.
Used to clean text overlaid on artwork before rendering English translations.

Shares the GPU inference lock with the webtoon inpainter to prevent
concurrent GPU access.
"""

import logging
from dataclasses import dataclass

import numpy as np
from PIL import Image

from pipeline.cleanup import (
    CleanupBudgetExceeded,
    CleanupPolicy,
    build_cleanup_mask,
    deadline,
    gpu_memory_available,
    host_memory_available,
    plan_cleanup,
)
from webtoon.inpainter import SupervisedInpaintError, supervised_inpaint_tiles

from .config import MANGA_INPAINT_PAD

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MangaCleanupOutcome:
    image: Image.Image
    applied: bool
    degraded: bool = False
    reason: str | None = None


def cleanup_region(
    img_pil: Image.Image,
    bbox: tuple[int, int, int, int],
    pad: int = MANGA_INPAINT_PAD,
) -> MangaCleanupOutcome:
    """Inpaint a text region on artwork. Returns modified image copy.

    Crops the region with padding, builds a mask covering the text bbox,
    runs LaMa inpainting, and composites the result back.

    Args:
        img_pil: Full page image (PIL RGB).
        bbox: Text region bounding box (x1, y1, x2, y2).
        pad: Padding around text region for inpainting context.

    Returns:
        Modified copy of img_pil with text region inpainted.
        Returns the original image unchanged if inpainting fails.
    """
    img_w, img_h = img_pil.size
    try:
        mask = build_cleanup_mask(
            (img_w, img_h), [bbox], padding_px=4, dilation_px=2,
        )
    except CleanupBudgetExceeded:
        return MangaCleanupOutcome(img_pil, False, True, "page_budget")
    if mask is None:
        return MangaCleanupOutcome(img_pil, False, True, "empty_mask")
    policy = CleanupPolicy(context_px=pad)
    try:
        plan = plan_cleanup(img_pil, mask, semantic_mask=None, policy=policy)
    except CleanupBudgetExceeded:
        log.warning("Manga artwork cleanup exceeds tile budget")
        return MangaCleanupOutcome(img_pil, False, True, "tile_budget")
    if not host_memory_available(policy.min_host_available_mb):
        return MangaCleanupOutcome(img_pil, False, True, "host_memory")
    if not gpu_memory_available(policy.min_gpu_available_mb):
        return MangaCleanupOutcome(img_pil, False, True, "gpu_memory")
    end = deadline(policy)
    requests: list[tuple[Image.Image, Image.Image, int]] = []
    for index, tile in enumerate(plan.tiles):
        crop_box = tile.bbox
        requests.append((
            img_pil.crop(crop_box), mask.crop(crop_box), plan.seed + index,
        ))
    try:
        results = supervised_inpaint_tiles(requests, end=end)
    except SupervisedInpaintError as error:
        log.warning("Manga artwork cleanup degraded: %s", error.reason)
        return MangaCleanupOutcome(img_pil, False, True, error.reason)

    result = img_pil.copy()
    for tile, result_crop in zip(plan.tiles, results, strict=True):
        crop_box = tile.bbox
        mask_arr = np.asarray(mask.crop(crop_box)) > 0
        composite = np.where(
            mask_arr[:, :, np.newaxis],
            np.asarray(result_crop.convert("RGB")),
            np.asarray(result.crop(crop_box).convert("RGB")),
        )
        result.paste(Image.fromarray(composite.astype(np.uint8)), crop_box[:2])

    return MangaCleanupOutcome(result, True)


def inpaint_region(
    img_pil: Image.Image,
    bbox: tuple[int, int, int, int],
    pad: int = MANGA_INPAINT_PAD,
) -> Image.Image:
    """Compatibility wrapper returning the safely completed image."""
    return cleanup_region(img_pil, bbox, pad).image
