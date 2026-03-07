"""Text rendering for manga bubbles: vertical Japanese w/ furigana and English."""

import logging
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .config import (
    FONT_JP,
    FONT_EN_BOLD,
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


# --- Mask-aware bounding box ---

def _mask_safe_bbox(bbox: tuple[int, int, int, int],
                    mask: np.ndarray,
                    vertical: bool = False) -> tuple[int, int, int, int]:
    """Compute a tighter bbox that fits inside the bubble mask.

    For each row in the mask, find the horizontal extent of filled pixels.
    In horizontal mode (default): uses 15% vertical inset and 30th-percentile
    width — conservative to prevent lines near the top/bottom from overflowing
    oval bubbles.  In vertical mode: uses 8% vertical inset and median width,
    since vertical Japanese text stacks characters individually and mask-clips
    per-character.
    """
    x1, y1, x2, y2 = bbox
    crop = mask[y1:y2, x1:x2]
    h, w = crop.shape[:2]
    if h < 4 or w < 4:
        return bbox

    # Per-row: does the row have any mask pixels?
    row_any = np.any(crop > 0, axis=1)
    filled = np.where(row_any)[0]
    if len(filled) < 4:
        return bbox

    mask_top, mask_bot = int(filled[0]), int(filled[-1])
    mask_h = mask_bot - mask_top + 1

    # Vertical inset: 8% for vertical Japanese (characters clip individually),
    # 15% for horizontal English (lines can overflow narrow oval edges).
    v_pct = 8 if vertical else 15
    v_inset = max(2, mask_h * v_pct // 100)
    safe_top = mask_top + v_inset
    safe_bot = mask_bot - v_inset
    if safe_top >= safe_bot:
        return bbox

    # Horizontal: use 30th-percentile width (safe for ~60% of rows).
    # This is more conservative than median, avoiding clipping on the
    # narrower rows near the edges of the safe vertical range.
    lefts = []
    rights = []
    for r in range(safe_top, safe_bot + 1):
        cols = np.where(crop[r] > 0)[0]
        if len(cols) > 0:
            lefts.append(int(cols[0]))
            rights.append(int(cols[-1]))

    if not lefts:
        return bbox

    # Vertical text uses median (50th pct) — less conservative since
    # characters are individually mask-clipped.  Horizontal uses 30th pct.
    width_pct = 50 if vertical else 30
    safe_left = int(np.percentile(lefts, 100 - width_pct))
    safe_right = int(np.percentile(rights, width_pct))

    if safe_left >= safe_right:
        return bbox

    return (x1 + safe_left, y1 + safe_top, x1 + safe_right + 1, y1 + safe_bot + 1)


# --- Font size pre-computation for page-level normalization ---

def compute_bubble_font_size(bbox: tuple[int, int, int, int],
                              text: str,
                              base_font_size: int | None = None,
                              mask: 'np.ndarray | None' = None) -> int | None:
    """Compute the fitted font size for horizontal English text in a bubble.

    Uses mask-safe bbox (both width and height) so the computed size matches
    what will actually be visible after mask clipping.  Returns None for
    SFX/vertical layout so the caller can skip normalization for those.
    """
    if _choose_layout(text) == "vertical_sfx":
        return None

    if mask is not None:
        safe = _mask_safe_bbox(bbox, mask)
        bw = safe[2] - safe[0] - 2 * TEXT_MARGIN
        bh = safe[3] - safe[1] - 2 * TEXT_MARGIN
    else:
        x1, y1, x2, y2 = bbox
        bw = x2 - x1 - 2 * TEXT_MARGIN
        bh = y2 - y1 - 2 * TEXT_MARGIN

    if bw < 10 or bh < 10:
        return None

    return _fit_horizontal_english_size(text, bw, bh, base_font_size)


# --- English rendering entry point ---

def render_english(img: Image.Image, bbox: tuple[int, int, int, int],
                   text: str, base_font_size: int | None = None,
                   mask: 'np.ndarray | None' = None) -> None:
    """Render English text inside a bubble, choosing the best layout.

    ``base_font_size`` is the page-wide target computed from image height.
    All bubbles try to use this size for consistency; smaller bubbles shrink
    as needed.  When `mask` is provided, horizontal text uses the mask-safe
    bbox for sizing, wrapping, and positioning so text stays inside the
    visible bubble area, with mask-clip as a safety net.
    """
    x1, y1, x2, y2 = bbox
    bw = x2 - x1 - 2 * TEXT_MARGIN
    bh = y2 - y1 - 2 * TEXT_MARGIN

    if bw < 10 or bh < 10 or not text.strip():
        return

    layout = _choose_layout(text)
    log.debug("  Layout: %s for '%s' in %dx%d", layout, text, bw, bh)

    if layout == "vertical_sfx":
        _render_vertical_sfx(img, bbox, text, mask=mask)
    else:
        _render_horizontal_english(img, bbox, text,
                                   target_size=base_font_size, mask=mask)


# --- Vertical sound effect rendering ---

def _render_vertical_sfx(img: Image.Image, bbox: tuple[int, int, int, int],
                         text: str,
                         mask: 'np.ndarray | None' = None) -> None:
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
    font = _load_font(FONT_EN_BOLD, font_size)

    char_h = int(font_size * 1.1)
    total_h = len(chars) * char_h

    # Center the stack in the bubble
    start_y = y1 + TEXT_MARGIN + max(0, (bh - total_h) // 2)
    cx = x1 + TEXT_MARGIN + bw // 2

    if mask is not None:
        # Render to RGBA overlay, then mask-clip
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
    else:
        draw = ImageDraw.Draw(img)

    for ch in chars:
        ch_bbox = draw.textbbox((0, 0), ch, font=font)
        ch_w = ch_bbox[2] - ch_bbox[0]
        ch_h = ch_bbox[3] - ch_bbox[1]
        tx = cx - ch_w // 2
        # Offset background by bbox origin (font ascender shifts ink down/right)
        bg_x = tx + ch_bbox[0]
        bg_y = start_y + ch_bbox[1]
        bg_fill = (255, 255, 255, 255) if mask is not None else "white"
        text_fill = (0, 0, 0, 255) if mask is not None else "black"
        draw.rectangle(
            (bg_x - _TEXT_BG_PAD, bg_y - _TEXT_BG_PAD,
             bg_x + ch_w + _TEXT_BG_PAD, bg_y + ch_h + _TEXT_BG_PAD),
            fill=bg_fill,
        )
        draw.text((tx, start_y), ch, fill=text_fill, font=font)
        start_y += char_h
        if start_y + char_h > y2 - TEXT_MARGIN:
            break

    if mask is not None:
        overlay_arr = np.array(overlay)
        overlay_arr[:, :, 3][mask == 0] = 0
        overlay = Image.fromarray(overlay_arr)
        img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))


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
                               target_size: int | None = None,
                               mask: 'np.ndarray | None' = None) -> None:
    """Render horizontal English text centered inside a bubble region.

    ``target_size`` is the page-wide font target for consistency.  Text
    starts at this size and shrinks only if it doesn't fit.  Words are
    hyphenated during word wrap when they don't fit on a line.
    When `mask` is provided, uses mask-safe bbox for sizing, wrapping,
    and positioning so text stays inside the visible bubble area, then
    renders to an RGBA overlay and clips to mask as a safety net.
    """
    # Use mask-safe bbox for sizing/wrapping/positioning when mask available
    if mask is not None:
        sx1, sy1, sx2, sy2 = _mask_safe_bbox(bbox, mask)
    else:
        sx1, sy1, sx2, sy2 = bbox

    bw = sx2 - sx1 - 2 * TEXT_MARGIN
    bh = sy2 - sy1 - 2 * TEXT_MARGIN

    if bw < 10 or bh < 10:
        return

    font_size = _fit_horizontal_english_size(text, bw, bh, target_size)
    font = _load_font(FONT_EN_BOLD, font_size)

    if mask is not None:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
    else:
        draw = ImageDraw.Draw(img)

    lines = _word_wrap(text, font, bw, draw)
    if not lines:
        return

    line_height = int(font_size * 1.2)
    total_height = len(lines) * line_height

    text_y = sy1 + TEXT_MARGIN + (bh - total_height) // 2

    for line in lines:
        line_bbox = draw.textbbox((0, 0), line, font=font)
        line_w = line_bbox[2] - line_bbox[0]
        line_h = line_bbox[3] - line_bbox[1]
        text_x = sx1 + TEXT_MARGIN + (bw - line_w) // 2

        # Offset background by bbox origin (font ascender shifts ink down/right)
        bg_x = text_x + line_bbox[0]
        bg_y = text_y + line_bbox[1]
        bg_fill = (255, 255, 255, 255) if mask is not None else "white"
        text_fill = (0, 0, 0, 255) if mask is not None else "black"
        draw.rectangle(
            (bg_x - _TEXT_BG_PAD, bg_y - _TEXT_BG_PAD,
             bg_x + line_w + _TEXT_BG_PAD, bg_y + line_h + _TEXT_BG_PAD),
            fill=bg_fill,
        )
        draw.text((text_x, text_y), line, fill=text_fill, font=font)
        text_y += line_height

    if mask is not None:
        overlay_arr = np.array(overlay)
        overlay_arr[:, :, 3][mask == 0] = 0
        overlay = Image.fromarray(overlay_arr)
        img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))


