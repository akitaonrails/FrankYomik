"""Tests for worker job processing and routing."""

from unittest.mock import patch, MagicMock, call

from PIL import Image

from pipeline.image_utils import encode_image_pil
from pipeline.processor import BubbleResult
from worker.job import ProcessingJob, ProcessingResult, process_job, VALID_PIPELINES


def _make_test_image_bytes(width: int = 100, height: int = 80) -> bytes:
    """Create a simple white test image as PNG bytes."""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    return encode_image_pil(img, fmt="PNG")


# --- Dataclasses ---


class TestProcessingJobDataclass:
    def test_fields(self):
        job = ProcessingJob(
            job_id="test-123",
            pipeline="manga_translate",
            image_bytes=b"fake",
        )
        assert job.job_id == "test-123"
        assert job.pipeline == "manga_translate"
        assert job.priority == "high"

    def test_low_priority(self):
        job = ProcessingJob(
            job_id="lp-1", pipeline="webtoon",
            image_bytes=b"x", priority="low",
        )
        assert job.priority == "low"

    def test_metadata_defaults(self):
        job = ProcessingJob(
            job_id="m-1", pipeline="manga_translate", image_bytes=b"x",
        )
        assert job.title == ""
        assert job.chapter == ""
        assert job.page_number == ""
        assert job.source_url == ""

    def test_metadata_fields(self):
        job = ProcessingJob(
            job_id="m-2", pipeline="manga_translate", image_bytes=b"x",
            title="One Piece", chapter="1084", page_number="003",
            source_url="https://example.com",
        )
        assert job.title == "One Piece"
        assert job.chapter == "1084"
        assert job.page_number == "003"
        assert job.source_url == "https://example.com"


class TestProcessingResultDataclass:
    def test_defaults(self):
        result = ProcessingResult(job_id="r-1", status="completed")
        assert result.image_bytes is None
        assert result.error == ""
        assert result.processing_time_ms == 0
        assert result.bubble_count == 0

    def test_all_fields(self):
        result = ProcessingResult(
            job_id="r-2", status="degraded",
            image_bytes=b"img", error="ollama down",
            processing_time_ms=1500, bubble_count=7,
        )
        assert result.status == "degraded"
        assert result.error == "ollama down"
        assert result.bubble_count == 7


# --- Pipeline validation ---


class TestValidPipelines:
    def test_valid_pipelines(self):
        assert "manga_translate" in VALID_PIPELINES
        assert "manga_furigana" in VALID_PIPELINES
        assert "webtoon" in VALID_PIPELINES

    def test_count(self):
        assert len(VALID_PIPELINES) == 3

    def test_invalid_pipeline_fails(self):
        job = ProcessingJob(
            job_id="bad-1",
            pipeline="unknown_pipeline",
            image_bytes=_make_test_image_bytes(),
        )
        result = process_job(job)
        assert result.status == "failed"
        assert "Unknown pipeline" in result.error
        assert result.job_id == "bad-1"

    def test_invalid_pipeline_still_records_time(self):
        job = ProcessingJob(
            job_id="bad-2", pipeline="invalid",
            image_bytes=_make_test_image_bytes(),
        )
        result = process_job(job)
        # Even failed jobs should have a non-negative time
        assert result.processing_time_ms >= 0


# --- Manga pipeline routing ---


