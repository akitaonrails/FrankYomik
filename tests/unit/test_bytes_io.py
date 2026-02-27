"""Tests for bytes I/O functions (Phase 1 of web service)."""

from unittest.mock import patch

import numpy as np
from PIL import Image

from pipeline.image_utils import decode_image_bytes, encode_image_pil


def _make_test_image(width: int = 100, height: int = 80) -> Image.Image:
    """Create a simple test image with known content."""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    draw.rectangle((20, 20, 60, 60), fill=(0, 0, 0))
    return img


# --- encode_image_pil ---


class TestEncodeImagePil:
    def test_png_roundtrip(self):
        img = _make_test_image()
        data = encode_image_pil(img, fmt="PNG")
        assert isinstance(data, bytes)
        assert len(data) > 0
        assert data[:4] == b'\x89PNG'

    def test_jpeg_roundtrip(self):
        img = _make_test_image()
        data = encode_image_pil(img, fmt="JPEG")
        assert isinstance(data, bytes)
        assert data[:2] == b'\xff\xd8'

    def test_default_format_is_png(self):
        img = _make_test_image()
        data = encode_image_pil(img)
        assert data[:4] == b'\x89PNG'

    def test_returns_different_sizes_for_different_formats(self):
        img = _make_test_image()
        png_data = encode_image_pil(img, fmt="PNG")
        jpeg_data = encode_image_pil(img, fmt="JPEG")
        # PNG and JPEG produce different byte lengths for same image
        assert len(png_data) != len(jpeg_data)

    def test_different_images_produce_different_bytes(self):
        img1 = Image.new("RGB", (50, 50), (255, 0, 0))
        img2 = Image.new("RGB", (50, 50), (0, 0, 255))
        assert encode_image_pil(img1) != encode_image_pil(img2)


# --- decode_image_bytes ---


class TestDecodeImageBytes:
    def test_decode_png(self):
        original = _make_test_image(120, 90)
        png_bytes = encode_image_pil(original, fmt="PNG")

        img_cv, img_pil = decode_image_bytes(png_bytes)

        assert isinstance(img_cv, np.ndarray)
        assert img_cv.shape == (90, 120, 3)
        assert isinstance(img_pil, Image.Image)
        assert img_pil.size == (120, 90)
        assert img_pil.mode == "RGB"

    def test_decode_jpeg(self):
        original = _make_test_image(200, 150)
        jpeg_bytes = encode_image_pil(original, fmt="JPEG")

        img_cv, img_pil = decode_image_bytes(jpeg_bytes)
        assert img_cv.shape == (150, 200, 3)
        assert img_pil.size == (200, 150)

    def test_decode_invalid_bytes_raises(self):
        import pytest
        with pytest.raises(ValueError, match="Could not decode"):
            decode_image_bytes(b"not an image")

    def test_decode_empty_bytes_raises(self):
        import pytest
        with pytest.raises((ValueError, Exception)):
            decode_image_bytes(b"")

    def test_pixel_values_preserved_png(self):
        """PNG is lossless — pixel values should be exactly preserved."""
        original = _make_test_image()
        png_bytes = encode_image_pil(original, fmt="PNG")
        _, decoded_pil = decode_image_bytes(png_bytes)

        original_arr = np.array(original)
        decoded_arr = np.array(decoded_pil)
        np.testing.assert_array_equal(original_arr, decoded_arr)

    def test_cv2_is_bgr_pil_is_rgb(self):
        """OpenCV should be BGR, Pillow should be RGB."""
        img = Image.new("RGB", (10, 10), (255, 0, 0))  # Pure red
        data = encode_image_pil(img)
        img_cv, img_pil = decode_image_bytes(data)

        # Pillow: R channel is 255
        pil_arr = np.array(img_pil)
        assert pil_arr[0, 0, 0] == 255  # R
        assert pil_arr[0, 0, 2] == 0    # B

        # OpenCV BGR: B channel is at index 0, R at index 2
        assert img_cv[0, 0, 2] == 255   # R (at index 2 in BGR)
        assert img_cv[0, 0, 0] == 0     # B (at index 0 in BGR)


# --- load_page_from_memory ---


class TestLoadPageFromMemory:
    def test_manga_pipeline(self):
        from pipeline.processor import load_page_from_memory, PageResult
        img_pil = _make_test_image()
        img_cv = np.array(img_pil)[:, :, ::-1].copy()

        page = load_page_from_memory(img_cv, img_pil, name="test_page")

        assert isinstance(page, PageResult)
        assert page.name == "test_page"
        assert page.image_path == ""
        assert page.img_cv is img_cv
        assert page.img_pil is img_pil
        assert page.bubbles_raw == []
        assert page.bubble_results == []
        assert page.output_img is None

    def test_manga_default_name(self):
        from pipeline.processor import load_page_from_memory
        img_pil = _make_test_image()
        img_cv = np.array(img_pil)[:, :, ::-1].copy()

        page = load_page_from_memory(img_cv, img_pil)
        assert page.name == "page"

    def test_webtoon_pipeline(self):
        from webtoon.processor import load_page_from_memory, WebtoonPageResult
        img_pil = _make_test_image()
        img_cv = np.array(img_pil)[:, :, ::-1].copy()

        page = load_page_from_memory(img_cv, img_pil, name="webtoon_test")

        assert isinstance(page, WebtoonPageResult)
        assert page.name == "webtoon_test"
        assert page.image_path == ""
        assert page.detections == []
        assert page.bubbles == []
        assert page.regions == []

    def test_webtoon_default_name(self):
        from webtoon.processor import load_page_from_memory
        img_pil = _make_test_image()
        img_cv = np.array(img_pil)[:, :, ::-1].copy()

        page = load_page_from_memory(img_cv, img_pil)
        assert page.name == "page"