def _fit_horizontal_english_size(text: str, bw: int, bh: int,
                                 target_size: int | None = None) -> int:
    """Binary search for the largest English font that fits the bubble.

    When ``target_size`` is given (page-wide target), uses it as the upper
    bound so all bubbles on a page share a consistent size.  Small bubbles
    that can't fit at the target will shrink.  Without a target, uses the
    bubble height as the upper bound.  MAX_FONT_SIZE is always enforced as
    a hard cap to prevent oversized text in large bubbles.
    """
    upper = min(target_size, bh) if target_size else bh
    upper = min(upper, MAX_FONT_SIZE)
    lo, hi = MIN_FONT_SIZE, upper
    best = lo

    for _ in range(15):
        mid = (lo + hi) // 2
        font = _load_font(FONT_EN_BOLD, mid)
        draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))

        lines = _word_wrap(text, font, bw, draw)
        line_height = int(mid * 1.2)
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


# Characters that need 90° CW rotation in vertical text
_VERTICAL_ROTATE_CHARS = frozenset("ー～—")

def render_furigana_vertical(img: Image.Image, bbox: tuple[int, int, int, int],
                             segments: list[dict],
                             mask: 'np.ndarray | None' = None) -> None:
    """Render vertical Japanese text with furigana inside a bubble region.

    When `mask` is provided, the bbox is first tightened to the mask's
    interior (same as English rendering) so text is sized to the actual
    bubble shape, then renders to an RGBA overlay and clips to mask.
    """
    if mask is not None:
        bbox = _mask_safe_bbox(bbox, mask, vertical=True)

    x1, y1, x2, y2 = bbox
    bw = x2 - x1 - 2 * TEXT_MARGIN
    bh = y2 - y1 - 2 * TEXT_MARGIN

    if bw < 10 or bh < 10:
        return

    chars = []
    for seg in segments:
        furigana_text = seg.get("furigana")
        seg_text = seg["text"]
        for i, ch in enumerate(seg_text):
            ch_furi = None
            if furigana_text and seg["needs_furigana"]:
                seg_len = len(seg_text)
                furi_len = len(furigana_text)
                start = int(i * furi_len / seg_len)
                end = int((i + 1) * furi_len / seg_len)
                ch_furi = furigana_text[start:end] if end > start else None
            chars.append({
                "char": ch,
                "furigana": ch_furi,
                "seg_start": i == 0,
                "seg_len": len(seg_text),
            })

    if not chars:
        return

    font_size = _fit_vertical_font_size(chars, bw, bh)
    furi_size = max(MIN_FONT_SIZE, int(font_size * FURIGANA_SIZE_RATIO))

    font = _load_font(FONT_JP, font_size)
    furi_font = _load_font(FONT_JP, furi_size)

    if mask is not None:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        bg_fill = (255, 255, 255, 255)
        text_fill = (0, 0, 0, 255)
        furi_bg_fill = (255, 255, 255, 255)
    else:
        draw = ImageDraw.Draw(img)
        bg_fill = "white"
        text_fill = "black"
        furi_bg_fill = "white"

    col_width = font_size + furi_size + 2
    char_height = int(font_size * 1.05)

    furi_space = furi_size + 2 if any(c["furigana"] for c in chars) else 0

    # Center columns horizontally in the bubble.
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

    for idx, ch_info in enumerate(chars):
        # Column overflow — move to next column
        if char_y + char_height > y2 - TEXT_MARGIN:
            col_x -= col_width
            char_y = start_y
            if col_x < x1 + TEXT_MARGIN:
                break

        # Segment-aware column break: if this is the start of a new segment,
        # check if the whole segment fits in the remaining column space.
        # If not (and we're not already at the column top, and the segment
        # is small enough to fit in a full column), start a new column.
        if ch_info["seg_start"] and char_y > start_y:
            seg_len = ch_info["seg_len"]
            remaining_slots = max(1, (y2 - TEXT_MARGIN - char_y) // char_height)
            if seg_len > remaining_slots and seg_len <= chars_per_col:
                col_x -= col_width
                char_y = start_y
                if col_x < x1 + TEXT_MARGIN:
                    break

        ch = ch_info["char"]

        # Characters like ー ～ — must be rotated 90° CW for vertical text
        if ch in _VERTICAL_ROTATE_CHARS:
            main_bbox = draw.textbbox((0, 0), ch, font=font)
            main_w = main_bbox[2] - main_bbox[0]
            main_h = main_bbox[3] - main_bbox[1]
            # Render to a small RGBA temp image, then rotate
            pad = 4
            tmp = Image.new("RGBA", (main_w + pad * 2, main_h + pad * 2), (0, 0, 0, 0))
            tmp_draw = ImageDraw.Draw(tmp)
            tmp_draw.rectangle((0, 0, tmp.width, tmp.height), fill=bg_fill)
            tmp_draw.text((pad - main_bbox[0], pad - main_bbox[1]), ch,
                          fill=text_fill, font=font)
            rotated = tmp.rotate(-90, expand=True)
            # Center the rotated glyph in the char cell
            rx = col_x + (font_size - rotated.width) // 2
            ry = char_y + (char_height - rotated.height) // 2
            if mask is not None:
                overlay.paste(rotated, (rx, ry), rotated)
            else:
                img.paste(rotated, (rx, ry), rotated)
        else:
            main_bbox = draw.textbbox((0, 0), ch, font=font)
            main_w = main_bbox[2] - main_bbox[0]
            main_h = main_bbox[3] - main_bbox[1]
            # Offset background by bbox origin (font ascender shifts ink down/right)
            mbg_x = col_x + main_bbox[0]
            mbg_y = char_y + main_bbox[1]
            draw.rectangle(
                (mbg_x - _TEXT_BG_PAD, mbg_y - _TEXT_BG_PAD,
                 mbg_x + main_w + _TEXT_BG_PAD, mbg_y + main_h + _TEXT_BG_PAD),
                fill=bg_fill,
            )
            draw.text((col_x, char_y), ch, fill=text_fill, font=font)

        if ch_info["furigana"]:
            furi_x = col_x + font_size + 1
            furi_char_h = furi_size + 1
            furi_total_h = len(ch_info["furigana"]) * furi_char_h
            local_furi_font = furi_font
            local_furi_size = furi_size
            # Compress furigana spacing and font when stack overflows char_height
            if furi_total_h > char_height and len(ch_info["furigana"]) > 1:
                furi_char_h = char_height // len(ch_info["furigana"])
                furi_total_h = len(ch_info["furigana"]) * furi_char_h
                # Shrink font to match reduced spacing so characters don't overlap
                if furi_char_h < furi_size:
                    local_furi_size = max(MIN_FONT_SIZE // 2, furi_char_h - 1)
                    local_furi_font = _load_font(FONT_JP, local_furi_size)
            furi_y = char_y + (char_height - furi_total_h) // 2
            for fc in ch_info["furigana"]:
                if furi_x + local_furi_size <= x2 - BUBBLE_PADDING:
                    fc_bbox = draw.textbbox((0, 0), fc, font=local_furi_font)
                    fc_w = fc_bbox[2] - fc_bbox[0]
                    fc_h = fc_bbox[3] - fc_bbox[1]
                    # Offset background by bbox origin
                    fbg_x = furi_x + fc_bbox[0]
                    fbg_y = furi_y + fc_bbox[1]
                    draw.rectangle(
                        (fbg_x - 1, fbg_y - 1,
                         fbg_x + fc_w + 1, fbg_y + fc_h + 1),
                        fill=furi_bg_fill,
                    )
                    draw.text((furi_x, furi_y), fc, fill=text_fill, font=local_furi_font)
                furi_y += furi_char_h

        char_y += char_height

    if mask is not None:
        overlay_arr = np.array(overlay)
        overlay_arr[:, :, 3][mask == 0] = 0
        overlay = Image.fromarray(overlay_arr)
        img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))


