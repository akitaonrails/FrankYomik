"""AI inpainting for webtoon balloon text removal.

Replaces the solid-fill text clearing with AI-powered inpainting that
naturally reconstructs balloon interiors (semi-transparent panels, gradients,
brush textures) before rendering English text on top.

Follows the lazy-singleton + thread-lock pattern from webtoon/ocr.py.
"""

import logging
import threading
from abc import ABC, abstractmethod

import numpy as np
from PIL import Image, ImageFilter

from .bubble_detector import WebtoonBubble
from .config import (
    INPAINT_CONTEXT_PAD,
    INPAINT_ENABLED,
    INPAINT_ERODE_PX,
    INPAINT_MODEL,
    INPAINT_PROMPT,
    INPAINT_STEPS,
    INPAINT_TEXT_DILATE,
    INPAINT_TEXT_PAD,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backend abstraction
# ---------------------------------------------------------------------------

class InpaintBackend(ABC):
    """Abstract base for inpainting model backends."""

    @abstractmethod
    def load(self) -> bool:
        """Load model weights. Return True on success."""

    @abstractmethod
    def inpaint(self, image: Image.Image, mask: Image.Image) -> Image.Image:
        """Inpaint *image* where *mask* is white. Return result same size."""

    @abstractmethod
    def unload(self) -> None:
        """Release GPU memory."""


class LamaBackend(InpaintBackend):
    """LaMa (Large Mask Inpainting) via simple-lama-inpainting."""

    def __init__(self):
        self._model = None

    def load(self) -> bool:
        try:
            from simple_lama_inpainting import SimpleLama
            self._model = SimpleLama()
            log.info("LaMa inpainting model loaded")
            return True
        except ImportError:
            log.warning("simple-lama-inpainting not installed; "
                        "pip install simple-lama-inpainting")
            return False

    def inpaint(self, image: Image.Image, mask: Image.Image) -> Image.Image:
        result = self._model(image.convert("RGB"), mask.convert("L"))
        return result.convert("RGB")

    def unload(self) -> None:
        if self._model is not None:
            del self._model
            self._model = None
            _cuda_empty_cache()


class FluxFillBackend(InpaintBackend):
    """Flux.1-Fill-dev via diffusers with CPU offload."""

    def __init__(self):
        self._pipe = None

    def load(self) -> bool:
        try:
            import torch
            from diffusers import FluxFillPipeline
            self._pipe = FluxFillPipeline.from_pretrained(
                "black-forest-labs/FLUX.1-Fill-dev",
                torch_dtype=torch.bfloat16,
            )
            self._pipe.enable_model_cpu_offload()
            log.info("Flux.1-Fill-dev inpainting model loaded")
            return True
        except ImportError:
            log.warning("diffusers/transformers/accelerate not installed; "
                        "pip install diffusers transformers accelerate")
            return False

    def inpaint(self, image: Image.Image, mask: Image.Image) -> Image.Image:
        result = self._pipe(
            prompt=INPAINT_PROMPT,
            image=image.convert("RGB"),
            mask_image=mask.convert("L"),
            num_inference_steps=INPAINT_STEPS,
            height=image.height,
            width=image.width,
        ).images[0]
        return result.convert("RGB")

    def unload(self) -> None:
        if self._pipe is not None:
            del self._pipe
            self._pipe = None
            _cuda_empty_cache()


class SDXLInpaintBackend(InpaintBackend):
    """Stable Diffusion XL Inpainting via diffusers."""

    def __init__(self):
        self._pipe = None

    def load(self) -> bool:
        try:
            import torch
            from diffusers import StableDiffusionXLInpaintPipeline
            self._pipe = StableDiffusionXLInpaintPipeline.from_pretrained(
                "diffusers/stable-diffusion-xl-1.0-inpainting-0.1",
                torch_dtype=torch.float16,
            )
            self._pipe.enable_model_cpu_offload()
            log.info("SDXL inpainting model loaded")
            return True
        except ImportError:
            log.warning("diffusers/transformers/accelerate not installed; "
                        "pip install diffusers transformers accelerate")
            return False

    def inpaint(self, image: Image.Image, mask: Image.Image) -> Image.Image:
        result = self._pipe(
            prompt=INPAINT_PROMPT,
            image=image.convert("RGB"),
            mask_image=mask.convert("L"),
            num_inference_steps=INPAINT_STEPS,
        ).images[0]
        # SDXL may resize; resize back to match input
        if result.size != image.size:
            result = result.resize(image.size, Image.LANCZOS)
        return result.convert("RGB")

    def unload(self) -> None:
        if self._pipe is not None:
            del self._pipe
            self._pipe = None
            _cuda_empty_cache()


def _cuda_empty_cache():
    """Free GPU memory if torch is available."""
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Singleton backend management
# ---------------------------------------------------------------------------

_BACKEND_MAP = {
    "lama": LamaBackend,
    "flux-fill": FluxFillBackend,
    "sdxl": SDXLInpaintBackend,
}

_backend: InpaintBackend | None = None
_init_lock = threading.Lock()
_infer_lock = threading.Lock()


def _get_backend() -> InpaintBackend | None:
    """Lazy-load the configured inpainting backend. Returns None when disabled."""
    global _backend
    if not INPAINT_ENABLED:
        return None
    if _backend is None:
        with _init_lock:
            if _backend is None:
                cls = _BACKEND_MAP.get(INPAINT_MODEL)
                if cls is None:
                    log.error("Unknown inpainting model %r; "
                              "choose from %s", INPAINT_MODEL,
                              list(_BACKEND_MAP.keys()))
                    return None
                instance = cls()
                if not instance.load():
                    return None
                _backend = instance
    return _backend


def get_backend_for_name(name: str) -> InpaintBackend | None:
    """Create and load a backend by name (for A/B testing). Caller manages lifecycle."""
    cls = _BACKEND_MAP.get(name)
    if cls is None:
        log.error("Unknown inpainting model %r", name)
        return None
    instance = cls()
    if not instance.load():
        return None
    return instance


# ---------------------------------------------------------------------------
# Mask building
# ---------------------------------------------------------------------------

def build_inpaint_mask(
    bubble: WebtoonBubble,
    img_size: tuple[int, int],
    erode_px: int = INPAINT_ERODE_PX,
    text_pad: int = INPAINT_TEXT_PAD,
    text_dilate: int = INPAINT_TEXT_DILATE,
) -> Image.Image | None:
    """Build binary mask: white=inpaint (text inside balloon), black=keep.

    1. Start with bubble.bubble_mask (return None if absent → fallback)
    2. Erode by erode_px (preserve balloon outline)
    3. Build text rects mask (union of padded detection bboxes)
    4. Intersect: eroded_bubble AND text_rects (only inpaint where text is)
    5. Dilate result by text_dilate px (catch glyph antialiasing edges)
    """
    if bubble.bubble_mask is None:
        return None
    if not bubble.text_regions:
        return None

    w, h = img_size

    # Step 1: bubble mask as PIL
    bubble_mask = Image.fromarray(bubble.bubble_mask)

    # Step 2: erode to preserve balloon outline
    if erode_px > 0:
        bubble_mask = bubble_mask.filter(ImageFilter.MinFilter(erode_px * 2 + 1))

    # Step 3: build text rects mask (union of padded detection bboxes)
    text_mask = Image.new("L", (w, h), 0)
    text_arr = np.array(text_mask)
    for det in bubble.text_regions:
        x1, y1, x2, y2 = det.bbox_rect
        x1 = max(0, x1 - text_pad)
        y1 = max(0, y1 - text_pad)
        x2 = min(w, x2 + text_pad)
        y2 = min(h, y2 + text_pad)
        text_arr[y1:y2, x1:x2] = 255
    text_mask = Image.fromarray(text_arr)

    # Step 4: intersect — only inpaint where text is AND inside bubble.
    # If the bubble mask poorly covers the text area (<85%), skip the
    # intersection and use text rects alone.  Incomplete masks leave
    # Korean remnants that the AI model can't clean.
    bubble_arr = np.array(bubble_mask)
    text_arr = np.array(text_mask)

    text_pixels = np.count_nonzero(text_arr)
    overlap_pixels = np.count_nonzero((bubble_arr > 0) & (text_arr > 0))
    coverage = overlap_pixels / max(1, text_pixels)

    if coverage >= 0.85:
        # Good coverage — clip to bubble boundary
        result_arr = np.where((bubble_arr > 0) & (text_arr > 0), 255, 0).astype(np.uint8)
    else:
        # Poor coverage — use text rects directly so all text gets inpainted
        log.info("Bubble mask coverage %.0f%% < 85%%, using text rects only",
                 coverage * 100)
        result_arr = text_arr.copy()

    # Step 5: dilate to catch glyph antialiasing
    if text_dilate > 0:
        result = Image.fromarray(result_arr)
        result = result.filter(ImageFilter.MaxFilter(text_dilate * 2 + 1))
        result_arr = np.array(result)

    if np.count_nonzero(result_arr) == 0:
        return None

    return Image.fromarray(result_arr)


# ---------------------------------------------------------------------------
# Main inpainting function
# ---------------------------------------------------------------------------

def inpaint_bubble(
    original_img: Image.Image,
    bubble: WebtoonBubble,
    context_pad: int = INPAINT_CONTEXT_PAD,
    backend: InpaintBackend | None = None,
    target_img: Image.Image | None = None,
) -> bool:
    """Inpaint a single bubble's text area.

    Args:
        original_img: Original unmodified page image (PIL RGB).
            Used as input for the AI model (it needs surrounding context).
        bubble: The bubble whose text to inpaint.
        context_pad: Extra pixels around bubble crop for model context.
        backend: Override backend (for A/B testing). Uses singleton if None.
        target_img: Image to paste the inpainted result onto (accumulated
            output). If None, pastes onto original_img.

    Returns:
        True if inpainting was applied, False if unavailable/failed.
    """
    be = backend or _get_backend()
    if be is None:
        return False

    img_w, img_h = original_img.size
    mask = build_inpaint_mask(bubble, (img_w, img_h))
    if mask is None:
        return False

    # Crop to bubble bbox + context padding
    bx1, by1, bx2, by2 = bubble.bbox
    cx1 = max(0, bx1 - context_pad)
    cy1 = max(0, by1 - context_pad)
    cx2 = min(img_w, bx2 + context_pad)
    cy2 = min(img_h, by2 + context_pad)

    # Pad to multiple of 8 (diffusion models require this)
    cw = cx2 - cx1
    ch = cy2 - cy1
    pad_w = (8 - cw % 8) % 8
    pad_h = (8 - ch % 8) % 8
    cx2 = min(img_w, cx2 + pad_w)
    cy2 = min(img_h, cy2 + pad_h)

    crop_box = (cx1, cy1, cx2, cy2)
    img_crop = original_img.crop(crop_box)
    mask_crop = mask.crop(crop_box)

    # Thread-safe inference (GPU not safe for concurrent use)
    with _infer_lock:
        try:
            result_crop = be.inpaint(img_crop, mask_crop)
        except Exception:
            log.exception("Inpainting failed for bubble at %s", bubble.bbox)
            return False

    # Composite: paste inpainted pixels onto the target image
    dest = target_img if target_img is not None else original_img
    crop_size = img_crop.size  # (width, height)

    # Ensure result matches crop dimensions (backend may return different size)
    if result_crop.size != crop_size:
        result_crop = result_crop.resize(crop_size, Image.LANCZOS)

    mask_arr = np.array(mask_crop)
    result_arr = np.array(result_crop)
    # Use pixels from the current target (preserves previous inpainting work)
    target_crop_arr = np.array(dest.crop(crop_box))

    # Only replace pixels where mask is white
    composite = np.where(
        mask_arr[:, :, np.newaxis] > 0,
        result_arr,
        target_crop_arr,
    )
    dest.paste(Image.fromarray(composite.astype(np.uint8)), (cx1, cy1))

    return True
