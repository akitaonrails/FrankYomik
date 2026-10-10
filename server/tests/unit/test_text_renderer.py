"""Regression tests for text rendering: SFX detection, hyphenation, word wrap."""

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import kindle.text_renderer as text_renderer
from kindle.text_renderer import (
    _is_sound_effect,
    _word_wrap,
    _choose_layout,
    _cap_expanded_furigana_font_size,
    _cap_furigana_to_source_scale,
    _floor_furigana_to_page_dialogue_scale,
    compute_furigana_page_font_limits,
    estimate_source_vertical_font_size,
    _furigana_font_size,
    _fit_vertical_font_size,
    _fit_furigana_stack,
    _expand_bright_region_bbox,
    _expand_limited_no_mask_bbox,
    _no_mask_furigana_layout_bbox,
    _tighten_no_mask_bbox_to_bright_region,
    _WIDE_BBOX_HORIZONTAL_RATIO,
    _mask_safe_bbox,
    _vertical_furigana_char_height,
    _vertical_main_char_height,
    render_furigana_vertical,
)
from kindle.config import FONT_EN


def test_furigana_render_is_contained_and_reports_actual_layout():
    image = Image.new("RGB", (120, 140), (90, 100, 110))
    before = np.asarray(image).copy()
    bbox = (40, 40, 80, 100)
    segments = [{
        "text": "長い日本語文章長い日本語文章",
        "furigana": "ながいにほんごぶんしょうながいにほんごぶんしょう",
        "needs_furigana": True,
    }]

    outcome = render_furigana_vertical(image, bbox, segments)

    changed = np.any(np.asarray(image) != before, axis=2)
    allowed = np.zeros(changed.shape, dtype=bool)
    allowed[40:100, 40:80] = True
    assert not np.any(changed & ~allowed)
    assert outcome == {
        "font_size": 10,
        "line_count": 2,
        "fit_status": "overflow",
        "ellipsized": False,
    }


def _make_draw_and_font(size=16):
    img = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(FONT_EN, size)
    except Exception:
        font = ImageFont.load_default()
    return draw, font


class TestSFXDetection:
    """Ensure SFX detection is selective — only match real sound effects."""

    def test_repeated_chars_are_sfx(self):
        assert _is_sound_effect("Grrr")
        assert _is_sound_effect("Aaaa")
        assert _is_sound_effect("AAAA!!")

    def test_specific_sfx_words(self):
        assert _is_sound_effect("Bam!")
        assert _is_sound_effect("Crash!")
        assert _is_sound_effect("Boom")
        assert _is_sound_effect("Wham!")

    def test_pure_punctuation_is_sfx(self):
        assert _is_sound_effect("!!")
        assert _is_sound_effect("...")
        assert _is_sound_effect("...!")

    def test_dialogue_is_not_sfx(self):
        """Common short dialogue words must NOT be classified as SFX."""
        assert not _is_sound_effect("Why?")
        assert not _is_sound_effect("Stop!")
        assert not _is_sound_effect("But...")
        assert not _is_sound_effect("No!")
        assert not _is_sound_effect("What?")
        assert not _is_sound_effect("Hey!")

    def test_multi_word_is_not_sfx(self):
        assert not _is_sound_effect("What is this?")
        assert not _is_sound_effect("That is")
        assert not _is_sound_effect("Here we go")

    def test_layout_choice(self):
        assert _choose_layout("Grrr") == "vertical_sfx"
        assert _choose_layout("!!") == "vertical_sfx"
        assert _choose_layout("Why?") == "horizontal"
        assert _choose_layout("Hello world") == "horizontal"


