"""Regression tests for text rendering: SFX detection, hyphenation, word wrap."""

from PIL import Image, ImageDraw, ImageFont
from kindle.text_renderer import (
    _is_sound_effect,
    _word_wrap,
    _break_word_to_fit,
    _choose_layout,
)
from kindle.config import FONT_EN


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
