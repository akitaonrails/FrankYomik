"""Image loading, cropping, and manipulation utilities."""

import base64
import cv2
import numpy as np
from PIL import Image


def load_image(path: str) -> np.ndarray:
    """Load image as OpenCV BGR array."""
    img = cv2.imread(path)
    if img is None:
        raise FileNotFoundError(f"Could not load image: {path}")
    return img


def load_image_pil(path: str) -> Image.Image:
    """Load image as Pillow RGB Image."""
    return Image.open(path).convert("RGB")


def crop_region(img: np.ndarray, bbox: tuple[int, int, int, int]) -> np.ndarray:
    """Crop a region from an OpenCV image. bbox = (x1, y1, x2, y2)."""
    x1, y1, x2, y2 = bbox
    return img[y1:y2, x1:x2].copy()


def crop_region_pil(img: Image.Image, bbox: tuple[int, int, int, int]) -> Image.Image:
    """Crop a region from a Pillow image. bbox = (x1, y1, x2, y2)."""
    return img.crop(bbox)


def clear_text_in_region(img: Image.Image, bbox: tuple[int, int, int, int],
                         fill_color: tuple = (255, 255, 255)) -> None:
    """Fill a bounding box region with a solid color (default white) in-place."""
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    draw.rectangle(bbox, fill=fill_color)


def clear_text_in_contour(img: Image.Image, contour: np.ndarray,
                          fill_color: tuple = (255, 255, 255)) -> None:
    """Fill the interior of a contour with a solid color (default white) in-place.

    Uses the bubble's actual contour shape instead of a bounding rectangle,
    preserving artwork outside the bubble boundary.
    """
    img_array = np.array(img)
    mask = np.zeros(img_array.shape[:2], dtype=np.uint8)
    cv2.drawContours(mask, [contour], -1, 255, -1)
    img_array[mask == 255] = fill_color
    img.paste(Image.fromarray(img_array))


def normalize_bbox(bbox_norm: list[int], img_width: int, img_height: int) -> tuple[int, int, int, int]:
    """Convert Qwen's 0-999 normalized coordinates to pixel coordinates.

    Qwen VL models output bounding boxes as [x1, y1, x2, y2] in 0-999 range.
    """
    x1 = int(bbox_norm[0] / 999 * img_width)
    y1 = int(bbox_norm[1] / 999 * img_height)
    x2 = int(bbox_norm[2] / 999 * img_width)
    y2 = int(bbox_norm[3] / 999 * img_height)
    return (
        max(0, min(x1, img_width)),
        max(0, min(y1, img_height)),
        max(0, min(x2, img_width)),
        max(0, min(y2, img_height)),
    )


def image_to_base64(path: str) -> str:
    """Read an image file and return base64-encoded string."""
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def pil_to_cv2(img: Image.Image) -> np.ndarray:
    """Convert Pillow RGB image to OpenCV BGR array."""
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def cv2_to_pil(img: np.ndarray) -> Image.Image:
    """Convert OpenCV BGR array to Pillow RGB image."""
    return Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