class TestFuriganaSizing:
    """Furigana should stay readable while layout reserves matching space."""

    def test_furigana_size_is_half_of_main_text(self):
        assert _furigana_font_size(24) == 12
        assert _furigana_font_size(30) == 15

    def test_furigana_size_keeps_minimum(self):
        assert _furigana_font_size(12) == 10
        assert _furigana_font_size(17) == 10
        assert _furigana_font_size(18) == 12

    def test_fit_uses_rendered_width_without_extra_furigana_reserve(self):
        chars = [{"char": "今", "furigana": "きょう"} for _ in range(8)]

        # With a 90px-wide bubble, 29px main text renders as two columns:
        #   2 * (29 main + 14 furigana + 2 gap) == 90px.
        # The fitter should allow that exact rendered width instead of
        # subtracting another furigana column from the available width.
        assert _fit_vertical_font_size(chars, bw=90, bh=200) == 29

    def test_vertical_cells_include_background_padding(self):
        # The old 1.05x cell was sometimes a pixel shorter than the actual
        # white background around CJK glyphs, so the next character could erase
        # the bottom of the previous one in tight stylized bubbles.
        assert _vertical_main_char_height(29) > int(29 * 1.05)
        assert _vertical_furigana_char_height(14) > 14 + 1

    def test_compressed_furigana_stack_uses_measured_spacing(self):
        main_cell = _vertical_main_char_height(29)
        size, cell = _fit_furigana_stack(target_size=14,
                                         total_height=main_cell,
                                         count=2)
        assert size <= 14
        assert cell * 2 <= main_cell

    def test_expanded_furigana_caps_near_original_text_scale(self):
        # Bright whitespace can improve cramped no-mask captions, but should not
        # turn normal narration into oversized headline text.
        assert _cap_expanded_furigana_font_size(40, 20) == 23
        assert _cap_expanded_furigana_font_size(60, 30) == 34

    def test_expanded_furigana_cap_does_not_force_growth(self):
        assert _cap_expanded_furigana_font_size(18, 20) == 18

    def test_source_scale_cap_is_per_bubble(self):
        assert _cap_furigana_to_source_scale(60, 20) == 23
        assert _cap_furigana_to_source_scale(18, 20) == 18

    def test_page_font_limits_bound_normal_dialogue_but_preserve_outliers(self):
        cap, outlier, floor = compute_furigana_page_font_limits([
            20, 21, 22, 30, 45,
        ])

        assert cap == 26
        assert outlier == 34
        assert floor == 19

    def test_page_floor_raises_normal_dialogue_when_it_fits(self):
        assert _floor_furigana_to_page_dialogue_scale(
            20,
            geometric_font_size=30,
            source_font_size=21,
            page_font_floor=24,
            source_outlier_threshold=34,
            char_count=12,
        ) == 24

    def test_page_floor_never_exceeds_geometric_fit(self):
        assert _floor_furigana_to_page_dialogue_scale(
            20,
            geometric_font_size=22,
            source_font_size=21,
            page_font_floor=24,
            source_outlier_threshold=34,
            char_count=12,
        ) == 22

    def test_page_floor_preserves_large_and_short_outliers(self):
        assert _floor_furigana_to_page_dialogue_scale(
            20,
            geometric_font_size=30,
            source_font_size=35,
            page_font_floor=24,
            source_outlier_threshold=34,
            char_count=12,
        ) == 20
        assert _floor_furigana_to_page_dialogue_scale(
            20,
            geometric_font_size=30,
            source_font_size=21,
            page_font_floor=24,
            source_outlier_threshold=34,
            char_count=3,
        ) == 20

    def test_estimates_small_source_text_in_large_bubble(self):
        img = Image.new("RGB", (160, 220), "white")
        draw = ImageDraw.Draw(img)
        for y in range(55, 145, 22):
            draw.rectangle((72, y, 88, y + 12), fill="black")

        size = estimate_source_vertical_font_size(
            img,
            (20, 20, 140, 200),
            "空腹食事",
        )

        assert size is not None
        assert 10 <= size <= 30

    def test_estimates_larger_source_text_larger(self):
        img = Image.new("RGB", (180, 240), "white")
        draw = ImageDraw.Draw(img)
        for y in range(45, 175, 38):
            draw.rectangle((66, y, 106, y + 28), fill="black")

        size = estimate_source_vertical_font_size(
            img,
            (20, 20, 160, 220),
            "空腹食事",
        )

        assert size is not None
        assert size >= 30

    def test_source_estimate_rejects_dense_artwork(self):
        img = Image.new("RGB", (120, 120), (40, 40, 40))

        assert estimate_source_vertical_font_size(
            img,
            (10, 10, 110, 110),
            "空腹",
        ) is None


class TestMaskSafeBBox:
    """Mask-safe vertical text bounds should not over-shrink jagged bubbles."""

    def test_vertical_uses_more_jagged_mask_width_than_horizontal(self):
        mask = np.zeros((120, 100), dtype=np.uint8)
        for y in range(10, 110):
            if y % 4 < 2:
                mask[y, 5:95] = 255
            else:
                mask[y, 20:80] = 255

        horizontal = _mask_safe_bbox((0, 0, 100, 120), mask, vertical=False)
        vertical = _mask_safe_bbox((0, 0, 100, 120), mask, vertical=True)

        assert vertical[0] < horizontal[0]
        assert vertical[2] > horizontal[2]
        assert vertical[3] - vertical[1] > horizontal[3] - horizontal[1]


