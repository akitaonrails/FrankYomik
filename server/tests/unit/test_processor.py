"""Unit tests for processor pipeline stages (mocked dependencies)."""

from unittest.mock import patch, MagicMock
import numpy as np
from PIL import Image

from kindle.processor import (
    BubbleResult,
    PageResult,
    PipelineMode,
    load_page,
    ocr_bubble,
    transform_furigana,
    transform_translate,
)


class TestLoadPage:
    @patch("kindle.processor.load_image_pil")
    @patch("kindle.processor.load_image")
    def test_sets_name_and_images(self, mock_cv, mock_pil):
        mock_cv.return_value = np.zeros((10, 10, 3), dtype=np.uint8)
        mock_pil.return_value = Image.new("RGB", (10, 10))

        page = load_page("/tmp/test_page.png")
        assert page.name == "test_page"
        assert page.image_path == "/tmp/test_page.png"
        assert page.img_cv is not None
        assert page.img_pil is not None


class TestOcrBubbleArtwork:
    @patch("kindle.processor.is_valid_japanese", return_value=True)
    @patch("kindle.processor.extract_text_from_region", return_value="テスト")
    def test_artwork_flag_propagated(self, mock_ocr, mock_valid):
        img = Image.new("RGB", (100, 100))
        bubble = {"bbox": (10, 10, 50, 50), "is_artwork": True}
        br = ocr_bubble(img, bubble)
        assert br.is_artwork_text is True

    @patch("kindle.processor.is_valid_japanese", return_value=True)
    @patch("kindle.processor.extract_text_from_region", return_value="テスト")
    def test_regular_bubble_not_artwork(self, mock_ocr, mock_valid):
        img = Image.new("RGB", (100, 100))
        bubble = {"bbox": (10, 10, 50, 50)}
        br = ocr_bubble(img, bubble)
        assert br.is_artwork_text is False


class TestOcrBubble:
    @patch("kindle.processor.is_valid_japanese", return_value=True)
    @patch("kindle.processor.extract_text_from_region", return_value="こんにちは")
    def test_valid_text(self, mock_ocr, mock_valid):
        img = Image.new("RGB", (100, 100))
        bubble = {"bbox": (10, 10, 50, 50)}
        br = ocr_bubble(img, bubble)
        assert br.is_valid
        assert br.ocr_text == "こんにちは"

    @patch("kindle.processor.is_valid_japanese", return_value=False)
    @patch("kindle.processor.extract_text_from_region", return_value="abc")
    def test_invalid_text(self, mock_ocr, mock_valid):
        img = Image.new("RGB", (100, 100))
        bubble = {"bbox": (10, 10, 50, 50)}
        br = ocr_bubble(img, bubble)
        assert not br.is_valid

    @patch("kindle.processor.extract_text_from_region", return_value="")
    def test_empty_text(self, mock_ocr):
        img = Image.new("RGB", (100, 100))
        bubble = {"bbox": (10, 10, 50, 50)}
        br = ocr_bubble(img, bubble)
        assert not br.is_valid


class TestTransformFurigana:
    @patch("kindle.processor.furigana_annotate")
    def test_sets_transformed(self, mock_annotate):
        mock_annotate.return_value = [
            {"text": "今日", "furigana": "きょう", "needs_furigana": True}
        ]
        br = BubbleResult(bbox=(0, 0, 100, 100),
                          ocr_text="今日", is_valid=True)
        transform_furigana(br)
        assert br.transformed is not None
        assert len(br.transformed) == 1

    def test_skips_invalid(self):
        br = BubbleResult(bbox=(0, 0, 100, 100),
                          ocr_text="", is_valid=False)
        transform_furigana(br)
        assert br.transformed is None


class TestTransformTranslate:
    @patch("kindle.processor.translate", return_value="Hello")
    def test_sets_translated(self, mock_translate):
        br = BubbleResult(bbox=(0, 0, 100, 100),
                          ocr_text="こんにちは", is_valid=True)
        transform_translate(br)
        assert br.transformed == "Hello"

    def test_skips_invalid(self):
        br = BubbleResult(bbox=(0, 0, 100, 100),
                          ocr_text="", is_valid=False)
        transform_translate(br)
        assert br.transformed is None
