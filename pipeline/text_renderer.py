"""Text rendering for manga bubbles: vertical Japanese w/ furigana and English."""

import logging
import re

from PIL import Image, ImageDraw, ImageFont

from .config import (
    FONT_JP,
    FONT_EN,
    FURIGANA_SIZE_RATIO,
    MIN_FONT_SIZE,
    MAX_FONT_SIZE,
    TEXT_MARGIN,
    BUBBLE_PADDING,
)

# Padding around each text element's white background
_TEXT_BG_PAD = 2

log = logging.getLogger(__name__)

# --- Hyphenation ---

# Common syllable break points for English — simple heuristic, not a full hyphenation algorithm
_VOWELS = set("aeiouyAEIOUY")


def _hyphenate_words(words: list[str], max_chars: int = 6) -> list[str]:
    """Break long words with hyphens for better vertical fitting.

    Words longer than max_chars get split at reasonable points.
    Short words pass through unchanged.
    """
    result = []
    for word in words:
        if len(word) <= max_chars:
            result.append(word)
            continue

        parts = _split_word(word, max_chars)
        result.extend(parts)

    return result


def _split_word(word: str, max_len: int) -> list[str]:
    """Split a word into hyphenated parts at syllable-ish boundaries."""
    # Strip trailing punctuation, re-attach later
    tail = ""
    core = word
    while core and core[-1] in ".,!?;:":
        tail = core[-1] + tail
        core = core[:-1]

    if len(core) <= max_len:
        return [core + tail]

    parts = []
    pos = 0
    while pos < len(core):
        if len(core) - pos <= max_len:
            parts.append(core[pos:])
            break

        # Find best break point: prefer after a consonant before a vowel
        best = min(max_len, len(core) - pos)
        for i in range(min(max_len, len(core) - pos) - 1, max(2, max_len // 2) - 1, -1):
            ch = core[pos + i]
            prev = core[pos + i - 1] if i > 0 else ""
            # Break before a vowel that follows a consonant (syllable boundary)
            if ch in _VOWELS and prev and prev not in _VOWELS:
                best = i
                break
            # Break after a vowel followed by a consonant
            if prev in _VOWELS and ch not in _VOWELS and i < max_len:
                best = i
                break

        parts.append(core[pos:pos + best] + "-")
        pos += best

    # Re-attach punctuation to last part
    if tail and parts:
        parts[-1] = parts[-1] + tail

    return parts


# --- Text classification ---

# Patterns that indicate sound effects / exclamations
_SFX_PATTERNS = [
    re.compile(r'([a-zA-Z])\1{2,}'),                   # Repeated letters: Grrr, Aaaa
    re.compile(r'^(Ugh|Guh|Tch|Grr|Ahh|Gah|Bam|Wham|Crack|Snap|Boom|Thud|Slash|Crash|Pow|Zap)!*$', re.IGNORECASE),
]

def _is_sound_effect(text: str) -> bool:
    """Detect if text is a sound effect or short exclamation.

    Must be selective: "Guh!!", "!!", "Grrrr" are SFX.
    "Why?", "Stop!", "But..." are NOT (they're dialogue).
    """
    stripped = text.strip()

    # Pure punctuation: "!!", "...", "...!"
    alpha = re.sub(r'[^a-zA-Z]', '', stripped)
    if not alpha:
        return True

    # Only one "word" (no spaces) and matches SFX patterns
    if ' ' not in stripped:
        for pat in _SFX_PATTERNS:
            if pat.search(stripped):
                return True

    return False


def _choose_layout(text: str) -> str:
    """Decide rendering layout: 'vertical_sfx' or 'horizontal'."""
    if _is_sound_effect(text):
        return "vertical_sfx"
    return "horizontal"


# --- Font loading ---

def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        log.warning("Could not load font %s, using default", path)
        return ImageFont.load_default()


# --- English rendering entry point ---

def render_english(img: Image.Image, bbox: tuple[int, int, int, int],
                   text: str, base_font_size: int | None = None) -> None:
    """Render English text inside a bubble, choosing the best layout."""
    x1, y1, x2, y2 = bbox
    bw = x2 - x1 - 2 * TEXT_MARGIN
    bh = y2 - y1 - 2 * TEXT_MARGIN

    if bw < 10 or bh < 10 or not text.strip():
        return

    layout = _choose_layout(text)
    log.debug("  Layout: %s for '%s' in %dx%d", layout, text, bw, bh)

    if layout == "vertical_sfx":
        _render_vertical_sfx(img, bbox, text)
    else:
        _render_horizontal_english(img, bbox, text, base_font_size)


# --- Vertical sound effect rendering ---

def _render_vertical_sfx(img: Image.Image, bbox: tuple[int, int, int, int],
                         text: str) -> None:
    """Render a sound effect / short exclamation vertically with large font."""
    x1, y1, x2, y2 = bbox
    bw = x2 - x1 - 2 * TEXT_MARGIN
    bh = y2 - y1 - 2 * TEXT_MARGIN

    # Strip ellipsis for cleaner display, keep punctuation
    display = text.strip()
    chars = list(display)
    if not chars:
        return

    # Find largest font size where all chars fit stacked vertically
    font_size = _fit_vertical_chars(chars, bw, bh)
    font = _load_font(FONT_EN, font_size)
    draw = ImageDraw.Draw(img)

    char_h = int(font_size * 1.1)
    total_h = len(chars) * char_h

    # Center the stack in the bubble
    start_y = y1 + TEXT_MARGIN + max(0, (bh - total_h) // 2)
    cx = x1 + TEXT_MARGIN + bw // 2

    for ch in chars:
        ch_bbox = draw.textbbox((0, 0), ch, font=font)
        ch_w = ch_bbox[2] - ch_bbox[0]
        ch_h = ch_bbox[3] - ch_bbox[1]
        tx = cx - ch_w // 2
        # Offset background by bbox origin (font ascender shifts ink down/right)
        bg_x = tx + ch_bbox[0]
        bg_y = start_y + ch_bbox[1]
        draw.rectangle(
            (bg_x - _TEXT_BG_PAD, bg_y - _TEXT_BG_PAD,
             bg_x + ch_w + _TEXT_BG_PAD, bg_y + ch_h + _TEXT_BG_PAD),
            fill="white",
        )
        draw.text((tx, start_y), ch, fill="black", font=font)
        start_y += char_h
        if start_y + char_h > y2 - TEXT_MARGIN:
            break


def _fit_vertical_chars(chars: list[str], bw: int, bh: int) -> int:
    """Find largest font size for vertically stacked single characters."""
    lo, hi = MIN_FONT_SIZE, min(MAX_FONT_SIZE, bw, bh)
    best = lo

    for _ in range(15):
        mid = (lo + hi) // 2
        char_h = int(mid * 1.1)
        total_h = len(chars) * char_h

        if total_h <= bh and mid <= bw:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    return best


# --- Horizontal English ---

def _render_horizontal_english(img: Image.Image, bbox: tuple[int, int, int, int],
                               text: str,
                               base_font_size: int | None = None) -> None:
    """Render horizontal English text centered inside a bubble region.

    Words are only hyphenated during word wrap when they don't fit on a line.
    """
    x1, y1, x2, y2 = bbox
    bw = x2 - x1 - 2 * TEXT_MARGIN
    bh = y2 - y1 - 2 * TEXT_MARGIN

    font_size = _fit_horizontal_english_size(text, bw, bh, base_font_size)
    font = _load_font(FONT_EN, font_size)
    draw = ImageDraw.Draw(img)

    lines = _word_wrap(text, font, bw, draw)
    if not lines:
        return

    line_height = int(font_size * 1.3)
    total_height = len(lines) * line_height

    text_y = y1 + TEXT_MARGIN + (bh - total_height) // 2

    for line in lines:
        line_bbox = draw.textbbox((0, 0), line, font=font)
        line_w = line_bbox[2] - line_bbox[0]
        line_h = line_bbox[3] - line_bbox[1]
        text_x = x1 + TEXT_MARGIN + (bw - line_w) // 2

        # Offset background by bbox origin (font ascender shifts ink down/right)
        bg_x = text_x + line_bbox[0]
        bg_y = text_y + line_bbox[1]
        draw.rectangle(
            (bg_x - _TEXT_BG_PAD, bg_y - _TEXT_BG_PAD,
             bg_x + line_w + _TEXT_BG_PAD, bg_y + line_h + _TEXT_BG_PAD),
            fill="white",
        )
        draw.text((text_x, text_y), line, fill="black", font=font)
        text_y += line_height


def _fit_horizontal_english_size(text: str, bw: int, bh: int,
                                 base_font_size: int | None = None) -> int:
    """Binary search for the largest horizontal English font size."""
    upper = base_font_size if base_font_size is not None else MAX_FONT_SIZE
    lo, hi = MIN_FONT_SIZE, min(upper, bh)
    best = lo

    for _ in range(15):
        mid = (lo + hi) // 2
        font = _load_font(FONT_EN, mid)
        draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))

        lines = _word_wrap(text, font, bw, draw)
        line_height = int(mid * 1.3)
        total_height = len(lines) * line_height

        if total_height <= bh and len(lines) > 0:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    return best


def _word_wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int,
               draw: ImageDraw.ImageDraw) -> list[str]:
    """Wrap text into lines that fit within max_width pixels.

    Words that are wider than max_width are broken with hyphens at
    syllable-ish boundaries (only when needed, not pre-emptively).
    """
    words = text.split()
    if not words:
        return []

    lines = []
    current = ""

    for word in words:
        if not current:
            test = word
        else:
            test = current + " " + word

        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            # Check if this single word is wider than max_width
            word_bbox = draw.textbbox((0, 0), word, font=font)
            if word_bbox[2] - word_bbox[0] > max_width:
                # Break the word into fragments that fit
                fragments = _break_word_to_fit(word, font, max_width, draw)
                for frag in fragments[:-1]:
                    lines.append(frag)
                current = fragments[-1]
            else:
                current = word

    if current:
        lines.append(current)
    return lines