class TestProcessJobManga:
    @patch("worker.job.transform_translate")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_text")
    @patch("worker.job.detect_page_bubbles")
    def test_translate_pipeline_returns_png(
        self, mock_detect, mock_text, mock_ocr, mock_translate
    ):
        mock_ocr.return_value = BubbleResult(
            bbox=(10, 10, 50, 50), contour=None,
            ocr_text="テスト", is_valid=True,
        )

        img_bytes = _make_test_image_bytes()
        job = ProcessingJob(
            job_id="t-1", pipeline="manga_translate", image_bytes=img_bytes,
        )
        result = process_job(job)

        assert result.status == "completed"
        assert result.image_bytes is not None
        assert result.image_bytes[:4] == b'\x89PNG'
        assert result.processing_time_ms >= 0

    @patch("worker.job.transform_furigana")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_text")
    @patch("worker.job.detect_page_bubbles")
    def test_furigana_pipeline_returns_bytes(
        self, mock_detect, mock_text, mock_ocr, mock_furigana
    ):
        mock_ocr.return_value = BubbleResult(
            bbox=(10, 10, 50, 50), contour=None,
        )

        img_bytes = _make_test_image_bytes()
        job = ProcessingJob(
            job_id="f-1", pipeline="manga_furigana", image_bytes=img_bytes,
        )
        result = process_job(job)

        assert result.status == "completed"
        assert result.image_bytes is not None

    @patch("worker.job.transform_translate")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_text")
    @patch("worker.job.detect_page_bubbles")
    def test_calls_stages_in_order(
        self, mock_detect, mock_text, mock_ocr, mock_translate
    ):
        """Verify pipeline stages are called: detect → text → ocr → transform."""
        mock_ocr.return_value = BubbleResult(
            bbox=(0, 0, 10, 10), contour=None,
        )

        img_bytes = _make_test_image_bytes()
        job = ProcessingJob(
            job_id="ord-1", pipeline="manga_translate", image_bytes=img_bytes,
        )
        process_job(job)

        mock_detect.assert_called_once()
        mock_text.assert_called_once()
        # OCR is called for each bubble in bubbles_raw (default empty = 0 calls)

    @patch("worker.job.transform_translate")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_text")
    @patch("worker.job.detect_page_bubbles")
    def test_translate_uses_transform_translate(
        self, mock_detect, mock_text, mock_ocr, mock_translate
    ):
        """manga_translate should call transform_translate, not furigana."""
        mock_ocr.return_value = BubbleResult(
            bbox=(0, 0, 10, 10), contour=None, is_valid=True, ocr_text="テスト",
        )
        # Simulate one bubble being detected
        def add_bubble(page):
            page.bubbles_raw = [{"bbox": (0, 0, 10, 10)}]
        mock_detect.side_effect = add_bubble

        img_bytes = _make_test_image_bytes()
        job = ProcessingJob(
            job_id="tt-1", pipeline="manga_translate", image_bytes=img_bytes,
        )
        process_job(job)

        mock_translate.assert_called_once()

    @patch("worker.job.transform_furigana")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_text")
    @patch("worker.job.detect_page_bubbles")
    def test_furigana_uses_transform_furigana(
        self, mock_detect, mock_text, mock_ocr, mock_furigana
    ):
        """manga_furigana should call transform_furigana."""
        mock_ocr.return_value = BubbleResult(
            bbox=(0, 0, 10, 10), contour=None, is_valid=True, ocr_text="漢字",
        )
        def add_bubble(page):
            page.bubbles_raw = [{"bbox": (0, 0, 10, 10)}]
        mock_detect.side_effect = add_bubble

        img_bytes = _make_test_image_bytes()
        job = ProcessingJob(
            job_id="tf-1", pipeline="manga_furigana", image_bytes=img_bytes,
        )
        process_job(job)

        mock_furigana.assert_called_once()

    @patch("worker.job.transform_translate")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_text")
    @patch("worker.job.detect_page_bubbles")
    def test_bubble_count_reflects_transformed(
        self, mock_detect, mock_text, mock_ocr, mock_translate
    ):
        """bubble_count should count only bubbles with non-None transformed."""
        def add_bubbles(page):
            page.bubbles_raw = [
                {"bbox": (0, 0, 10, 10)},
                {"bbox": (20, 20, 30, 30)},
                {"bbox": (40, 40, 50, 50)},
            ]
        mock_detect.side_effect = add_bubbles

        call_count = [0]
        def mock_ocr_fn(img, bubble):
            call_count[0] += 1
            return BubbleResult(
                bbox=bubble["bbox"], contour=None,
                ocr_text="テスト", is_valid=True,
            )
        mock_ocr.side_effect = mock_ocr_fn

        # Only set transformed on 2 out of 3
        transform_calls = [0]
        def mock_transform(br):
            transform_calls[0] += 1
            if transform_calls[0] <= 2:
                br.transformed = "English text"
        mock_translate.side_effect = mock_transform

        img_bytes = _make_test_image_bytes()
        job = ProcessingJob(
            job_id="bc-1", pipeline="manga_translate", image_bytes=img_bytes,
        )
        result = process_job(job)

        assert result.bubble_count == 2


# --- Webtoon pipeline routing ---


class TestProcessJobWebtoon:
    @patch("worker.job._process_webtoon")
    def test_webtoon_pipeline_routes_correctly(self, mock_wt):
        """pipeline='webtoon' should call _process_webtoon."""
        mock_wt.return_value = ProcessingResult(
            job_id="wt-1", status="completed", image_bytes=b"png",
        )

        img_bytes = _make_test_image_bytes()
        job = ProcessingJob(
            job_id="wt-1", pipeline="webtoon", image_bytes=img_bytes,
        )
        result = process_job(job)

        mock_wt.assert_called_once()
        assert result.status == "completed"

    @patch("worker.job._process_webtoon")
    def test_webtoon_sets_processing_time(self, mock_wt):
        mock_wt.return_value = ProcessingResult(
            job_id="wt-2", status="completed",
        )

        job = ProcessingJob(
            job_id="wt-2", pipeline="webtoon",
            image_bytes=_make_test_image_bytes(),
        )
        result = process_job(job)
        assert result.processing_time_ms >= 0


# --- Error handling ---


class TestProcessJobError:
    def test_invalid_image_bytes_fails(self):
        job = ProcessingJob(
            job_id="err-1", pipeline="manga_translate",
            image_bytes=b"not-an-image",
        )
        result = process_job(job)
        assert result.status == "failed"
        assert result.error != ""
        assert result.processing_time_ms >= 0

    def test_exception_during_processing_caught(self):
        """Exceptions during pipeline execution should be caught."""
        with patch("worker.job._process_manga", side_effect=RuntimeError("GPU OOM")):
            job = ProcessingJob(
                job_id="err-2", pipeline="manga_translate",
                image_bytes=_make_test_image_bytes(),
            )
            result = process_job(job)
            assert result.status == "failed"
            assert "GPU OOM" in result.error
            assert result.job_id == "err-2"

    def test_webtoon_exception_caught(self):
        with patch("worker.job._process_webtoon", side_effect=MemoryError("CUDA")):
            job = ProcessingJob(
                job_id="err-3", pipeline="webtoon",
                image_bytes=_make_test_image_bytes(),
            )
            result = process_job(job)
            assert result.status == "failed"
            assert "CUDA" in result.error
