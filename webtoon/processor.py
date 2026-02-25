"""Webtoon pipeline orchestration: OCR → cluster → validate → translate → render."""

import logging
import os
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pipeline.image_utils import clear_text_in_region, load_image, load_image_pil
from .config import FONT_KO

from .bubble_detector import WebtoonBubble, detect_bubbles
from .image_utils import split_tall_image, stitch_detections
from .ocr import TextDetection, detect_and_read, is_valid_korean
from .translator import translate

log = logging.getLogger(__name__)


@dataclass
class WebtoonTextRegion:
    """A validated and translated text region."""
    bubble: WebtoonBubble
    is_valid: bool = False
    english: str = ""


@dataclass
class WebtoonPageResult:
    """Result of processing a single webtoon image/strip."""
    image_path: str
    name: str
    img_cv: np.ndarray | None = None
    img_pil: Image.Image | None = None
    detections: list[TextDetection] = field(default_factory=list)
    bubbles: list[WebtoonBubble] = field(default_factory=list)
    regions: list[WebtoonTextRegion] = field(default_factory=list)
    output_img: Image.Image | None = None


# --- Stage functions ---


def load_page(path: str) -> WebtoonPageResult:
    """Stage 1: Load image as both OpenCV and Pillow formats."""
    name = os.path.splitext(os.path.basename(path))[0]
    return WebtoonPageResult(
        image_path=path,
        name=name,
        img_cv=load_image(path),
        img_pil=load_image_pil(path),
    )


def detect_text(page: WebtoonPageResult) -> None:
    """Stage 2: Run EasyOCR, handling tall images by splitting into strips."""
    h = page.img_cv.shape[0]
    max_strip_height = 2000

    if h <= max_strip_height:
        page.detections = detect_and_read(page.img_cv)
    else:
        strips = split_tall_image(page.img_cv, max_height=max_strip_height)
        strip_results = []
        for strip_img, y_offset in strips:
            dets = detect_and_read(strip_img)
            strip_results.append((dets, y_offset))
        page.detections = stitch_detections(strip_results)

    log.info("Detected %d text regions in %s", len(page.detections), page.name)


def cluster_and_find_bubbles(page: WebtoonPageResult) -> None:
    """Stage 3: Cluster text detections into bubbles and find boundaries."""
    page.bubbles = detect_bubbles(page.img_cv, page.detections)
    log.info("Found %d bubbles in %s", len(page.bubbles), page.name)


def _is_title_text(bubble: WebtoonBubble) -> bool:
    """Detect decorative title/logo text that should not be translated.

    Title text has large, single-character detections (artistic/decorative).
    Normal dialogue has full text lines per detection, even if tall.
    """
    if not bubble.text_regions:
        return False
    heights = [d.bbox_rect[3] - d.bbox_rect[1] for d in bubble.text_regions]
    avg_h = sum(heights) / len(heights)
    avg_chars = (sum(len(d.text.strip()) for d in bubble.text_regions)
                 / len(bubble.text_regions))

    # Title/decorative: large characters that are single-char detections
    # (narration text has many chars per detection line, so avg_chars > 2)
    return avg_h > 60 and avg_chars <= 2


def validate_and_translate(page: WebtoonPageResult) -> None:
    """Stage 4: Validate Korean text and translate to English."""
    for bubble in page.bubbles:
        region = WebtoonTextRegion(bubble=bubble)

        if _is_title_text(bubble):
            log.info("  Skipping title/logo text: %s", bubble.combined_text)
            page.regions.append(region)
            continue

        if not is_valid_korean(bubble.combined_text):
            if bubble.combined_text.strip():
                log.info("  OCR noise (not Korean): %s", bubble.combined_text)
            page.regions.append(region)
            continue

        region.is_valid = True
        log.info("  KO: %s", bubble.combined_text)

        english = translate(bubble.combined_text)
        if english.strip():
            region.english = english
            log.info("  EN: %s", english)
        else:
            log.info("  Translation empty for '%s'", bubble.combined_text)

        page.regions.append(region)