def _break_word_to_fit(word: str, font: ImageFont.FreeTypeFont,
                       max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """Break a word into hyphenated fragments that each fit within max_width."""
    fragments = []
    remaining = word

    while remaining:
        # Try progressively shorter prefixes
        for end in range(len(remaining), 0, -1):
            fragment = remaining[:end]
            if end < len(remaining):
                display = fragment + "-"
            else:
                display = fragment

            bbox = draw.textbbox((0, 0), display, font=font)
            if bbox[2] - bbox[0] <= max_width and end > 0:
                fragments.append(display)
                remaining = remaining[end:]
                break
        else:
            # Even a single char doesn't fit — force it
            fragments.append(remaining[0])
            remaining = remaining[1:]

    return fragments if fragments else [word]


# --- Vertical Japanese with furigana ---

def render_furigana_vertical(img: Image.Image, bbox: tuple[int, int, int, int],
                             segments: list[dict]) -> None:
    """Render vertical Japanese text with furigana inside a bubble region."""
    x1, y1, x2, y2 = bbox
    bw = x2 - x1 - 2 * TEXT_MARGIN
    bh = y2 - y1 - 2 * TEXT_MARGIN

    if bw < 10 or bh < 10:
        return

    chars = []
    for seg in segments:
        furigana_text = seg.get("furigana")
        for i, ch in enumerate(seg["text"]):
            ch_furi = None
            if furigana_text and seg["needs_furigana"]:
                seg_len = len(seg["text"])
                furi_len = len(furigana_text)
                start = int(i * furi_len / seg_len)
                end = int((i + 1) * furi_len / seg_len)
                ch_furi = furigana_text[start:end] if end > start else None
            chars.append({"char": ch, "furigana": ch_furi})

    if not chars:
        return

    font_size = _fit_vertical_font_size(chars, bw, bh)
    furi_size = max(MIN_FONT_SIZE, int(font_size * FURIGANA_SIZE_RATIO))

    font = _load_font(FONT_JP, font_size)
    furi_font = _load_font(FONT_JP, furi_size)

    draw = ImageDraw.Draw(img)

    col_width = font_size + furi_size + 4
    char_height = int(font_size * 1.15)

    furi_space = furi_size + 2 if any(c["furigana"] for c in chars) else 0

    # Center columns horizontally in the bubble.  Right-aligning pushes
    # the first column against the right edge, which can overflow curved
    # bubble walls (the bbox corresponds to the widest point, but the
    # bubble narrows at top/bottom).  Centering distributes the margin
    # equally on both sides.
    chars_per_col = max(1, bh // char_height)
    cols_needed = (len(chars) + chars_per_col - 1) // chars_per_col
    block_width = font_size + furi_space + max(0, cols_needed - 1) * col_width
    usable_center = x1 + TEXT_MARGIN + bw // 2
    start_x = int(usable_center - font_size - furi_space + block_width / 2)
    # Clamp so the block doesn't overflow on the right
    max_start_x = x2 - TEXT_MARGIN - font_size - furi_space
    start_x = min(start_x, max_start_x)
    start_y = y1 + TEXT_MARGIN

    col_x = start_x
    char_y = start_y

    for ch_info in chars:
        if char_y + char_height > y2 - TEXT_MARGIN:
            col_x -= col_width
            char_y = start_y
            if col_x < x1 + TEXT_MARGIN:
                break

        main_bbox = draw.textbbox((0, 0), ch_info["char"], font=font)
        main_w = main_bbox[2] - main_bbox[0]
        main_h = main_bbox[3] - main_bbox[1]
        # Offset background by bbox origin (font ascender shifts ink down/right)
        mbg_x = col_x + main_bbox[0]
        mbg_y = char_y + main_bbox[1]
        draw.rectangle(
            (mbg_x - _TEXT_BG_PAD, mbg_y - _TEXT_BG_PAD,
             mbg_x + main_w + _TEXT_BG_PAD, mbg_y + main_h + _TEXT_BG_PAD),
            fill="white",
        )
        draw.text((col_x, char_y), ch_info["char"], fill="black", font=font)

        if ch_info["furigana"]:
            furi_x = col_x + font_size + 1
            furi_char_h = furi_size + 1
            furi_total_h = len(ch_info["furigana"]) * furi_char_h
            # Compress furigana spacing when stack overflows char_height
            if furi_total_h > char_height and len(ch_info["furigana"]) > 1:
                furi_char_h = char_height // len(ch_info["furigana"])
                furi_total_h = len(ch_info["furigana"]) * furi_char_h
            furi_y = char_y + (char_height - furi_total_h) // 2
            for fc in ch_info["furigana"]:
                if furi_x + furi_size <= x2 - BUBBLE_PADDING:
                    fc_bbox = draw.textbbox((0, 0), fc, font=furi_font)
                    fc_w = fc_bbox[2] - fc_bbox[0]
                    fc_h = fc_bbox[3] - fc_bbox[1]
                    # Offset background by bbox origin
                    fbg_x = furi_x + fc_bbox[0]
                    fbg_y = furi_y + fc_bbox[1]
                    draw.rectangle(
                        (fbg_x - 1, fbg_y - 1,
                         fbg_x + fc_w + 1, fbg_y + fc_h + 1),
                        fill="white",
                    )
                    draw.text((furi_x, furi_y), fc, fill="black", font=furi_font)
                furi_y += furi_char_h

        char_y += char_height


def _fit_vertical_font_size(chars: list[dict], bw: int, bh: int) -> int:
    """Binary search for the largest font size that fits all characters."""
    has_furigana = any(c["furigana"] for c in chars)
    n = len(chars)

    lo, hi = MIN_FONT_SIZE, min(MAX_FONT_SIZE, bh // 2)
    best = lo

    for _ in range(15):
        mid = (lo + hi) // 2
        furi_extra = int(mid * FURIGANA_SIZE_RATIO) + 4 if has_furigana else 0
        col_width = mid + furi_extra
        char_height = int(mid * 1.15)

        # Reserve space for the first column's furigana extending past the rightmost kanji
        furi_offset = (int(mid * FURIGANA_SIZE_RATIO) + 2) if has_furigana else 0
        available_width = bw - furi_offset

        chars_per_col = max(1, bh // char_height)
        cols_needed = (n + chars_per_col - 1) // chars_per_col
        total_width = cols_needed * col_width

        if total_width <= available_width:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    return best


# --- Artwork text rendering ---

def render_english_on_artwork(img: Image.Image, bbox: tuple[int, int, int, int],
                               text: str, base_font_size: int | None = None) -> None:
    """Render English text on inpainted artwork with semi-transparent background.

    Similar to render_english but adds a translucent white background
    rectangle for readability on complex artwork backgrounds.
    """
    x1, y1, x2, y2 = bbox
    bw = x2 - x1 - 2 * TEXT_MARGIN
    bh = y2 - y1 - 2 * TEXT_MARGIN

    if bw < 10 or bh < 10 or not text.strip():
        return

    layout = _choose_layout(text)
    if layout == "vertical_sfx":
        _render_vertical_sfx(img, bbox, text)
        return

    font_size = _fit_horizontal_english_size(text, bw, bh, base_font_size)
    font = _load_font(FONT_EN, font_size)
    draw = ImageDraw.Draw(img)

    lines = _word_wrap(text, font, bw, draw)
    if not lines:
        return

    line_height = int(font_size * 1.3)
    total_height = len(lines) * line_height

    # Draw semi-transparent background for readability
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    bg_pad = 6
    bg_y1 = y1 + TEXT_MARGIN + (bh - total_height) // 2 - bg_pad
    bg_y2 = bg_y1 + total_height + 2 * bg_pad
    overlay_draw.rectangle(
        (x1 + TEXT_MARGIN - bg_pad, bg_y1, x2 - TEXT_MARGIN + bg_pad, bg_y2),
        fill=(255, 255, 255, 180),
    )
    img_rgba = img.convert("RGBA")
    img_rgba = Image.alpha_composite(img_rgba, overlay)
    # Paste back as RGB
    img.paste(img_rgba.convert("RGB"))

    # Draw text on top
    draw = ImageDraw.Draw(img)
    text_y = y1 + TEXT_MARGIN + (bh - total_height) // 2

    for line in lines:
        line_bbox = draw.textbbox((0, 0), line, font=font)
        line_w = line_bbox[2] - line_bbox[0]
        text_x = x1 + TEXT_MARGIN + (bw - line_w) // 2
        draw.text((text_x, text_y), line, fill="black", font=font)
        text_y += line_height


# --- Debug ---

def draw_debug_boxes(img: Image.Image, bubbles: list[dict]) -> Image.Image:
    """Draw bounding boxes on the image for debugging."""
    debug_img = img.copy()
    draw = ImageDraw.Draw(debug_img)

    colors = {
        "speech_bubble": "red",
        "narration_box": "blue",
        "sound_effect": "green",
    }

    for b in bubbles:
        color = colors.get(b.get("type", ""), "yellow")
        draw.rectangle(b["bbox"], outline=color, width=2)

    return debug_img
