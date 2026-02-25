"""End-to-end pipeline integration test.

Marked slow — skipped by default, run with: pytest -m slow
"""

import os

import pytest

from tests.conftest import DOCS_DIR


@pytest.mark.slow
class TestFullPipeline:
    """Smoke test: run actual pipeline on one page per mode."""

    def test_furigana_pipeline(self, tmp_path):
        from pipeline.processor import (
            PipelineMode, load_page, detect_page_bubbles,
            ocr_bubble, transform_furigana, render_page,
        )
        img_path = os.path.join(DOCS_DIR, "adult.png")
        if not os.path.exists(img_path):
            pytest.skip("Test image not found")

        page = load_page(img_path)
        detect_page_bubbles(page)
        assert len(page.bubbles_raw) > 0

        page.bubble_results = [
            ocr_bubble(page.img_pil, b) for b in page.bubbles_raw
        ]
        for br in page.bubble_results:
            if br.is_valid:
                transform_furigana(br)

        out_dir = str(tmp_path)
        render_page(page, PipelineMode.FURIGANA, out_dir)
        assert os.path.exists(os.path.join(out_dir, "adult-furigana.png"))

    def test_translate_pipeline_mocked(self, tmp_path):
        """Run translate pipeline with mocked translation (no Ollama needed)."""
        from unittest.mock import patch
        from pipeline.processor import (
            PipelineMode, load_page, detect_page_bubbles,
            ocr_bubble, transform_translate, render_page,
        )
        img_path = os.path.join(DOCS_DIR, "shounen.png")
        if not os.path.exists(img_path):
            pytest.skip("Test image not found")

        page = load_page(img_path)
        detect_page_bubbles(page)
        assert len(page.bubbles_raw) > 0

        page.bubble_results = [
            ocr_bubble(page.img_pil, b) for b in page.bubbles_raw
        ]

        with patch("pipeline.processor.translate", return_value="Test translation"):
            for br in page.bubble_results:
                if br.is_valid:
                    transform_translate(br)

        out_dir = str(tmp_path)
        render_page(page, PipelineMode.TRANSLATE, out_dir)
        assert os.path.exists(os.path.join(out_dir, "shounen-en.png"))
