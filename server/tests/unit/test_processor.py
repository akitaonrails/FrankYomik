"""Unit tests for processor pipeline stages (mocked dependencies)."""

from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image

from kindle.processor import (
    BubbleResult,
    PageResult,
    PipelineMode,
    _render_page_image,
    cleanup_page,
    load_page,
    ocr_bubble,
    render_cleaned_page_to_bytes,
    transform_furigana,
    transform_translate,
)
from kindle.inpainter import MangaCleanupOutcome


@pytest.fixture(autouse=True)
def _keep_existing_stage_tests_on_primary_ocr(monkeypatch):
    monkeypatch.setattr("kindle.processor.HAYAI_ENABLED", False)


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

    @patch("kindle.processor.is_valid_japanese", return_value=True)
    @patch(
        "kindle.processor.extract_text_from_region",
        side_effect=["日本語。。。。。。。。。。。。", "別の読み"],
    )
    def test_disabled_hayai_preserves_legacy_stripped_length_trigger(
        self, mock_ocr, _mock_valid,
    ):
        img = Image.new("RGB", (100, 100))
        bubble = {"bbox": (10, 10, 50, 50)}

        result = ocr_bubble(img, bubble)

        assert result.is_valid is True
        assert result.ocr_text == "日本語。。。。。。。。。。。。"
        assert mock_ocr.call_count == 1


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


class TestRenderPageImage:
    @patch("kindle.processor.clear_text_strokes")
    @patch("kindle.processor.render_furigana_vertical")
    def test_furigana_passes_original_image_for_layout(self, mock_render, mock_clear):
        source = Image.new("RGB", (100, 140), "gray")
        page = PageResult(
            image_path="/tmp/page.png",
            name="page",
            img_pil=source,
            output_img=source.copy(),
            bubble_results=[
                BubbleResult(
                    bbox=(20, 20, 70, 100),
                    ocr_text="空腹",
                    is_valid=True,
                    transformed=[{
                        "text": "空腹",
                        "furigana": "くうふく",
                        "needs_furigana": True,
                    }],
                    source_font_size=20,
                )
            ],
        )

        _render_page_image(page, PipelineMode.FURIGANA)

        assert mock_render.call_args.kwargs["layout_img"] is source

    def test_cleanup_and_render_are_separate_production_stages(self):
        source = Image.new("RGB", (100, 140), "white")
        page = PageResult(
            image_path="",
            name="page",
            img_pil=source,
            bubble_results=[BubbleResult(
                bbox=(20, 20, 70, 100),
                ocr_text="こんにちは",
                is_valid=True,
                transformed="Hello",
            )],
        )

        with patch("kindle.processor.clear_text_strokes") as clear:
            cleanup_page(page, PipelineMode.TRANSLATE)
        clear.assert_called_once()

        with (
            patch("kindle.processor.clear_text_strokes") as clear_again,
            patch("kindle.processor.render_english") as render,
        ):
            output = render_cleaned_page_to_bytes(page, PipelineMode.TRANSLATE)

        assert output.startswith(b"\x89PNG")
        clear_again.assert_not_called()
        render.assert_called_once()

    def test_failed_artwork_inpaint_is_not_credited(self):
        source = Image.new("RGB", (100, 140), "white")
        region = BubbleResult(
            bbox=(20, 20, 70, 100), ocr_text="ドン", is_valid=True,
            transformed="BOOM", is_artwork_text=True,
        )
        page = PageResult(
            image_path="", name="page", img_pil=source,
            bubble_results=[region],
        )
        outcome = MangaCleanupOutcome(
            source, applied=False, degraded=True, reason="backend_failure",
        )
        with (
            patch("kindle.processor.MANGA_INPAINT_ENABLED", True),
            patch("kindle.inpainter.cleanup_region", return_value=outcome),
        ):
            cleanup_page(page, PipelineMode.TRANSLATE)
        assert page.render_plan.artwork_inpainted == frozenset()
        assert region.cleanup_method is None
        assert region.cleanup_degraded is True

    def test_disabled_artwork_model_moves_exact_fill_into_cleanup_stage(self):
        source = Image.new("RGB", (100, 140), (12, 34, 56))
        region = BubbleResult(
            bbox=(20, 20, 70, 100), ocr_text="ドン", is_valid=True,
            transformed="BOOM", is_artwork_text=True,
        )
        page = PageResult(
            image_path="", name="page", img_pil=source,
            bubble_results=[region],
        )

        with patch("kindle.processor.MANGA_INPAINT_ENABLED", False):
            cleanup_page(page, PipelineMode.TRANSLATE)

        assert region.cleanup_method == "region_fill"
        assert np.all(region.cleanup_mask[20:100, 20:70] == 255)
        assert np.count_nonzero(region.cleanup_mask) == 50 * 80
        assert page.output_img.getpixel((20, 20)) == (255, 255, 255)
        assert 0 in page.render_plan.artwork_inpainted
