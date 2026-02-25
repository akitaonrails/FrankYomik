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

        # Step 1: Clear Korean text before rendering English
        if bubble.has_bubble_boundary:
            # For real bubbles, clear individual detections to preserve artwork
            pad = 6
            for det in bubble.text_regions:
                x1, y1, x2, y2 = det.bbox_rect
                clear_bbox = (
                    max(0, x1 - pad),
                    max(0, y1 - pad),
                    min(page.output_img.width, x2 + pad),
                    min(page.output_img.height, y2 + pad),
                )
                clear_text_in_region(page.output_img, clear_bbox, fill_color=bg_color)
        else:
            # For narration text (no bubble boundary), clear the full padded
            # cluster bbox — it's already a tight fit around the text
            bx1, by1, bx2, by2 = bubble.bbox
            clear_text_in_region(page.output_img, (bx1, by1, bx2, by2),
                                 fill_color=bg_color)

        # Step 2: Render English text with color-aware styling
        _render_webtoon_english(page.output_img, bubble, region.english)

    output_path = os.path.join(out_dir, f"{page.name}-en.png")
    page.output_img.save(output_path)
    log.info("Saved: %s", output_path)


def _bg_luminance(color: tuple[int, int, int]) -> float:
    """Compute perceptual luminance (0=black, 1=white) from RGB."""
    r, g, b = color
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _render_webtoon_english(img: Image.Image, bubble: WebtoonBubble,
                            text: str) -> None:
    """Render English text with color-aware font and background.

    - Chooses white text on dark backgrounds, black on light
    - Adds a semi-transparent background rectangle for readability
    - Sizes font based on the original Korean text region height
    """
    draw = ImageDraw.Draw(img)
    bg_color = bubble.bg_color
    lum = _bg_luminance(bg_color)

    # Font color: white on dark, black on light
    if lum < 0.5:
        font_color = (255, 255, 255)
        bg_rect_color = (0, 0, 0, 160)      # Semi-transparent black
    else:
        font_color = (0, 0, 0)
        bg_rect_color = (255, 255, 255, 200) # Semi-transparent white

    # Use bubble bbox as render area
    bx1, by1, bx2, by2 = bubble.bbox
    bw = bx2 - bx1
    bh = by2 - by1

    if bw < 10 or bh < 10:
        return

    # Size font based on original text region height
    text_heights = [d.bbox_rect[3] - d.bbox_rect[1] for d in bubble.text_regions]
    avg_text_h = sum(text_heights) / len(text_heights) if text_heights else 20
    # Start font size from ~80% of the average detected text line height
    target_font_size = max(12, min(36, int(avg_text_h * 0.8)))

    # Find largest font size that fits within the bubble
    font = None
    lines = []

    for size in range(target_font_size, 9, -1):
        try:
            f = ImageFont.truetype(FONT_KO, size)
        except OSError:
            continue
        wrapped = _wrap_text(text, f, bw - 8)
        total_h = sum(_line_height(f, line) for line in wrapped) + 4 * (len(wrapped) - 1)
        if total_h <= bh - 4:
            font = f
            lines = wrapped
            break

    if font is None or not lines:
        # Fallback: use smallest font
        try:
            font = ImageFont.truetype(FONT_KO, 10)
        except OSError:
            return
        lines = _wrap_text(text, font, bw - 8)

    if not lines:
        return

    # Calculate total text block height
    line_heights = [_line_height(font, line) for line in lines]
    total_text_h = sum(line_heights) + 4 * (len(lines) - 1)

    # Center text block within bubble bbox
    text_y = by1 + max(0, (bh - total_text_h) // 2)

    # Draw background rectangle
    bg_rect_x1 = bx1
    bg_rect_y1 = max(by1, text_y - 4)
    bg_rect_x2 = bx2
    bg_rect_y2 = min(by2, text_y + total_text_h + 4)

    # Use RGBA overlay for semi-transparent background
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rounded_rectangle(
        (bg_rect_x1, bg_rect_y1, bg_rect_x2, bg_rect_y2),
        radius=4,
        fill=bg_rect_color,
    )
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))

    # Render text lines centered
    draw = ImageDraw.Draw(img)  # Re-acquire after paste
    y = text_y
    for line, lh in zip(lines, line_heights):
        bbox = font.getbbox(line)
        text_w = bbox[2] - bbox[0]
        x = bx1 + max(0, (bw - text_w) // 2)
        draw.text((x, y), line, font=font, fill=font_color)
        y += lh + 4


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