class TestBrightRegionExpansion:
    """No-mask furigana should use nearby white caption/glow space."""

    def test_expands_tight_text_box_to_bright_region(self):
        img = Image.new("RGB", (140, 180), (90, 90, 90))
        draw = ImageDraw.Draw(img)
        draw.rectangle((25, 20, 105, 160), fill=(245, 245, 245))

        expanded = _expand_bright_region_bbox(
            img,
            (55, 65, 75, 125),
            min_extra_row_height=30,
        )

        assert expanded[0] <= 26
        assert expanded[1] <= 21
        assert expanded[2] >= 104
        assert expanded[3] >= 159

    def test_no_mask_layout_uses_bounded_bright_region(self):
        img = Image.new("RGB", (140, 180), (90, 90, 90))
        draw = ImageDraw.Draw(img)
        draw.rectangle((25, 20, 105, 160), fill=(245, 245, 245))

        expanded = _no_mask_furigana_layout_bbox(
            img,
            (55, 65, 75, 125),
            min_extra_row_height=30,
        )

        assert expanded[0] <= 26
        assert expanded[1] <= 21
        assert expanded[2] >= 104
        assert expanded[3] >= 159

    def test_leaves_box_when_no_bright_region_overlaps(self):
        img = Image.new("RGB", (100, 100), (80, 80, 80))
        bbox = (40, 30, 60, 70)

        assert _expand_bright_region_bbox(img, bbox) == bbox

    def test_caps_large_bright_background_expansion(self):
        img = Image.new("RGB", (400, 500), (245, 245, 245))
        bbox = (190, 220, 210, 280)

        expanded = _expand_bright_region_bbox(
            img,
            bbox,
            min_extra_row_height=30,
        )

        assert expanded[0] <= bbox[0]
        assert expanded[1] <= bbox[1]
        assert expanded[2] >= bbox[2]
        assert expanded[3] >= bbox[3]
        assert expanded[2] - expanded[0] <= 140
        assert expanded[3] - expanded[1] <= 180

    def test_rejects_unbounded_bright_region_when_required(self):
        img = Image.new("RGB", (120, 200), (80, 80, 80))
        draw = ImageDraw.Draw(img)
        draw.rectangle((40, 0, 80, 200), fill=(245, 245, 245))
        bbox = (52, 80, 68, 120)

        assert _expand_bright_region_bbox(
            img,
            bbox,
            min_extra_row_height=30,
            require_bounded_region=True,
        ) == bbox

    def test_limited_no_mask_fallback_stays_near_original_text_area(self):
        img = Image.new("RGB", (400, 500), (245, 245, 245))
        bbox = (190, 220, 210, 280)

        expanded = _expand_limited_no_mask_bbox(
            img,
            bbox,
            min_extra_row_height=30,
        )

        assert expanded[0] < bbox[0]
        assert expanded[1] < bbox[1]
        assert expanded[2] > bbox[2]
        assert expanded[3] > bbox[3]
        assert expanded[2] - expanded[0] <= 52
        assert expanded[3] - expanded[1] <= 108

    def test_no_mask_layout_fallback_rejects_open_page_gutter(self):
        img = Image.new("RGB", (400, 500), (245, 245, 245))
        bbox = (190, 220, 210, 280)

        expanded = _no_mask_furigana_layout_bbox(
            img,
            bbox,
            min_extra_row_height=30,
        )

        assert expanded[2] - expanded[0] <= 52
        assert expanded[3] - expanded[1] <= 108

    def test_requires_an_extra_vertical_row_of_border_space(self):
        img = Image.new("RGB", (140, 180), (90, 90, 90))
        draw = ImageDraw.Draw(img)
        draw.rectangle((25, 20, 105, 160), fill=(245, 245, 245))
        bbox = (55, 35, 75, 145)

        assert _expand_bright_region_bbox(
            img,
            bbox,
            min_extra_row_height=40,
        ) == bbox

    def test_tighten_shrinks_bbox_extending_past_bright_plate(self):
        # White plate occupies 30..90 horizontally, 40..70 vertically.
        # bbox is wider than the plate, bleeding onto dark background.
        img = Image.new("RGB", (200, 120), (40, 40, 40))
        draw = ImageDraw.Draw(img)
        draw.rectangle((30, 40, 90, 70), fill=(245, 245, 245))
        bbox = (20, 35, 140, 75)

        tightened = _tighten_no_mask_bbox_to_bright_region(img, bbox)

        assert tightened[0] >= 25  # left edge moved toward the plate
        assert tightened[2] <= 95  # right edge no longer past the plate
        assert tightened[0] < tightened[2]
        assert tightened[1] < tightened[3]

    def test_tighten_leaves_well_fitted_bbox_alone(self):
        # bbox already matches the bright plate — nothing to tighten.
        img = Image.new("RGB", (160, 120), (40, 40, 40))
        draw = ImageDraw.Draw(img)
        draw.rectangle((30, 40, 130, 100), fill=(245, 245, 245))
        bbox = (30, 40, 130, 100)

        assert _tighten_no_mask_bbox_to_bright_region(img, bbox) == bbox

    def test_tighten_leaves_bbox_with_no_bright_region(self):
        img = Image.new("RGB", (120, 120), (40, 40, 40))
        bbox = (10, 10, 90, 90)

        assert _tighten_no_mask_bbox_to_bright_region(img, bbox) == bbox

    def test_wide_short_bbox_routes_to_horizontal_furigana(self):
        # Wide-short bbox (chapter title strip) should not crash when fed to
        # the vertical entry point; the dispatcher renders it horizontally.
        img = Image.new("RGB", (1200, 200), "white")
        segments = [{"text": "現在60話エンジェルナンバー", "furigana": "げんざい",
                     "needs_furigana": True}]
        bbox = (50, 50, 1150, 150)  # 1100x100, ratio 11.0
        bw = bbox[2] - bbox[0]
        bh = bbox[3] - bbox[1]
        assert bw >= bh * _WIDE_BBOX_HORIZONTAL_RATIO
        # Should complete without exceptions and modify the image.
        before = img.tobytes()
        render_furigana_vertical(img, bbox, segments)
        assert img.tobytes() != before

    def test_rejects_normal_balloons_with_other_text_in_border_space(self):
        img = Image.new("RGB", (220, 220), (90, 90, 90))
        draw = ImageDraw.Draw(img)
        draw.rectangle((25, 20, 195, 200), fill=(245, 245, 245))
        # Simulate neighboring vertical text columns inside the same balloon.
        for x in (55, 135):
            draw.rectangle((x, 55, x + 9, 165), fill=(20, 20, 20))
            draw.rectangle((x + 18, 70, x + 25, 150), fill=(20, 20, 20))
        bbox = (92, 80, 112, 140)

        assert _expand_bright_region_bbox(
            img,
            bbox,
            min_extra_row_height=30,
        ) == bbox

    def test_render_uses_layout_image_for_no_mask_expansion(self, monkeypatch):
        target = Image.new("RGB", (120, 160), "white")
        source = Image.new("RGB", (120, 160), "gray")
        seen = []

        def fake_layout(img, bbox, min_extra_row_height=None):
            seen.append(img)
            return bbox

        monkeypatch.setattr(text_renderer, "_no_mask_furigana_layout_bbox", fake_layout)

        render_furigana_vertical(
            target,
            (40, 30, 80, 130),
            [{"text": "空腹", "furigana": "くうふく", "needs_furigana": True}],
            layout_img=source,
        )

        assert seen == [source]

    def test_render_defaults_to_target_for_no_mask_layout(self, monkeypatch):
        target = Image.new("RGB", (120, 160), "white")
        seen = []

        def fake_layout(img, bbox, min_extra_row_height=None):
            seen.append(img)
            return bbox

        monkeypatch.setattr(text_renderer, "_no_mask_furigana_layout_bbox", fake_layout)

        render_furigana_vertical(
            target,
            (40, 30, 80, 130),
            [{"text": "空腹", "furigana": "くうふく", "needs_furigana": True}],
        )

        assert seen == [target]