def render_page(page: WebtoonPageResult, out_dir: str,
                debug: bool = False) -> None:
    """Stage 5: Clear text regions, render color-aware English, save output."""
    if debug:
        debug_img = _draw_webtoon_debug(page)
        debug_img.save(os.path.join(out_dir, f"{page.name}-debug.png"))

    page.output_img = page.img_pil.copy()

    for region in page.regions:
        if not region.english:
            continue

        bubble = region.bubble
        bg_color = bubble.bg_color

        # Compute text-region bbox: union of all OCR detection bboxes + padding.
        # This is the actual area where Korean text lives (not the contour bbox
        # which can be much larger and cause misplaced rendering).
        text_bbox = _text_region_bbox(bubble, page.output_img.width,
                                      page.output_img.height)

        # Step 1: Clear Korean text per detection.
        # For each text detection, clear within bubble mask if available,
        # otherwise use rectangle fill.  This handles cases where some
        # detections fall inside a contour and others are floating text
        # that got clustered together.
        _clear_bubble_text(page.output_img, bubble)

        # Step 2: Render English text within the text-region bbox
        _render_webtoon_english(page.output_img, bubble, region.english,
                                text_bbox)

    output_path = os.path.join(out_dir, f"{page.name}-en.png")
    page.output_img.save(output_path)
    log.info("Saved: %s", output_path)


def _clear_bubble_text(img: Image.Image, bubble: WebtoonBubble) -> None:
    """Clear Korean text in a bubble, respecting bubble mask when available.

    Two-phase clearing:
    1. Clear the full text_region_bbox within the bubble mask (covers all text
       inside the contour, including gaps between detection rects)
    2. Clear EVERY detection with a padded rectangle using locally-sampled
       background color (catches anything the mask missed — floating text,
       partial mask coverage, glyph strokes beyond mask edge)
    """
    bg_color = bubble.bg_color
    pad = 14
    mask = bubble.bubble_mask
    img_w, img_h = img.width, img.height

    # Phase 1: clear the full text region within the mask (bubble-aware)
    if mask is not None:
        text_bbox = _text_region_bbox(bubble, img_w, img_h)
        _clear_with_mask(img, text_bbox, mask, bg_color)

    # Phase 2: clear EVERY detection with rectangle + local bg color.
    # This catches floating text outside the mask and glyph strokes that
    # extend beyond the mask edge.  Double-clearing inside-mask detections
    # is harmless since both phases use matching bg colors.
    for det in bubble.text_regions:
        x1, y1, x2, y2 = det.bbox_rect
        det_bbox = (
            max(0, x1 - pad),
            max(0, y1 - pad),
            min(img_w, x2 + pad),
            min(img_h, y2 + pad),
        )
        local_bg = _sample_local_bg(img, det.bbox_rect)
        clear_text_in_region(img, det_bbox, fill_color=local_bg)


def _sample_local_bg(img: Image.Image,
                     bbox: tuple[int, int, int, int]) -> tuple[int, int, int]:
    """Sample the background color from a band around a text detection.

    Used for floating text outside bubble masks where the bubble-level
    bg_color may not match the local background.
    """
    img_array = np.array(img)
    h, w = img_array.shape[:2]
    x1, y1, x2, y2 = bbox
    band = 8

    regions = []
    if y1 - band >= 0:
        regions.append(img_array[max(0, y1 - band):y1, x1:x2])
    if y2 + band <= h:
        regions.append(img_array[y2:min(h, y2 + band), x1:x2])
    if x1 - band >= 0:
        regions.append(img_array[y1:y2, max(0, x1 - band):x1])
    if x2 + band <= w:
        regions.append(img_array[y1:y2, x2:min(w, x2 + band)])

    if not regions:
        return (255, 255, 255)

    pixels = np.concatenate([r.reshape(-1, 3) for r in regions if r.size > 0])
    if len(pixels) == 0:
        return (255, 255, 255)

    median = np.median(pixels, axis=0).astype(int)
    return (int(median[0]), int(median[1]), int(median[2]))


