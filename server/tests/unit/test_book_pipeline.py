"""Unit tests for routing the book pipeline.

The manga pipelines must be unaffected, and a page that is not prose has to be
turned away before it reaches OCR rather than annotated as if it were.
"""

from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw

from kindle.image_utils import encode_image_pil
from worker.job import VALID_PIPELINES, ProcessingJob, process_job


def job(pipeline="book_furigana", img=None, **kwargs):
    image = img if img is not None else Image.new("RGB", (400, 400), (0, 0, 0))
    return ProcessingJob(job_id="j1", pipeline=pipeline,
                         image_bytes=encode_image_pil(image),
                         source_hash="hash", **kwargs)


class TestPipelineRouting:
    def test_book_pipeline_is_accepted(self):
        assert "book_furigana" in VALID_PIPELINES

    def test_manga_and_webtoon_pipelines_are_untouched(self):
        assert {"manga_translate", "manga_furigana", "webtoon"} <= VALID_PIPELINES

    def test_an_unknown_pipeline_still_fails(self):
        result = process_job(job(pipeline="book_translate"))
        assert result.status == "failed"
        assert "Unknown pipeline" in result.error


class TestNonProsePages:
    def test_a_blank_page_is_turned_away(self):
        result = process_job(job())
        assert result.status == "failed"
        assert "prose" in result.error

    def test_manga_like_artwork_is_turned_away(self):
        # Reaching OCR here would scatter furigana across the artwork.
        img = Image.new("RGB", (900, 700), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        for x, y, w, h in [(40, 40, 200, 300), (300, 80, 60, 500),
                           (420, 200, 350, 120), (700, 60, 150, 600)]:
            draw.rectangle([x, y, x + w, y + h], fill=(0, 0, 0))

        result = process_job(job(img=img))

        assert result.status == "failed"
        assert "manga pipeline" in result.error

    def test_the_failure_names_the_pipeline_and_page(self):
        result = process_job(job())
        assert result.pipeline == "book_furigana"
        assert result.source_hash == "hash"


class TestRerender:
    def test_book_pages_cannot_be_rerendered_from_metadata(self):
        # Book pages are annotated in the gutters; there are no editable
        # regions to replay.
        result = process_job(job(rerender_from_metadata=True))
        assert result.status == "failed"
        assert "cannot be re-rendered" in result.error


class TestPageKind:
    """The manga pipelines report what kind of page they were given.

    A book on the wrong pipeline is otherwise invisible until its renders come
    back rearranged, and a client should not have to infer that.
    """

    @staticmethod
    def _prose_page():
        from PIL import ImageFont
        from kindle.config import FONT_JP
        img = Image.new("RGB", (700, 500), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(FONT_JP, 26)
        except OSError:  # pragma: no cover - depends on the host's fonts
            pytest.skip("Japanese font unavailable")
        text = "風が頬を撫で髪を優しく揺らしている今日は良い天気だと思った"
        for column in range(10):
            x = 40 + column * 60
            for row in range(14):
                draw.text((x, 30 + row * 30), text[(column + row) % len(text)],
                          font=font, fill=(210, 210, 210))
        return img

    @patch("worker.job.translate")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_bubbles")
    def test_prose_page_is_reported_as_prose(self, mock_detect, mock_ocr, mock_translate):
        result = process_job(job(pipeline="manga_translate", img=self._prose_page()))
        assert result.status == "completed"
        assert result.page_kind == "prose"

    @patch("worker.job.translate")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_bubbles")
    def test_artwork_is_not_reported_as_prose(self, mock_detect, mock_ocr, mock_translate):
        img = Image.new("RGB", (900, 700), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        for x, y, w, h in [(40, 40, 200, 300), (300, 80, 60, 500), (420, 200, 350, 120)]:
            draw.rectangle([x, y, x + w, y + h], fill=(0, 0, 0))

        result = process_job(job(pipeline="manga_furigana", img=img))

        assert result.status == "completed"
        assert result.page_kind == "artwork"