# --- render_page_to_bytes ---


class TestRenderPageToBytes:
    def test_manga_translate_returns_png(self):
        """render_page_to_bytes should produce valid PNG bytes."""
        from pipeline.processor import (
            PipelineMode, load_page_from_memory, render_page_to_bytes,
        )
        img_pil = _make_test_image(200, 300)
        img_cv = np.array(img_pil)[:, :, ::-1].copy()
        page = load_page_from_memory(img_cv, img_pil, name="rpt-test")

        # No bubbles — should still render the image as-is
        result = render_page_to_bytes(page, PipelineMode.TRANSLATE)

        assert isinstance(result, bytes)
        assert result[:4] == b'\x89PNG'
        assert len(result) > 0

    def test_manga_furigana_returns_png(self):
        from pipeline.processor import (
            PipelineMode, load_page_from_memory, render_page_to_bytes,
        )
        img_pil = _make_test_image(200, 300)
        img_cv = np.array(img_pil)[:, :, ::-1].copy()
        page = load_page_from_memory(img_cv, img_pil)

        result = render_page_to_bytes(page, PipelineMode.FURIGANA)
        assert result[:4] == b'\x89PNG'

    def test_output_image_dimensions_match_input(self):
        from pipeline.processor import (
            PipelineMode, load_page_from_memory, render_page_to_bytes,
        )
        img_pil = _make_test_image(320, 480)
        img_cv = np.array(img_pil)[:, :, ::-1].copy()
        page = load_page_from_memory(img_cv, img_pil)

        result_bytes = render_page_to_bytes(page, PipelineMode.TRANSLATE)
        _, decoded = decode_image_bytes(result_bytes)
        assert decoded.size == (320, 480)

    def test_page_with_bubble_results(self):
        """When bubble_results have no transform, output is still valid."""
        from pipeline.processor import (
            BubbleResult, PipelineMode, load_page_from_memory,
            render_page_to_bytes,
        )
        img_pil = _make_test_image(200, 300)
        img_cv = np.array(img_pil)[:, :, ::-1].copy()
        page = load_page_from_memory(img_cv, img_pil)
        # Add a bubble result with no transform (will be skipped)
        page.bubble_results = [
            BubbleResult(bbox=(10, 10, 50, 50), contour=None,
                         ocr_text="テスト", is_valid=True, transformed=None),
        ]

        result = render_page_to_bytes(page, PipelineMode.TRANSLATE)
        assert result[:4] == b'\x89PNG'

    def test_sets_output_img(self):
        """render_page_to_bytes should set page.output_img as a side effect."""
        from pipeline.processor import (
            PipelineMode, load_page_from_memory, render_page_to_bytes,
        )
        img_pil = _make_test_image()
        img_cv = np.array(img_pil)[:, :, ::-1].copy()
        page = load_page_from_memory(img_cv, img_pil)

        assert page.output_img is None
        render_page_to_bytes(page, PipelineMode.TRANSLATE)
        assert page.output_img is not None
        assert isinstance(page.output_img, Image.Image)


class TestWebtoonRenderPageToBytes:
    def test_returns_png(self):
        from webtoon.processor import (
            load_page_from_memory as wt_load,
            render_page_to_bytes as wt_render,
        )
        img_pil = _make_test_image(200, 300)
        img_cv = np.array(img_pil)[:, :, ::-1].copy()
        page = wt_load(img_cv, img_pil, name="wt-rpt")

        # Mock inpaint_bubble to avoid needing LaMa model
        with patch("webtoon.processor.inpaint_bubble", return_value=False):
            result = wt_render(page)

        assert isinstance(result, bytes)
        assert result[:4] == b'\x89PNG'

    def test_dimensions_match(self):
        from webtoon.processor import (
            load_page_from_memory as wt_load,
            render_page_to_bytes as wt_render,
        )
        img_pil = _make_test_image(400, 600)
        img_cv = np.array(img_pil)[:, :, ::-1].copy()
        page = wt_load(img_cv, img_pil)

        with patch("webtoon.processor.inpaint_bubble", return_value=False):
            result_bytes = wt_render(page)

        _, decoded = decode_image_bytes(result_bytes)
        assert decoded.size == (400, 600)