def _clear_with_mask(img: Image.Image,
                     bbox: tuple[int, int, int, int],
                     mask: np.ndarray,
                     fill_color: tuple[int, int, int]) -> None:
    """Clear text within bbox but only where the bubble mask allows.

    This prevents white/colored rectangles from leaking outside the
    bubble boundary into surrounding artwork.
    """
    x1, y1, x2, y2 = bbox
    # Clamp to image bounds
    x1 = max(0, x1)
    y1 = max(0, y1)
    x2 = min(img.width, x2)
    y2 = min(img.height, y2)
    if x2 <= x1 or y2 <= y1:
        return

    img_array = np.array(img)
    # Only fill pixels that are inside the bubble mask
    roi_mask = mask[y1:y2, x1:x2]
    img_array[y1:y2, x1:x2][roi_mask > 0] = fill_color
    img.paste(Image.fromarray(img_array))


def _text_region_bbox(bubble: WebtoonBubble, img_w: int,
                      img_h: int) -> tuple[int, int, int, int]:
    """Compute the actual text area from OCR detection bboxes + padding.

    The bubble.bbox (from contour detection) can be much larger than the
    actual text.  Using the tight union of detection bboxes + generous
    padding gives a render/clear area that matches where the Korean text
    actually is.
    """
    if not bubble.text_regions:
        return bubble.bbox

    x1 = min(d.bbox_rect[0] for d in bubble.text_regions)
    y1 = min(d.bbox_rect[1] for d in bubble.text_regions)
    x2 = max(d.bbox_rect[2] for d in bubble.text_regions)
    y2 = max(d.bbox_rect[3] for d in bubble.text_regions)

    # Generous padding — Korean glyphs often extend beyond tight OCR bbox
    pad_x, pad_y = 14, 10
    return (
        max(0, x1 - pad_x),
        max(0, y1 - pad_y),
        min(img_w, x2 + pad_x),
        min(img_h, y2 + pad_y),
    )