class TestWordWrap:
    """Test word wrapping only breaks words when they don't fit."""

    def test_short_text_single_line(self):
        draw, font = _make_draw_and_font(16)
        lines = _word_wrap("Hi", font, 500, draw)
        assert lines == ["Hi"]

    def test_wraps_at_word_boundary(self):
        draw, font = _make_draw_and_font(16)
        lines = _word_wrap("one two three four five six", font, 100, draw)
        assert len(lines) > 1
        # No hyphens in output — words fit without breaking
        for line in lines:
            assert "-" not in line, f"Unexpected hyphen in '{line}'"

    def test_breaks_long_word_with_hyphen(self):
        draw, font = _make_draw_and_font(16)
        # Very narrow width forces word to break
        lines = _word_wrap("corresponding", font, 40, draw)
        assert len(lines) > 1
        # At least one fragment should have a hyphen (except last)
        has_hyphen = any("-" in line for line in lines[:-1])
        assert has_hyphen, f"Expected hyphen in broken word, got: {lines}"

    def test_no_premature_hyphenation(self):
        """Words should NOT be pre-hyphenated when they fit on a line."""
        draw, font = _make_draw_and_font(14)
        # Wide enough to fit "corresponding" without breaking
        lines = _word_wrap("corresponding", font, 500, draw)
        assert lines == ["corresponding"], f"Should not hyphenate, got: {lines}"
