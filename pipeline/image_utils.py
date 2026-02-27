"""Image loading, cropping, and manipulation utilities."""

import base64
import io

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


def contour_fill_ratio(contour: np.ndarray) -> float:
    """Ratio of contour area to bounding rect area (0-1).

    High values (>0.7) indicate well-shaped bubbles. Low values suggest
    irregular contours that may have merged with faces or background.
    """
    x, y, w, h = cv2.boundingRect(contour)
    bbox_area = w * h
    if bbox_area == 0:
        return 0.0
    return cv2.contourArea(contour) / bbox_area


def contour_inner_bbox(contour: np.ndarray, margin: int = 8) -> tuple[int, int, int, int] | None:
    """Compute a layout bbox inset from the contour boundary.

    Erodes the contour mask by `margin` pixels and returns the bounding rect
    of the remaining area. This gives a rectangle fully inside the contour
    with margin from its edges, suitable for text layout.

    Returns (x1, y1, x2, y2) or None if the eroded area is too small.
    """
    x, y, w, h = cv2.boundingRect(contour)
    # Work on a local crop for efficiency
    pad = margin + 2
    mask = np.zeros((h + 2 * pad, w + 2 * pad), dtype=np.uint8)
    shifted = contour.copy()
    shifted[:, :, 0] -= x - pad
    shifted[:, :, 1] -= y - pad
    cv2.drawContours(mask, [shifted], -1, 255, -1)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                       (2 * margin + 1, 2 * margin + 1))
    eroded = cv2.erode(mask, kernel, iterations=1)

    coords = cv2.findNonZero(eroded)
    if coords is None:
        return None

    rx, ry, rw, rh = cv2.boundingRect(coords)
    # Map back to image coordinates
    ix1 = rx + x - pad
    iy1 = ry + y - pad
    ix2 = ix1 + rw
    iy2 = iy1 + rh

    if rw < 20 or rh < 20:
        return None
    return (ix1, iy1, ix2, iy2)


def clear_text_strokes(img: Image.Image, bbox: tuple[int, int, int, int],
                       margin: int = 1,
                       fill_color: tuple = (255, 255, 255)) -> None:
    """Clear the area covered by dark text strokes inside a bbox.

    Instead of filling the entire bbox (which overflows curved bubble borders),
    finds where the dark ink actually is, groups it into a tight cluster, adds
    a small margin, and clears only that sub-region.
    """
    from PIL import ImageDraw

    x1, y1, x2, y2 = bbox
    roi = np.array(img.crop(bbox).convert("L"))
    if roi.size == 0:
        return

    # Find dark text strokes (ink on white background)
    dark_mask = (roi < 160).astype(np.uint8) * 255

    # Dilate to connect nearby strokes into solid text clusters
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    connected = cv2.dilate(dark_mask, kernel, iterations=1)

    coords = cv2.findNonZero(connected)
    if coords is None:
        return

    # Bounding rect of all dark pixels = the text cluster area
    rx, ry, rw, rh = cv2.boundingRect(coords)

    # Add margin but stay inside the original bbox
    cx1 = x1 + max(0, rx - margin)
    cy1 = y1 + max(0, ry - margin)
    cx2 = x1 + min(roi.shape[1], rx + rw + margin)
    cy2 = y1 + min(roi.shape[0], ry + rh + margin)

    draw = ImageDraw.Draw(img)
    draw.rectangle((cx1, cy1, cx2, cy2), fill=fill_color)


def clear_text_in_contour(img: Image.Image, contour: np.ndarray,
                          fill_color: tuple = (255, 255, 255)) -> None:
    """Fill the interior of a contour with a solid color (default white) in-place.

    Uses the bubble's actual contour shape instead of a bounding rectangle,
    preserving artwork outside the bubble boundary.  After the contour fill,
    erases any remaining dark text strokes inside the bbox that the contour
    didn't cover — but preserves the bubble border by only targeting dark
    pixel islands that don't touch the bbox edge.
    """
    img_array = np.array(img)
    mask = np.zeros(img_array.shape[:2], dtype=np.uint8)
    cv2.drawContours(mask, [contour], -1, 255, -1)
    img_array[mask == 255] = fill_color

    # Second pass: erase leftover text strokes outside the contour.
    # The contour often doesn't fully cover the bubble interior, leaving
    # Japanese text visible near the edges.  We find dark pixel clusters
    # that are inside the bbox but outside the contour, and erase only
    # the ones that don't touch the bbox edge (those are text, not border).
    x, y, w, h = cv2.boundingRect(contour)
    gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
    roi_gray = gray[y:y + h, x:x + w]
    roi_cmask = mask[y:y + h, x:x + w]
    uncovered_dark = ((roi_cmask == 0) & (roi_gray < 150)).astype(np.uint8) * 255

    if np.any(uncovered_dark):
        n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
            uncovered_dark, connectivity=8)
        for i in range(1, n_labels):
            if stats[i, cv2.CC_STAT_AREA] < 10:
                continue
            comp = (labels == i)
            touches = (np.any(comp[0, :]) or np.any(comp[-1, :]) or
                       np.any(comp[:, 0]) or np.any(comp[:, -1]))
            if not touches:
                img_array[y:y + h, x:x + w][comp] = fill_color

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


def decode_image_bytes(data: bytes) -> tuple[np.ndarray, Image.Image]:
    """Decode image bytes to OpenCV BGR + Pillow RGB."""
    nparr = np.frombuffer(data, np.uint8)
    img_cv = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img_cv is None:
        raise ValueError("Could not decode image from bytes")
    img_pil = Image.open(io.BytesIO(data)).convert("RGB")
    return img_cv, img_pil


def encode_image_pil(img: Image.Image, fmt: str = "PNG") -> bytes:
    """Encode Pillow image to bytes."""
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()
