"""Deterministic cleanup-mask planning shared by manga and webtoon.

This module is deliberately model-free.  Adapters remain responsible for
loading LaMa (or a future, verified fallback) after a plan passes its resource
budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

import cv2
import numpy as np
from PIL import Image, ImageFilter


@dataclass(frozen=True, slots=True)
class CleanupPolicy:
    dilation_px: int = 6
    context_px: int = 20
    max_tiles: int = 16
    max_tile_width: int = 1024
    max_tile_height: int = 1024
    max_tile_pixels: int = 1_048_576
    max_page_pixels: int = 40_000_000
    max_wall_seconds: float = 30.0
    min_host_available_mb: int = 512
    min_gpu_available_mb: int = 512
    uniform_stddev: float = 8.0
    uniform_range: int = 28
    seed: int = 0


@dataclass(frozen=True, slots=True)
class CleanupTile:
    bbox: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    mask: Image.Image
    tiles: tuple[CleanupTile, ...]
    uniform_color: tuple[int, int, int] | None
    seed: int


class CleanupBudgetExceeded(RuntimeError):
    """Raised before inference when cleanup exceeds a deterministic budget."""


def build_cleanup_mask(
    size: tuple[int, int],
    ocr_boxes: list[tuple[int, int, int, int]],
    *,
    semantic_mask: np.ndarray | None = None,
    padding_px: int = 0,
    dilation_px: int = 0,
    erosion_px: int = 0,
    max_page_pixels: int = 40_000_000,
    ocr_stroke_mask: np.ndarray | None = None,
) -> Image.Image | None:
    """Union OCR stroke regions, dilate them, and clip to semantic geometry."""
    width, height = size
    if width <= 0 or height <= 0 or width * height > max_page_pixels:
        raise CleanupBudgetExceeded("cleanup page dimensions exceed policy")
    if ocr_stroke_mask is not None:
        strokes = np.asarray(ocr_stroke_mask, dtype=np.uint8)
        if strokes.shape != (height, width):
            raise ValueError("OCR stroke mask dimensions must match the page")
        strokes = np.where(strokes > 0, 255, 0).astype(np.uint8)
    else:
        strokes = np.zeros((height, width), dtype=np.uint8)
        for left, top, right, bottom in ocr_boxes:
            x1 = max(0, min(width, int(left) - padding_px))
            y1 = max(0, min(height, int(top) - padding_px))
            x2 = max(0, min(width, int(right) + padding_px))
            y2 = max(0, min(height, int(bottom) + padding_px))
            if x2 > x1 and y2 > y1:
                strokes[y1:y2, x1:x2] = 255
    expansion_px = dilation_px + (padding_px if ocr_stroke_mask is not None else 0)
    if expansion_px:
        strokes = np.asarray(Image.fromarray(strokes).filter(
            ImageFilter.MaxFilter(expansion_px * 2 + 1)
        ))
    if semantic_mask is not None:
        semantic = np.asarray(semantic_mask, dtype=np.uint8)
        if semantic.shape != strokes.shape:
            semantic = np.asarray(Image.fromarray(semantic).resize(
                size, Image.Resampling.NEAREST,
            ))
        if erosion_px:
            semantic = np.asarray(Image.fromarray(semantic).filter(
                ImageFilter.MinFilter(erosion_px * 2 + 1)
            ))
        strokes = np.where((strokes > 0) & (semantic > 0), 255, 0).astype(np.uint8)
    return None if not np.any(strokes) else Image.fromarray(strokes, mode="L")


def estimate_ocr_strokes(
    image: Image.Image,
    boxes: list[tuple[int, int, int, int]],
    background: tuple[int, int, int],
    *,
    threshold: int = 20,
) -> np.ndarray:
    """Estimate glyph pixels inside OCR geometry with per-box safe fallback."""
    pixels = np.asarray(image.convert("RGB"))
    height, width = pixels.shape[:2]
    result = np.zeros((height, width), dtype=np.uint8)
    bg = np.asarray(background, dtype=np.int16)
    for left, top, right, bottom in boxes:
        x1, y1 = max(0, int(left)), max(0, int(top))
        x2, y2 = min(width, int(right)), min(height, int(bottom))
        if x2 <= x1 or y2 <= y1:
            continue
        crop = pixels[y1:y2, x1:x2].astype(np.int16)
        foreground = np.max(np.abs(crop - bg), axis=2) >= threshold
        # Empty/near-empty estimates use the OCR geometry to avoid remnants.
        if np.count_nonzero(foreground) < max(1, foreground.size // 100):
            foreground[:, :] = True
        result[y1:y2, x1:x2] = np.where(foreground, 255, 0)
    return result


def _uniform_color(
    image: Image.Image,
    semantic_mask: np.ndarray | None,
    cleanup_mask: Image.Image,
    policy: CleanupPolicy,
) -> tuple[int, int, int] | None:
    if semantic_mask is None:
        return None
    pixels = np.asarray(image.convert("RGB"))
    mask = (np.asarray(semantic_mask) > 0) & (np.asarray(cleanup_mask) == 0)
    if mask.shape != pixels.shape[:2] or not np.any(mask):
        return None
    sample = pixels[mask]
    channel_range = sample.max(axis=0).astype(int) - sample.min(axis=0).astype(int)
    if float(sample.std(axis=0).max()) > policy.uniform_stddev:
        return None
    if int(channel_range.max()) > policy.uniform_range:
        return None
    median = np.median(sample, axis=0).astype(np.uint8)
    return tuple(int(value) for value in median)


def _tiles(mask: Image.Image, policy: CleanupPolicy) -> tuple[CleanupTile, ...]:
    binary = (np.asarray(mask) > 0).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    boxes: list[tuple[int, int, int, int]] = []
    width, height = mask.size
    for index in range(1, count):
        x, y, w, h, _ = (int(value) for value in stats[index])
        box = (
            max(0, x - policy.context_px), max(0, y - policy.context_px),
            min(width, x + w + policy.context_px),
            min(height, y + h + policy.context_px),
        )
        if box[2] - box[0] > policy.max_tile_width or box[3] - box[1] > policy.max_tile_height:
            raise CleanupBudgetExceeded("cleanup tile dimensions exceed policy")
        if (box[2] - box[0]) * (box[3] - box[1]) > policy.max_tile_pixels:
            raise CleanupBudgetExceeded("cleanup tile pixels exceed policy")
        boxes.append(box)
    boxes.sort(key=lambda box: (box[1], box[0], box[3], box[2]))
    if len(boxes) > policy.max_tiles:
        raise CleanupBudgetExceeded("cleanup tile count exceeds policy")
    return tuple(CleanupTile(box) for box in boxes)


def plan_cleanup(
    image: Image.Image,
    mask: Image.Image,
    *,
    semantic_mask: np.ndarray | None,
    policy: CleanupPolicy,
) -> CleanupPlan:
    return CleanupPlan(
        mask=mask,
        tiles=_tiles(mask, policy),
        uniform_color=_uniform_color(image, semantic_mask, mask, policy),
        seed=policy.seed,
    )


def host_memory_available(required_mb: int) -> bool:
    """Check Linux available memory without adding a runtime dependency."""
    if required_mb <= 0:
        return True
    try:
        values = {}
        with open("/proc/meminfo", encoding="ascii") as source:
            for line in source:
                key, value = line.split(":", 1)
                values[key] = int(value.strip().split()[0])
        return values.get("MemAvailable", 0) >= required_mb * 1024
    except (OSError, ValueError):
        return False


def gpu_memory_available(required_mb: int) -> bool:
    if required_mb <= 0:
        return True
    try:
        import torch
        if not torch.cuda.is_available():
            return True
        free_bytes, _ = torch.cuda.mem_get_info()
        return free_bytes >= required_mb * 1024 * 1024
    except (ImportError, RuntimeError):
        return False


def seed_inference(seed: int) -> None:
    """Set deterministic process-local seeds immediately before inference."""
    np.random.seed(seed)
    try:
        import torch
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
    except ImportError:
        pass


def deadline(policy: CleanupPolicy) -> float:
    return monotonic() + policy.max_wall_seconds


def within_deadline(value: float) -> bool:
    return monotonic() <= value
