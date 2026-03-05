"""AI inpainting for manga artwork text removal.

Thin wrapper around the LaMa backend from webtoon/inpainter.py.
Used to clean text overlaid on artwork before rendering English translations.

Shares the GPU inference lock with the webtoon inpainter to prevent
concurrent GPU access.
"""

import logging
import threading

import numpy as np
from PIL import Image, ImageFilter

from .config import MANGA_INPAINT_PAD

log = logging.getLogger(__name__)

# Import the shared inference lock and backend class from webtoon
from webtoon.inpainter import LamaBackend, _infer_lock

_backend: LamaBackend | None = None
_init_lock = threading.Lock()


def _get_backend() -> LamaBackend | None:
    """Lazy-load LaMa backend on first use."""
    global _backend
    if _backend is None:
        with _init_lock:
            if _backend is None:
                instance = LamaBackend()
                if not instance.load():
                    return None
                _backend = instance
    return _backend


def inpaint_region(
    img_pil: Image.Image,
    bbox: tuple[int, int, int, int],
    pad: int = MANGA_INPAINT_PAD,
) -> Image.Image:
    """Inpaint a text region on artwork. Returns modified image copy.

    Crops the region with padding, builds a mask covering the text bbox,
    runs LaMa inpainting, and composites the result back.

    Args:
        img_pil: Full page image (PIL RGB).
        bbox: Text region bounding box (x1, y1, x2, y2).
        pad: Padding around text region for inpainting context.

    Returns:
        Modified copy of img_pil with text region inpainted.
        Returns the original image unchanged if inpainting fails.
    """
    backend = _get_backend()
    if backend is None:
        log.warning("LaMa backend unavailable, skipping inpaint")
        return img_pil

    img_w, img_h = img_pil.size
    x1, y1, x2, y2 = bbox

    # Crop with padding for context
    cx1 = max(0, x1 - pad)
    cy1 = max(0, y1 - pad)
    cx2 = min(img_w, x2 + pad)
    cy2 = min(img_h, y2 + pad)

    # Pad to multiple of 8 (required by LaMa)
    cw = cx2 - cx1
    ch = cy2 - cy1
    pad_w = (8 - cw % 8) % 8
    pad_h = (8 - ch % 8) % 8
    cx2 = min(img_w, cx2 + pad_w)
    cy2 = min(img_h, cy2 + pad_h)

    crop_box = (cx1, cy1, cx2, cy2)
    img_crop = img_pil.crop(crop_box)

    # Build mask: white where text is (relative to crop)
    crop_w = cx2 - cx1
    crop_h = cy2 - cy1
    mask = Image.new("L", (crop_w, crop_h), 0)
    mask_arr = np.array(mask)

    # Text bbox relative to crop, with small dilation for glyph edges
    tx1 = max(0, x1 - cx1 - 4)
    ty1 = max(0, y1 - cy1 - 4)
    tx2 = min(crop_w, x2 - cx1 + 4)
    ty2 = min(crop_h, y2 - cy1 + 4)
    mask_arr[ty1:ty2, tx1:tx2] = 255

    # Dilate mask slightly for antialiasing
    mask = Image.fromarray(mask_arr)
    mask = mask.filter(ImageFilter.MaxFilter(5))

    # Thread-safe inference
    with _infer_lock:
        try:
            result_crop = backend.inpaint(img_crop, mask)
        except Exception:
            log.exception("Inpainting failed for bbox %s", bbox)
            return img_pil

    # Composite: only replace pixels where mask is white
    result = img_pil.copy()
    mask_arr = np.array(mask)
    result_arr = np.array(result_crop)
    target_arr = np.array(result.crop(crop_box))

    if result_arr.shape != target_arr.shape:
        result_crop = result_crop.resize((crop_w, crop_h), Image.LANCZOS)
        result_arr = np.array(result_crop)

    composite = np.where(
        mask_arr[:, :, np.newaxis] > 0,
        result_arr,
        target_arr,
    )
    result.paste(Image.fromarray(composite.astype(np.uint8)), (cx1, cy1))

    return result