def _fit_vertical_font_size(chars: list[dict], bw: int, bh: int) -> int:
    """Binary search for the largest font size that fits all characters."""
    has_furigana = any(c["furigana"] for c in chars)
    n = len(chars)

    lo, hi = MIN_FONT_SIZE, min(MAX_FONT_SIZE, bh // 2)
    best = lo

    for _ in range(15):
        mid = (lo + hi) // 2
        furi_extra = int(mid * FURIGANA_SIZE_RATIO) + 2 if has_furigana else 0
        col_width = mid + furi_extra
        char_height = int(mid * 1.05)

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
                               text: str, base_font_size: int | None = None,
                               inpainted: bool = False) -> None:
    """Render English text on artwork with background for readability.

    When inpainted=True (AI cleaned the background), uses a semi-transparent
    overlay to preserve the restored artwork.  When inpainted=False (default),
    fills the bbox with solid white first to fully cover the original Japanese
    text, then renders English on top.
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
    font = _load_font(FONT_EN_BOLD, font_size)
    draw = ImageDraw.Draw(img)

    lines = _word_wrap(text, font, bw, draw)
    if not lines:
        return

    line_height = int(font_size * 1.2)
    total_height = len(lines) * line_height

    if inpainted:
        # Semi-transparent overlay preserves AI-restored background
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        bg_pad = 6
        bg_y1 = y1 + TEXT_MARGIN + (bh - total_height) // 2 - bg_pad
        bg_y2 = bg_y1 + total_height + 2 * bg_pad
        overlay_draw.rectangle(
            (x1 + TEXT_MARGIN - bg_pad, bg_y1,
             x2 - TEXT_MARGIN + bg_pad, bg_y2),
            fill=(255, 255, 255, 180),
        )
        img_rgba = img.convert("RGBA")
        img_rgba = Image.alpha_composite(img_rgba, overlay)
        img.paste(img_rgba.convert("RGB"))
    else:
        # Solid white fill to fully cover original Japanese text
        draw.rectangle(bbox, fill="white")

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
