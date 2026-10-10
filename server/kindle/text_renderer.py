"""Text rendering for manga bubbles: vertical Japanese w/ furigana and English."""

import logging
import re
from functools import lru_cache

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
_HORIZONTAL_MASK_VERTICAL_INSET_PCT = 15
_HORIZONTAL_MASK_WIDTH_PCT = 30
_VERTICAL_MASK_VERTICAL_INSET_PCT = 5
_VERTICAL_MASK_WIDTH_PCT = 70
_MAIN_VERTICAL_METRIC_CHARS = "漢今藤原拓海接触鬼神全力"
_FURIGANA_METRIC_CHARS = "きょうせつふじわらたくみ"
_BRIGHT_REGION_THRESHOLD = 205
_BRIGHT_REGION_MIN_PAD = 24
_BRIGHT_REGION_MAX_WIDTH_RATIO = 3.0
_BRIGHT_REGION_MAX_HEIGHT_RATIO = 2.0
_BRIGHT_REGION_MAX_WIDTH_EXTRA = 120
_BRIGHT_REGION_MAX_HEIGHT_EXTRA = 120
_BRIGHT_REGION_MIN_AREA_GAIN = 1.15
_BRIGHT_REGION_MAX_BORDER_HOLE_RATIO = 0.015
_BRIGHT_REGION_MAX_BORDER_HOLE_PIXELS = 24
_NO_MASK_FALLBACK_MAX_PAD_X = 16
_NO_MASK_FALLBACK_MAX_PAD_Y = 24
_NO_MASK_FALLBACK_MIN_PAD_X = 6
_NO_MASK_FALLBACK_MIN_PAD_Y = 8
# No-mask tightening: shrink the bbox to the dominant bright component when
# RT-DETR's detection extends past the visible plate (chapter title plates,
# narration banners) so vertical text doesn't get anchored onto dark artwork.
# Trigger only when the bright region fills less than this fraction of the bbox.
_NO_MASK_TIGHTEN_MAX_FILL = 0.80
# Absolute minimum area for the dominant bright component before we trust it as
# a real plate. Smaller than this we treat as detector noise (specular reflections,
# stray bright specks) and skip tightening. A real title/narration plate is at
# least ~30x15 pixels.
_NO_MASK_TIGHTEN_MIN_AREA_PX = 400
# Aspect ratio above which we treat a detection as horizontal source text
# (chapter title strips, narration banners) and render the new furigana glyphs
# left-to-right instead of in vertical columns.
_WIDE_BBOX_HORIZONTAL_RATIO = 2.5
# Minimum padding when expanding bbox to the mask's bounding rect — keeps
# small detections from being limited to no expansion at all.
_MASK_UNION_MIN_PAD = 6
_FURIGANA_EXPANDED_MAX_FONT_RATIO = 1.15
_FURIGANA_EXPANDED_MAX_FONT_EXTRA = 4
_FURIGANA_SOURCE_MAX_FONT_RATIO = 1.15
_FURIGANA_SOURCE_MAX_FONT_EXTRA = 4
_FURIGANA_PAGE_NORMAL_CAP_RATIO = 1.20
_FURIGANA_PAGE_NORMAL_CAP_EXTRA = 5
_FURIGANA_PAGE_NORMAL_FLOOR_RATIO = 0.88
_FURIGANA_PAGE_NORMAL_FLOOR_EXTRA = 4
_FURIGANA_PAGE_OUTLIER_RATIO = 1.55
_FURIGANA_PAGE_OUTLIER_EXTRA = 10
_FURIGANA_PAGE_FLOOR_MIN_CHARS = 6
_FURIGANA_READABLE_MIN_SIZE = 12
_FURIGANA_READABLE_MAIN_THRESHOLD = 18
_SOURCE_FONT_MAX_DARK_DENSITY = 0.35
_SOURCE_FONT_MIN_DARK_PIXELS = 12

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
    In horizontal mode (default): uses a larger vertical inset and conservative
    width — this prevents full lines near the top/bottom from overflowing oval
    bubbles.  In vertical mode: uses a smaller vertical inset and a wider
    percentile span because Japanese text stacks characters individually and the
    rendered overlay is still clipped by the mask. This helps soft/stylized
    bubbles use more of their available interior without drawing outside them.

    The detector bbox can be tight around the inner text strokes while the
    bubble (mask) extends further out — common for stylized bubbles with
    spikes or curved edges. Union the bbox with the mask's bounding rect so
    the font fitter sees the full available interior; the alpha-clip at the
    end of rendering still confines glyphs to the actual mask.
    """
    x1, y1, x2, y2 = bbox
    h_full, w_full = mask.shape[:2]
    ys_all, xs_all = np.where(mask > 0)
    if len(ys_all) >= 4:
        mx1 = max(0, int(xs_all.min()))
        my1 = max(0, int(ys_all.min()))
        mx2 = min(w_full, int(xs_all.max()) + 1)
        my2 = min(h_full, int(ys_all.max()) + 1)
        # Cap how far we expand past the detector bbox so a misextracted mask
        # can't drag layout into adjacent panels.
        bw = max(1, x2 - x1)
        bh = max(1, y2 - y1)
        max_pad_x = max(_MASK_UNION_MIN_PAD, bw // 3)
        max_pad_y = max(_MASK_UNION_MIN_PAD, bh // 3)
        x1 = max(min(x1, mx1), x1 - max_pad_x)
        y1 = max(min(y1, my1), y1 - max_pad_y)
        x2 = min(max(x2, mx2), x2 + max_pad_x)
        y2 = min(max(y2, my2), y2 + max_pad_y)
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

    # Vertical Japanese can use more of the mask than horizontal English because
    # each glyph is placed independently and the final overlay is mask-clipped.
    v_pct = (_VERTICAL_MASK_VERTICAL_INSET_PCT if vertical
             else _HORIZONTAL_MASK_VERTICAL_INSET_PCT)
    v_inset = max(2, mask_h * v_pct // 100)
    safe_top = mask_top + v_inset
    safe_bot = mask_bot - v_inset
    if safe_top >= safe_bot:
        return bbox

    # Horizontal: use conservative width to avoid clipping whole lines near oval
    # edges. Vertical: use a wider span so jagged/stylized bubbles do not get
    # reduced to their median-width core.
    lefts = []
    rights = []
    for r in range(safe_top, safe_bot + 1):
        cols = np.where(crop[r] > 0)[0]
        if len(cols) > 0:
            lefts.append(int(cols[0]))
            rights.append(int(cols[-1]))

    if not lefts:
        return bbox

    width_pct = (_VERTICAL_MASK_WIDTH_PCT if vertical
                 else _HORIZONTAL_MASK_WIDTH_PCT)
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

def _restore_outside_bbox(
    image: Image.Image, original: Image.Image,
    bbox: tuple[int, int, int, int],
) -> None:
    """Keep a renderer transaction strictly inside its declared bounds."""
    x1, y1, x2, y2 = bbox
    image.paste(original.crop((0, 0, image.width, y1)), (0, 0))
    image.paste(original.crop((0, y2, image.width, image.height)), (0, y2))
    image.paste(original.crop((0, y1, x1, y2)), (0, y1))
    image.paste(original.crop((x2, y1, image.width, y2)), (x2, y1))

def render_english(img: Image.Image, bbox: tuple[int, int, int, int],
                   text: str, base_font_size: int | None = None,
                   mask: 'np.ndarray | None' = None) -> dict[str, object]:
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
        return {"font_size": MIN_FONT_SIZE, "line_count": 1,
                "fit_status": "overflow", "ellipsized": False}

    layout = _choose_layout(text)
    original = img.copy()
    log.debug("  Layout: %s for '%s' in %dx%d", layout, text, bw, bh)

    if layout == "vertical_sfx":
        chars = list(text.strip())
        font_size = _fit_vertical_chars(chars, bw, bh)
        _render_vertical_sfx(img, bbox, text, mask=mask)
        _restore_outside_bbox(img, original, bbox)
        fits = len(chars) * int(font_size * 1.1) <= bh
        return {"font_size": font_size, "line_count": len(chars) or 1,
                "fit_status": "fit" if fits else "overflow",
                "ellipsized": False}
    else:
        safe_bbox = _mask_safe_bbox(bbox, mask) if mask is not None else bbox
        safe_bw = safe_bbox[2] - safe_bbox[0] - 2 * TEXT_MARGIN
        safe_bh = safe_bbox[3] - safe_bbox[1] - 2 * TEXT_MARGIN
        font_size = _fit_horizontal_english_size(
            text, safe_bw, safe_bh, base_font_size,
        )
        font = _load_font(FONT_EN_BOLD, font_size)
        draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        lines = _word_wrap(text, font, safe_bw, draw)
        fits = bool(lines) and len(lines) * int(font_size * 1.2) <= safe_bh
        _render_horizontal_english(img, bbox, text,
                                   target_size=base_font_size, mask=mask)
        _restore_outside_bbox(img, original, bbox)
        return {"font_size": font_size, "line_count": len(lines) or 1,
                "fit_status": "fit" if fits else "overflow",
                "ellipsized": False}


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
                             mask: 'np.ndarray | None' = None,
                             source_font_size: int | None = None,
                             page_font_cap: int | None = None,
                             source_outlier_threshold: int | None = None,
                             page_font_floor: int | None = None,
                             layout_img: Image.Image | None = None,
                             ) -> dict[str, object]:
    """Render furigana transactionally inside the declared document box."""
    original = img.copy()
    outcome = _render_furigana_vertical_impl(
        img, bbox, segments, mask=mask,
        source_font_size=source_font_size, page_font_cap=page_font_cap,
        source_outlier_threshold=source_outlier_threshold,
        page_font_floor=page_font_floor, layout_img=layout_img,
    )
    _restore_outside_bbox(img, original, bbox)
    return outcome


def _render_furigana_vertical_impl(
    img: Image.Image, bbox: tuple[int, int, int, int], segments: list[dict],
    mask: 'np.ndarray | None' = None,
    source_font_size: int | None = None,
    page_font_cap: int | None = None,
    source_outlier_threshold: int | None = None,
    page_font_floor: int | None = None,
    layout_img: Image.Image | None = None,
) -> dict[str, object]:
    """Render vertical Japanese text with furigana inside a bubble region.

    When `mask` is provided, the bbox is first tightened to the mask's
    interior (same as English rendering) so text is sized to the actual
    bubble shape, then renders to an RGBA overlay and clips to mask.
    When `layout_img` is provided, no-mask bright-region layout uses that
    original source image while drawing still happens on `img`.
    """
    if mask is not None:
        bbox = _mask_safe_bbox(bbox, mask, vertical=True)

    x1, y1, x2, y2 = bbox
    bw = x2 - x1 - 2 * TEXT_MARGIN
    bh = y2 - y1 - 2 * TEXT_MARGIN

    if bw < 10 or bh < 10:
        return {"font_size": MIN_FONT_SIZE, "line_count": 1,
                "fit_status": "overflow", "ellipsized": False}

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
        return {"font_size": MIN_FONT_SIZE, "line_count": 1,
                "fit_status": "overflow", "ellipsized": False}

    # Wide-short bboxes belong to horizontal source text (chapter title strips,
    # narration banners). Stacking chars into vertical columns scatters new
    # white-backed glyphs onto adjacent artwork and looks worse than just
    # laying them out left-to-right where the original text was.
    if bw >= bh * _WIDE_BBOX_HORIZONTAL_RATIO:
        return _render_furigana_horizontal(
            img, bbox, chars,
            mask=mask,
            source_font_size=source_font_size,
            page_font_cap=page_font_cap,
            source_outlier_threshold=source_outlier_threshold,
            page_font_floor=page_font_floor,
            layout_img=layout_img,
        )

    original_font_size = None
    if mask is None:
        initial_font_size = _fit_vertical_font_size(chars, bw, bh)
        original_font_size = initial_font_size
        bbox = _no_mask_furigana_layout_bbox(
            layout_img if layout_img is not None else img,
            bbox,
            min_extra_row_height=_vertical_main_char_height(initial_font_size),
        )
        # Trim to the bright plate when the detection bleeds onto dark artwork.
        bbox = _tighten_no_mask_bbox_to_bright_region(
            layout_img if layout_img is not None else img,
            bbox,
        )
        x1, y1, x2, y2 = bbox
        bw = x2 - x1 - 2 * TEXT_MARGIN
        bh = y2 - y1 - 2 * TEXT_MARGIN
        if bw < 10 or bh < 10:
            return {"font_size": initial_font_size, "line_count": 1,
                    "fit_status": "overflow", "ellipsized": False}

    geometric_font_size = _fit_vertical_font_size(chars, bw, bh)
    font_size = geometric_font_size
    if original_font_size is not None:
        font_size = _cap_expanded_furigana_font_size(
            font_size,
            original_font_size,
        )
    if source_font_size is not None:
        font_size = _cap_furigana_to_source_scale(font_size, source_font_size)
    font_size = _cap_furigana_to_page_dialogue_scale(
        font_size,
        source_font_size=source_font_size,
        page_font_cap=page_font_cap,
        source_outlier_threshold=source_outlier_threshold,
    )
    font_size = _floor_furigana_to_page_dialogue_scale(
        font_size,
        geometric_font_size=geometric_font_size,
        source_font_size=source_font_size,
        page_font_floor=page_font_floor,
        source_outlier_threshold=source_outlier_threshold,
        char_count=len(chars),
    )
    furi_size = _furigana_font_size(font_size)

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
    char_height = _vertical_main_char_height(font_size)

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
    rendered_count = 0
    columns_used = 1

    for idx, ch_info in enumerate(chars):
        # Column overflow — move to next column
        if char_y + char_height > y2 - TEXT_MARGIN:
            col_x -= col_width
            char_y = start_y
            if col_x < x1 + TEXT_MARGIN:
                break
            columns_used += 1

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
                columns_used += 1

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
            furi_char_h = _vertical_furigana_char_height(furi_size)
            furi_total_h = len(ch_info["furigana"]) * furi_char_h
            local_furi_font = furi_font
            local_furi_size = furi_size
            # Compress furigana spacing and font when stack overflows char_height
            if furi_total_h > char_height and len(ch_info["furigana"]) > 1:
                local_furi_size, furi_char_h = _fit_furigana_stack(
                    furi_size, char_height, len(ch_info["furigana"])
                )
                furi_total_h = len(ch_info["furigana"]) * furi_char_h
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
        rendered_count += 1

    if mask is not None:
        overlay_arr = np.array(overlay)
        overlay_arr[:, :, 3][mask == 0] = 0
        overlay = Image.fromarray(overlay_arr)
        img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))
    return {
        "font_size": font_size,
        "line_count": max(1, columns_used),
        "fit_status": "fit" if rendered_count == len(chars) else "overflow",
        "ellipsized": False,
    }


def _render_furigana_horizontal(
    img: Image.Image,
    bbox: tuple[int, int, int, int],
    chars: list[dict],
    mask: 'np.ndarray | None' = None,
    source_font_size: int | None = None,
    page_font_cap: int | None = None,
    source_outlier_threshold: int | None = None,
    page_font_floor: int | None = None,
    layout_img: Image.Image | None = None,
) -> dict[str, object]:
    """Render furigana chars left-to-right for wide-short detections.

    Used when the source text was horizontal (chapter title strips, narration
    banners). Each char sits on the baseline; furigana, if any, stacks above
    its kanji. The block is centered in the bbox.
    """
    x1, y1, x2, y2 = bbox
    bw = x2 - x1 - 2 * TEXT_MARGIN
    bh = y2 - y1 - 2 * TEXT_MARGIN
    if bw < 10 or bh < 10:
        return {"font_size": MIN_FONT_SIZE, "line_count": 1,
                "fit_status": "overflow", "ellipsized": False}

    has_furigana = any(c["furigana"] for c in chars)
    n = len(chars)

    # Binary search the largest font size that fits all chars in one row
    # (or multiple rows if needed) within the bbox.
    lo, hi = MIN_FONT_SIZE, min(MAX_FONT_SIZE, bh)
    best = lo
    for _ in range(15):
        mid = (lo + hi) // 2
        furi_size = _furigana_font_size(mid) if has_furigana else 0
        row_height = mid + (furi_size + 2 if has_furigana else 0) + 2
        chars_per_row = max(1, bw // mid)
        rows_needed = (n + chars_per_row - 1) // chars_per_row
        if rows_needed * row_height <= bh:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    font_size = best
    if source_font_size is not None:
        font_size = _cap_furigana_to_source_scale(font_size, source_font_size)
    font_size = _cap_furigana_to_page_dialogue_scale(
        font_size,
        source_font_size=source_font_size,
        page_font_cap=page_font_cap,
        source_outlier_threshold=source_outlier_threshold,
    )
    font_size = max(MIN_FONT_SIZE, font_size)

    furi_size = _furigana_font_size(font_size) if has_furigana else 0
    row_height = font_size + (furi_size + 2 if has_furigana else 0) + 2
    chars_per_row = max(1, bw // font_size)
    rows_needed = (n + chars_per_row - 1) // chars_per_row

    font = _load_font(FONT_JP, font_size)
    furi_font = _load_font(FONT_JP, furi_size) if has_furigana else None

    if mask is not None:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        bg_fill = (255, 255, 255, 255)
        text_fill = (0, 0, 0, 255)
    else:
        draw = ImageDraw.Draw(img)
        bg_fill = "white"
        text_fill = "black"

    block_h = rows_needed * row_height
    start_y = y1 + TEXT_MARGIN + max(0, (bh - block_h) // 2)

    for row in range(rows_needed):
        row_chars = chars[row * chars_per_row:(row + 1) * chars_per_row]
        if not row_chars:
            continue
        row_w = len(row_chars) * font_size
        start_x = x1 + TEXT_MARGIN + max(0, (bw - row_w) // 2)
        cy = start_y + row * row_height
        if has_furigana:
            cy += furi_size + 2

        for col, ch_info in enumerate(row_chars):
            ch = ch_info["char"]
            cx = start_x + col * font_size
            ch_bbox = draw.textbbox((0, 0), ch, font=font)
            ch_w = ch_bbox[2] - ch_bbox[0]
            ch_h = ch_bbox[3] - ch_bbox[1]
            bg_x = cx + (font_size - ch_w) // 2 + ch_bbox[0]
            bg_y = cy + ch_bbox[1]
            draw.rectangle(
                (bg_x - _TEXT_BG_PAD, bg_y - _TEXT_BG_PAD,
                 bg_x + ch_w + _TEXT_BG_PAD, bg_y + ch_h + _TEXT_BG_PAD),
                fill=bg_fill,
            )
            draw.text((cx + (font_size - ch_w) // 2, cy), ch,
                      fill=text_fill, font=font)

            if has_furigana and ch_info.get("furigana") and furi_font is not None:
                furi_text = ch_info["furigana"]
                fc_bbox = draw.textbbox((0, 0), furi_text, font=furi_font)
                fc_w = fc_bbox[2] - fc_bbox[0]
                fc_h = fc_bbox[3] - fc_bbox[1]
                fcx = cx + (font_size - fc_w) // 2
                fcy = cy - furi_size - 2
                fbg_x = fcx + fc_bbox[0]
                fbg_y = fcy + fc_bbox[1]
                draw.rectangle(
                    (fbg_x - 1, fbg_y - 1,
                     fbg_x + fc_w + 1, fbg_y + fc_h + 1),
                    fill=bg_fill,
                )
                draw.text((fcx, fcy), furi_text, fill=text_fill, font=furi_font)

    if mask is not None:
        overlay_arr = np.array(overlay)
        overlay_arr[:, :, 3][mask == 0] = 0
        overlay = Image.fromarray(overlay_arr)
        img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))
    return {
        "font_size": font_size,
        "line_count": max(1, rows_needed),
        "fit_status": ("fit" if rows_needed * row_height <= bh
                       else "overflow"),
        "ellipsized": False,
    }


def _fit_vertical_font_size(chars: list[dict], bw: int, bh: int) -> int:
    """Binary search for the largest font size that fits all characters."""
    has_furigana = any(c["furigana"] for c in chars)
    n = len(chars)

    lo, hi = MIN_FONT_SIZE, min(MAX_FONT_SIZE, bh // 2)
    best = lo

    for _ in range(15):
        mid = (lo + hi) // 2
        furi_extra = _furigana_font_size(mid) + 2 if has_furigana else 0
        col_width = mid + furi_extra
        char_height = _vertical_main_char_height(mid)

        chars_per_col = max(1, bh // char_height)
        cols_needed = (n + chars_per_col - 1) // chars_per_col
        total_width = cols_needed * col_width

        if total_width <= bw:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    return best


def _furigana_font_size(font_size: int) -> int:
    """Return furigana font size for a fitted main Japanese font size."""
    ratio_size = int(font_size * FURIGANA_SIZE_RATIO)
    if font_size >= _FURIGANA_READABLE_MAIN_THRESHOLD:
        return max(_FURIGANA_READABLE_MIN_SIZE, ratio_size)
    return max(MIN_FONT_SIZE, ratio_size)


def _cap_expanded_furigana_font_size(fitted_size: int,
                                     original_size: int) -> int:
    """Keep no-mask bright-region expansion near the original text scale.

    The detector bbox is our best rough measure of the source glyph size.  It is
    OK for caption/glow whitespace to make furigana a bit larger, but open white
    art or page margins must not turn normal narration into huge headline text.
    """
    if fitted_size <= original_size:
        return fitted_size
    ratio_cap = int(round(original_size * _FURIGANA_EXPANDED_MAX_FONT_RATIO))
    extra_cap = original_size + _FURIGANA_EXPANDED_MAX_FONT_EXTRA
    return max(MIN_FONT_SIZE, min(fitted_size, ratio_cap, extra_cap))


def _cap_furigana_to_source_scale(fitted_size: int, source_size: int) -> int:
    """Cap rendered furigana near the original glyph scale for this bubble.

    This is deliberately per-bubble.  Large original scream/SFX text can still
    render large because its source estimate is large, while short normal text
    in a spacious balloon cannot balloon into an unrelated headline size.
    """
    if source_size <= 0 or fitted_size <= source_size:
        return fitted_size
    ratio_cap = int(round(source_size * _FURIGANA_SOURCE_MAX_FONT_RATIO))
    extra_cap = source_size + _FURIGANA_SOURCE_MAX_FONT_EXTRA
    return max(MIN_FONT_SIZE, min(fitted_size, ratio_cap, extra_cap))


def compute_furigana_page_font_limits(
    source_sizes: list[int],
) -> tuple[int | None, int | None, int | None]:
    """Compute soft page limits for normal dialogue without flattening SFX.

    Most manga dialogue on one page uses a narrow source glyph-size band.  Use
    the robust median to prevent normal bubbles from drifting wildly.  Keep
    source-size outliers (large shouts/SFX/captions) eligible for larger text,
    while giving long normal-dialogue bubbles a readability floor.
    """
    clean = sorted(size for size in source_sizes if size and size > 0)
    if len(clean) < 3:
        return None, None, None
    median = int(np.median(clean))
    if median <= 0:
        return None, None, None
    normal_cap = min(
        int(round(median * _FURIGANA_PAGE_NORMAL_CAP_RATIO)),
        median + _FURIGANA_PAGE_NORMAL_CAP_EXTRA,
    )
    normal_floor = max(
        int(round(median * _FURIGANA_PAGE_NORMAL_FLOOR_RATIO)),
        median - _FURIGANA_PAGE_NORMAL_FLOOR_EXTRA,
    )
    outlier_threshold = max(
        int(round(median * _FURIGANA_PAGE_OUTLIER_RATIO)),
        median + _FURIGANA_PAGE_OUTLIER_EXTRA,
    )
    return (
        max(MIN_FONT_SIZE, normal_cap),
        max(MIN_FONT_SIZE, outlier_threshold),
        max(MIN_FONT_SIZE, normal_floor),
    )


def _cap_furigana_to_page_dialogue_scale(
    fitted_size: int,
    *,
    source_font_size: int | None,
    page_font_cap: int | None,
    source_outlier_threshold: int | None,
) -> int:
    if page_font_cap is None:
        return fitted_size
    if source_font_size is not None and source_outlier_threshold is not None:
        if source_font_size >= source_outlier_threshold:
            return fitted_size
    return max(MIN_FONT_SIZE, min(fitted_size, page_font_cap))


def _floor_furigana_to_page_dialogue_scale(
    font_size: int,
    *,
    geometric_font_size: int,
    source_font_size: int | None,
    page_font_floor: int | None,
    source_outlier_threshold: int | None,
    char_count: int,
) -> int:
    """Raise long normal dialogue toward the page scale when it still fits."""
    if page_font_floor is None:
        return font_size
    if char_count < _FURIGANA_PAGE_FLOOR_MIN_CHARS:
        return font_size
    if source_font_size is not None and source_outlier_threshold is not None:
        if source_font_size >= source_outlier_threshold:
            return font_size
    floor_target = min(geometric_font_size, page_font_floor)
    return max(MIN_FONT_SIZE, max(font_size, floor_target))


def estimate_source_vertical_font_size(
    img: Image.Image,
    bbox: tuple[int, int, int, int],
    ocr_text: str = "",
    mask: 'np.ndarray | None' = None,
) -> int | None:
    """Estimate the original vertical Japanese glyph size in one bubble.

    Returns None when the crop is too dense/noisy to trust.  The renderer then
    falls back to geometric fitting rather than over-shrinking from bad data.
    """
    try:
        import cv2
    except Exception:
        return None

    img_w, img_h = img.size
    if mask is not None:
        bbox = _mask_safe_bbox(bbox, mask, vertical=True)
    x1, y1, x2, y2 = _clamp_bbox(bbox, img_w, img_h)
    if x2 - x1 < 10 or y2 - y1 < 10:
        return None

    crop = np.array(img.crop((x1, y1, x2, y2)).convert("L"))
    valid = np.ones(crop.shape, dtype=bool)
    if mask is not None:
        valid = mask[y1:y2, x1:x2] > 0

    inset_x = max(2, (x2 - x1) // 25)
    inset_y = max(2, (y2 - y1) // 25)
    valid[:inset_y, :] = False
    valid[-inset_y:, :] = False
    valid[:, :inset_x] = False
    valid[:, -inset_x:] = False
    if not np.any(valid):
        return None

    dark = (crop < 150) & valid
    dark_pixels = int(np.count_nonzero(dark))
    valid_pixels = int(np.count_nonzero(valid))
    if dark_pixels < _SOURCE_FONT_MIN_DARK_PIXELS or valid_pixels <= 0:
        return None
    if dark_pixels / valid_pixels > _SOURCE_FONT_MAX_DARK_DENSITY:
        return None

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    dark_u8 = cv2.morphologyEx(dark.astype(np.uint8), cv2.MORPH_CLOSE, kernel,
                               iterations=1)
    labels_n, labels, stats, _ = cv2.connectedComponentsWithStats(dark_u8, 8)
    if labels_n <= 1:
        return None

    crop_h, crop_w = crop.shape
    sizes: list[int] = []
    for label in range(1, labels_n):
        area = int(stats[label, cv2.CC_STAT_AREA])
        cx = int(stats[label, cv2.CC_STAT_LEFT])
        cy = int(stats[label, cv2.CC_STAT_TOP])
        cw = int(stats[label, cv2.CC_STAT_WIDTH])
        ch = int(stats[label, cv2.CC_STAT_HEIGHT])
        if area < max(4, valid_pixels // 6000):
            continue
        if cw < 2 or ch < 2:
            continue
        # Drop panel/bubble border fragments and long rule lines.
        if cw > crop_w * 0.70 and ch < crop_h * 0.08:
            continue
        if ch > crop_h * 0.70 and cw < crop_w * 0.08:
            continue
        touches_edge = cx <= inset_x or cy <= inset_y or (
            cx + cw >= crop_w - inset_x or cy + ch >= crop_h - inset_y)
        if touches_edge and (cw > crop_w * 0.35 or ch > crop_h * 0.35):
            continue
        sizes.append(max(cw, ch))

    if not sizes:
        return None

    size = int(round(float(np.percentile(sizes, 75)) * 1.10))

    # If OCR text length gives a plausible vertical run estimate, blend it in
    # conservatively.  Short text is exactly where unrestricted run estimates
    # overstate source size and allow huge rendered text.
    char_count = _source_text_char_count(ocr_text)
    if char_count >= 2:
        ys, xs = np.where(dark_u8 > 0)
        if len(xs) > 0:
            ink_h = int(ys.max() - ys.min() + 1)
            cols = _estimate_vertical_text_columns(dark_u8)
            chars_per_col = max(1, int(np.ceil(char_count / max(1, cols))))
            run_est = int(round(ink_h / chars_per_col * 1.15))
            if MIN_FONT_SIZE <= run_est <= MAX_FONT_SIZE:
                if char_count >= 4:
                    capped_run = min(run_est, int(size * 1.25), size + 6)
                else:
                    capped_run = min(run_est, int(size * 1.10), size + 3)
                size = max(size, capped_run)

    return max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, size))


def _source_text_char_count(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def _estimate_vertical_text_columns(dark: np.ndarray) -> int:
    """Estimate main vertical text columns from x-projection clusters."""
    projection = dark.sum(axis=0)
    active = projection >= max(2, int(dark.shape[0] * 0.03))
    clusters = []
    start = None
    for idx, value in enumerate(active):
        if value and start is None:
            start = idx
        elif not value and start is not None:
            if idx - start >= 2:
                clusters.append((start, idx))
            start = None
    if start is not None and len(active) - start >= 2:
        clusters.append((start, len(active)))
    return max(1, min(6, len(clusters)))


@lru_cache(maxsize=128)
def _vertical_main_char_height(font_size: int) -> int:
    """Cell height for vertical Japanese glyphs, including background padding."""
    measured = _font_background_cell_height(
        font_size, _MAIN_VERTICAL_METRIC_CHARS, _TEXT_BG_PAD, gap_rows=1,
    )
    return max(int(font_size * 1.05), measured)


@lru_cache(maxsize=128)
def _vertical_furigana_char_height(font_size: int) -> int:
    """Cell height for stacked furigana glyphs, including background padding."""
    measured = _font_background_cell_height(
        font_size, _FURIGANA_METRIC_CHARS, 1, gap_rows=0,
    )
    return max(font_size + 1, measured)


def _fit_furigana_stack(target_size: int, total_height: int,
                        count: int) -> tuple[int, int]:
    """Fit stacked furigana inside one main character cell without overlap."""
    min_size = max(1, MIN_FONT_SIZE // 2)
    for size in range(target_size, min_size - 1, -1):
        char_height = _vertical_furigana_char_height(size)
        if char_height * count <= total_height:
            return size, char_height
    return min_size, max(1, total_height // count)


def _no_mask_furigana_layout_bbox(
    img: Image.Image,
    bbox: tuple[int, int, int, int],
    min_extra_row_height: int | None = None,
) -> tuple[int, int, int, int]:
    """Choose layout space for furigana when no bubble mask is available.

    Bounded bright regions (caption boxes, glows, missed balloon interiors) are
    safe to use.  Open white gutters or panel backgrounds are not: expanding to
    those connected components can let vertical text cross panel borders.  When
    the bright region is unbounded, stay close to the detector's original text
    bbox with only a small readability pad.
    """
    expanded = _expand_bright_region_bbox(
        img,
        bbox,
        min_extra_row_height=min_extra_row_height,
        require_bounded_region=True,
    )
    if expanded != bbox:
        return expanded
    return _expand_limited_no_mask_bbox(
        img,
        bbox,
        min_extra_row_height=min_extra_row_height,
    )


def _expand_limited_no_mask_bbox(
    img: Image.Image,
    bbox: tuple[int, int, int, int],
    min_extra_row_height: int | None = None,
) -> tuple[int, int, int, int]:
    """Small no-mask fallback expansion around the original text area."""
    img_w, img_h = img.size
    x1, y1, x2, y2 = _clamp_bbox(bbox, img_w, img_h)
    if x2 <= x1 or y2 <= y1:
        return bbox

    row_h = max(1, int(min_extra_row_height or 0))
    pad_x = min(
        _NO_MASK_FALLBACK_MAX_PAD_X,
        max(_NO_MASK_FALLBACK_MIN_PAD_X, (x2 - x1) // 4),
    )
    pad_y = min(
        _NO_MASK_FALLBACK_MAX_PAD_Y,
        max(_NO_MASK_FALLBACK_MIN_PAD_Y, row_h // 2),
    )
    return (
        max(0, x1 - pad_x),
        max(0, y1 - pad_y),
        min(img_w, x2 + pad_x),
        min(img_h, y2 + pad_y),
    )


def _tighten_no_mask_bbox_to_bright_region(
    img: Image.Image,
    bbox: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """Shrink a no-mask bbox to the dominant bright component inside it.

    RT-DETR sometimes returns a bbox that extends past the visible plate
    (e.g. a chapter title strip whose detection bleeds onto the surrounding
    dark artwork).  Without a bubble mask, the vertical furigana renderer
    will pin its rightmost column to the bbox's right edge — which lands on
    artwork rather than on the white plate.  This trims back to the largest
    bright connected region so the renderer stays on the plate.

    Returns the original bbox when the bright region already fills the bbox
    (well-fitted balloon interiors) or when no usable bright region exists.
    """
    import cv2

    img_w, img_h = img.size
    x1, y1, x2, y2 = _clamp_bbox(bbox, img_w, img_h)
    bw_b, bh_b = x2 - x1, y2 - y1
    if bw_b < 8 or bh_b < 8:
        return bbox

    gray = np.array(img.crop((x1, y1, x2, y2)).convert("L"))
    bright = (gray >= _BRIGHT_REGION_THRESHOLD).astype(np.uint8)
    if not np.any(bright):
        return bbox

    # Bridge dark text strokes on the plate so the plate registers as one
    # connected bright region (3x3 was too small for typical kanji stroke widths).
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11))
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, kernel, iterations=1)

    n_labels, _, stats, _ = cv2.connectedComponentsWithStats(bright, 8)
    if n_labels <= 1:
        return bbox

    areas = stats[1:, cv2.CC_STAT_AREA]
    best_idx = int(np.argmax(areas)) + 1
    best_area = int(stats[best_idx, cv2.CC_STAT_AREA])
    bbox_area = bw_b * bh_b

    if best_area >= bbox_area * _NO_MASK_TIGHTEN_MAX_FILL:
        return bbox
    if best_area < _NO_MASK_TIGHTEN_MIN_AREA_PX:
        return bbox

    rx = int(stats[best_idx, cv2.CC_STAT_LEFT])
    ry = int(stats[best_idx, cv2.CC_STAT_TOP])
    rw = int(stats[best_idx, cv2.CC_STAT_WIDTH])
    rh = int(stats[best_idx, cv2.CC_STAT_HEIGHT])

    if rw < 10 or rh < 10:
        return bbox

    return (x1 + rx, y1 + ry, x1 + rx + rw, y1 + ry + rh)


def _expand_bright_region_bbox(img: Image.Image,
                               bbox: tuple[int, int, int, int],
                               min_extra_row_height: int | None = None,
                               require_bounded_region: bool = False,
                               ) -> tuple[int, int, int, int]:
    """Expand no-mask furigana boxes into nearby white caption space.

    RT-DETR ``text_free`` detections are often tight around the original text,
    even when the manga has a larger soft white glow/caption region around it.
    If we size to that tight text bbox, the furigana render looks small and
    oddly spaced despite visible whitespace nearby.  For no-mask furigana only,
    find the bright connected component overlapping the text bbox and size to
    that component, capped so page gutters/margins cannot explode the region.
    """
    import cv2

    img_w, img_h = img.size
    x1, y1, x2, y2 = _clamp_bbox(bbox, img_w, img_h)
    bw = x2 - x1
    bh = y2 - y1
    if bw < 8 or bh < 8:
        return bbox

    max_w = max(bw, int(bw * _BRIGHT_REGION_MAX_WIDTH_RATIO),
                bw + _BRIGHT_REGION_MAX_WIDTH_EXTRA)
    max_h = max(bh, int(bh * _BRIGHT_REGION_MAX_HEIGHT_RATIO),
                bh + _BRIGHT_REGION_MAX_HEIGHT_EXTRA)
    pad_x = max(_BRIGHT_REGION_MIN_PAD, (max_w - bw) // 2)
    pad_y = max(_BRIGHT_REGION_MIN_PAD, (max_h - bh) // 2)
    sx1 = max(0, x1 - pad_x)
    sy1 = max(0, y1 - pad_y)
    sx2 = min(img_w, x2 + pad_x)
    sy2 = min(img_h, y2 + pad_y)
    if sx2 <= sx1 or sy2 <= sy1:
        return bbox

    gray = np.array(img.crop((sx1, sy1, sx2, sy2)).convert("L"))
    bright = (gray >= _BRIGHT_REGION_THRESHOLD).astype(np.uint8)
    if not np.any(bright):
        return bbox

    # Bridge scan noise / tiny dark holes in white glows without merging across
    # large dark artwork regions.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, kernel, iterations=1)
    labels_n, labels, stats, _ = cv2.connectedComponentsWithStats(bright, 8)
    if labels_n <= 1:
        return bbox

    lx1, ly1 = x1 - sx1, y1 - sy1
    lx2, ly2 = x2 - sx1, y2 - sy1
    overlap = labels[max(0, ly1):max(0, ly2), max(0, lx1):max(0, lx2)]
    if overlap.size == 0:
        return bbox

    candidate_labels, counts = np.unique(overlap[overlap > 0], return_counts=True)
    if len(candidate_labels) == 0:
        return bbox
    label = int(candidate_labels[int(np.argmax(counts))])

    rx = int(stats[label, cv2.CC_STAT_LEFT])
    ry = int(stats[label, cv2.CC_STAT_TOP])
    rw = int(stats[label, cv2.CC_STAT_WIDTH])
    rh = int(stats[label, cv2.CC_STAT_HEIGHT])

    if require_bounded_region:
        search_w = sx2 - sx1
        search_h = sy2 - sy1
        touches_search_edge = (
            rx <= 0 or ry <= 0 or
            rx + rw >= search_w or ry + rh >= search_h
        )
        if touches_search_edge:
            return bbox

    ex1, ey1 = sx1 + rx, sy1 + ry
    ex2, ey2 = ex1 + rw, ey1 + rh

    # Never shrink below the original detection.
    ex1 = min(ex1, x1)
    ey1 = min(ey1, y1)
    ex2 = max(ex2, x2)
    ey2 = max(ey2, y2)

    ex1, ex2 = _cap_interval(ex1, ex2, x1, x2, max_w, sx1, sx2)
    ey1, ey2 = _cap_interval(ey1, ey2, y1, y2, max_h, sy1, sy2)

    component_mask = labels == label
    if not _has_clear_bright_expansion_space(
        component_mask,
        search_origin=(sx1, sy1),
        original_bbox=(x1, y1, x2, y2),
        expanded_bbox=(ex1, ey1, ex2, ey2),
        min_extra_row_height=min_extra_row_height,
    ):
        return bbox

    if (ex2 - ex1) * (ey2 - ey1) < bw * bh * _BRIGHT_REGION_MIN_AREA_GAIN:
        return bbox
    return (ex1, ey1, ex2, ey2)


def _has_clear_bright_expansion_space(
    component_mask: np.ndarray,
    *,
    search_origin: tuple[int, int],
    original_bbox: tuple[int, int, int, int],
    expanded_bbox: tuple[int, int, int, int],
    min_extra_row_height: int | None,
) -> bool:
    """Return true only when expansion border is genuinely blank space.

    Normal speech balloons can be one large connected white component with
    multiple text columns inside.  Expanding one tight no-mask detection to that
    whole component makes the new text/furigana overwhelm the balloon.  Keep the
    expansion only when the added top/bottom border is at least one extra text
    row and the added area is mostly clean white space, not other glyph holes.
    """
    import cv2

    sx1, sy1 = search_origin
    ox1, oy1, ox2, oy2 = original_bbox
    ex1, ey1, ex2, ey2 = expanded_bbox
    if ex2 <= ex1 or ey2 <= ey1:
        return False

    lx1, ly1 = ex1 - sx1, ey1 - sy1
    lx2, ly2 = ex2 - sx1, ey2 - sy1
    region = component_mask[ly1:ly2, lx1:lx2]
    if region.size == 0:
        return False

    orig = np.zeros(region.shape, dtype=bool)
    rx1 = max(0, ox1 - ex1)
    ry1 = max(0, oy1 - ey1)
    rx2 = min(region.shape[1], ox2 - ex1)
    ry2 = min(region.shape[0], oy2 - ey1)
    if rx2 <= rx1 or ry2 <= ry1:
        return False
    orig[ry1:ry2, rx1:rx2] = True

    extra_white_pixels = int(np.count_nonzero(region & ~orig))
    if min_extra_row_height is not None:
        row_height = max(1, int(min_extra_row_height))
        vertical_extra = max(0, oy1 - ey1) + max(0, ey2 - oy2)
        if vertical_extra < row_height:
            return False
        required_area = max(1, (ox2 - ox1) * row_height)
        if extra_white_pixels < required_area:
            return False

    # Fill only holes enclosed by the bright component.  Dark artwork outside a
    # rounded caption/glow is external background and should not count against
    # it; dark glyphs from neighboring text columns inside a speech balloon do.
    background = (~region).astype(np.uint8)
    padded = np.pad(background, 1, constant_values=1)
    flood = padded.copy()
    cv2.floodFill(flood, None, (0, 0), 2)
    external_background = flood[1:-1, 1:-1] == 2
    holes = (~region) & ~external_background
    border_holes = int(np.count_nonzero(holes & ~orig))
    hole_limit = max(
        _BRIGHT_REGION_MAX_BORDER_HOLE_PIXELS,
        int(extra_white_pixels * _BRIGHT_REGION_MAX_BORDER_HOLE_RATIO),
    )
    return border_holes <= hole_limit


def _clamp_bbox(bbox: tuple[int, int, int, int], img_w: int, img_h: int
                ) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    x1 = max(0, min(img_w, int(x1)))
    y1 = max(0, min(img_h, int(y1)))
    x2 = max(0, min(img_w, int(x2)))
    y2 = max(0, min(img_h, int(y2)))
    return x1, y1, x2, y2


def _cap_interval(left: int, right: int, orig_left: int, orig_right: int,
                  max_len: int, min_left: int, max_right: int) -> tuple[int, int]:
    if right - left <= max_len:
        return left, right
    center = (orig_left + orig_right) // 2
    left = center - max_len // 2
    right = left + max_len
    if left < min_left:
        left = min_left
        right = min(max_right, left + max_len)
    if right > max_right:
        right = max_right
        left = max(min_left, right - max_len)
    return left, right


def _font_background_cell_height(font_size: int, chars: str, bg_pad: int,
                                 gap_rows: int) -> int:
    """Measure cell height needed so adjacent background rects do not touch."""
    font = _load_font(FONT_JP, font_size)
    probe = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(probe)
    bboxes = [draw.textbbox((0, 0), ch, font=font) for ch in chars]
    top = min(b[1] for b in bboxes)
    bottom = max(b[3] for b in bboxes)
    # PIL rectangle endpoints are inclusive, so +1 prevents overlap. Main text
    # asks for one extra blank row; compact furigana only needs no-overlap.
    return int(bottom - top + 2 * bg_pad + 1 + gap_rows)


# --- Artwork text rendering ---

def render_english_on_artwork(img: Image.Image, bbox: tuple[int, int, int, int],
                               text: str, base_font_size: int | None = None,
                               inpainted: bool = False) -> dict[str, object]:
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
        return {"font_size": MIN_FONT_SIZE, "line_count": 1,
                "fit_status": "overflow", "ellipsized": False}

    layout = _choose_layout(text)
    original = img.copy()
    if layout == "vertical_sfx":
        chars = list(text.strip())
        font_size = _fit_vertical_chars(chars, bw, bh)
        _render_vertical_sfx(img, bbox, text)
        _restore_outside_bbox(img, original, bbox)
        fits = len(chars) * int(font_size * 1.1) <= bh
        return {"font_size": font_size, "line_count": len(chars) or 1,
                "fit_status": "fit" if fits else "overflow",
                "ellipsized": False}

    font_size = _fit_horizontal_english_size(text, bw, bh, base_font_size)
    font = _load_font(FONT_EN_BOLD, font_size)
    draw = ImageDraw.Draw(img)

    lines = _word_wrap(text, font, bw, draw)
    if not lines:
        return {"font_size": font_size, "line_count": 1,
                "fit_status": "overflow", "ellipsized": False}

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
    _restore_outside_bbox(img, original, bbox)
    return {"font_size": font_size, "line_count": len(lines),
            "fit_status": "fit" if total_height <= bh else "overflow",
            "ellipsized": False}


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