def _bg_luminance(color: tuple[int, int, int]) -> float:
    """Compute perceptual luminance (0=black, 1=white) from RGB."""
    r, g, b = color
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _render_webtoon_english(img: Image.Image, bubble: WebtoonBubble,
                            text: str,
                            render_bbox: tuple[int, int, int, int]) -> None:
    """Render English text within the text-region bbox.

    Uses render_bbox (tight around OCR detections) instead of bubble.bbox
    (contour-based, often oversized) to position text exactly where the
    original Korean text was.

    When the bubble has a mask, the semi-transparent background rectangle
    is clipped to the mask to prevent white rectangles leaking outside
    the bubble boundary.
    """
    bg_color = bubble.bg_color
    lum = _bg_luminance(bg_color)

    # Font color: white on dark, black on light
    if lum < 0.5:
        font_color = (255, 255, 255)
        bg_rect_color = (0, 0, 0, 160)
    else:
        font_color = (0, 0, 0)
        bg_rect_color = (255, 255, 255, 200)

    bx1, by1, bx2, by2 = render_bbox
    bw = bx2 - bx1
    bh = by2 - by1

    if bw < 10 or bh < 10:
        return

    # Find largest font size that fits within the render bbox.
    # Shrink bbox by 4px to prevent edge overflow from font rendering.
    fit_h = bh - 4
    fit_w = bw - 6
    target_font_size = max(10, min(28, int(fit_h * 0.45)))

    font = None
    lines: list[str] = []

    for size in range(target_font_size, 7, -1):
        try:
            f = ImageFont.truetype(FONT_KO, size)
        except OSError:
            continue
        wrapped = _wrap_text(text, f, fit_w)
        total_h = _total_block_height(f, wrapped)
        if total_h <= fit_h:
            font = f
            lines = wrapped
            break

    if font is None or not lines:
        try:
            font = ImageFont.truetype(FONT_KO, 8)
        except OSError:
            return
        lines = _wrap_text(text, font, fit_w)
        # If still too tall, truncate lines to fit
        total_h = _total_block_height(font, lines)
        if total_h > fit_h and len(lines) > 1:
            max_lines = max(1, fit_h // (_line_height(font, "X") + 3))
            lines = lines[:max_lines]
            lines[-1] = lines[-1].rstrip() + "..."

    if not lines:
        return

    # Calculate total text block height and center within render bbox
    line_heights = [_line_height(font, line) for line in lines]
    total_text_h = sum(line_heights) + 3 * (len(lines) - 1)

    text_y = by1 + max(0, (bh - total_text_h) // 2)
    # Hard clamp: never render below the bbox bottom (with 2px margin)
    if text_y + total_text_h > by2 - 2:
        text_y = by2 - 2 - total_text_h

    # Semi-transparent background rectangle (strictly within render bbox).
    # PIL's rounded_rectangle antialiasing can extend ~2px beyond the nominal
    # coords, so use a 3px inset from bbox edges.
    bg_y1 = max(by1 + 1, text_y - 2)
    bg_y2 = min(by2 - 3, text_y + total_text_h + 2)
    bg_x1 = bx1 + 1
    bg_x2 = bx2 - 2

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rounded_rectangle(
        (bg_x1, bg_y1, bg_x2, bg_y2), radius=3, fill=bg_rect_color,
    )

    # Clip the overlay to the bubble mask to prevent the bg rectangle
    # from leaking outside the bubble boundary (regression: 029).
    if bubble.bubble_mask is not None:
        overlay_arr = np.array(overlay)
        # Zero out alpha channel outside the mask
        overlay_arr[:, :, 3][bubble.bubble_mask == 0] = 0
        overlay = Image.fromarray(overlay_arr)

    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))

    # Render text lines centered horizontally
    draw = ImageDraw.Draw(img)
    y = text_y
    for line, lh in zip(lines, line_heights):
        bbox = font.getbbox(line)
        text_w = bbox[2] - bbox[0]
        x = bx1 + max(0, (bw - text_w) // 2)
        # Hard clamp: don't draw below render bbox
        if y + lh > by2 - 2:
            break
        draw.text((x, y), line, font=font, fill=font_color)
        y += lh + 3


def _wrap_text(text: str, font: ImageFont.FreeTypeFont,
               max_width: int) -> list[str]:
    """Word-wrap text to fit within max_width pixels."""
    words = text.split()
    if not words:
        return []

    lines = []
    current = words[0]

    for word in words[1:]:
        test = current + " " + word
        bbox = font.getbbox(test)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            lines.append(current)
            current = word

    lines.append(current)
    return lines


def _line_height(font: ImageFont.FreeTypeFont, text: str) -> int:
    """Get the pixel height of a line of text."""
    bbox = font.getbbox(text)
    return int(bbox[3] - bbox[1])


def _total_block_height(font: ImageFont.FreeTypeFont,
                        lines: list[str]) -> int:
    """Total pixel height of a wrapped text block including line spacing."""
    if not lines:
        return 0
    return (sum(_line_height(font, line) for line in lines)
            + 3 * (len(lines) - 1))


def _draw_webtoon_debug(page: WebtoonPageResult) -> Image.Image:
    """Draw debug bounding boxes for webtoon bubbles."""
    debug_img = page.img_pil.copy()
    draw = ImageDraw.Draw(debug_img)

    for bubble in page.bubbles:
        # Draw bubble bbox
        color = "lime" if bubble.has_bubble_boundary else "red"
        draw.rectangle(bubble.bbox, outline=color, width=2)

        # Draw individual text region bboxes
        for det in bubble.text_regions:
            draw.rectangle(det.bbox_rect, outline="cyan", width=1)

    return debug_img
