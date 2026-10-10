"""Contract tests for deterministic cleanup mask planning."""

import numpy as np
import pytest
from PIL import Image

from pipeline.cleanup import (
    CleanupBudgetExceeded,
    CleanupPolicy,
    build_cleanup_mask,
    estimate_ocr_strokes,
    plan_cleanup,
)


def test_cleanup_mask_dilates_then_clips_to_semantic_boundary():
    semantic = np.zeros((40, 40), dtype=np.uint8)
    semantic[10:30, 10:30] = 255
    mask = build_cleanup_mask(
        (40, 40), [(7, 15, 18, 20)], semantic_mask=semantic,
        dilation_px=4,
    )
    assert mask is not None
    result = np.asarray(mask)
    assert np.any(result[10:30, 10:22])
    assert not np.any(result[semantic == 0])


def test_estimated_ocr_strokes_keep_glyph_pixels_instead_of_whole_box():
    pixels = np.full((20, 20, 3), 255, dtype=np.uint8)
    pixels[6:14, 9:11] = 0
    strokes = estimate_ocr_strokes(
        Image.fromarray(pixels), [(4, 4, 16, 16)], (255, 255, 255),
    )
    assert np.all(strokes[6:14, 9:11] == 255)
    assert strokes[5, 5] == 0


def test_uniform_plan_has_stable_color_and_ordered_bounded_tiles():
    image = Image.new("RGB", (100, 80), (248, 249, 250))
    semantic = np.full((80, 100), 255, dtype=np.uint8)
    mask = build_cleanup_mask((100, 80), [(60, 40, 65, 45), (10, 5, 15, 10)])
    plan = plan_cleanup(
        image, mask, semantic_mask=semantic,
        policy=CleanupPolicy(context_px=2),
    )
    assert plan.uniform_color == (248, 249, 250)
    assert [tile.bbox for tile in plan.tiles] == [
        (8, 3, 17, 12), (58, 38, 67, 47),
    ]
    assert plan.seed == 0


def test_tile_count_and_dimensions_are_rejected_before_inference():
    image = Image.new("RGB", (100, 100), "white")
    mask = build_cleanup_mask((100, 100), [(0, 0, 80, 80)])
    with pytest.raises(CleanupBudgetExceeded, match="dimensions"):
        plan_cleanup(
            image, mask, semantic_mask=None,
            policy=CleanupPolicy(max_tile_width=40, max_tile_height=40),
        )


def test_page_pixel_budget_is_checked_before_mask_allocation():
    with pytest.raises(CleanupBudgetExceeded, match="page dimensions"):
        build_cleanup_mask(
            (100_000, 100_000), [(0, 0, 1, 1)], max_page_pixels=1_000,
        )


def test_tile_pixel_budget_is_independent_of_dimensions():
    image = Image.new("RGB", (100, 100), "white")
    mask = build_cleanup_mask((100, 100), [(0, 0, 60, 60)])
    with pytest.raises(CleanupBudgetExceeded, match="tile pixels"):
        plan_cleanup(
            image, mask, semantic_mask=None,
            policy=CleanupPolicy(
                max_tile_width=100, max_tile_height=100, max_tile_pixels=2_000,
            ),
        )
