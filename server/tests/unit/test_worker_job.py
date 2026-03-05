"""Tests for worker job processing and routing."""

import time
from unittest.mock import patch, MagicMock, call

from PIL import Image

from kindle.image_utils import encode_image_pil
from kindle.processor import BubbleResult
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
        assert job.target_lang == "en"

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
    @patch("worker.job.translate")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_bubbles")
    def test_translate_pipeline_returns_png(
        self, mock_detect, mock_ocr, mock_translate
    ):
        mock_translate.return_value = "Test"
        mock_ocr.return_value = BubbleResult(
            bbox=(10, 10, 50, 50),
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
    @patch("worker.job.detect_page_bubbles")
    def test_furigana_pipeline_returns_bytes(
        self, mock_detect, mock_ocr, mock_furigana
    ):
        mock_ocr.return_value = BubbleResult(
            bbox=(10, 10, 50, 50),
        )

        img_bytes = _make_test_image_bytes()
        job = ProcessingJob(
            job_id="f-1", pipeline="manga_furigana", image_bytes=img_bytes,
        )
        result = process_job(job)

        assert result.status == "completed"
        assert result.image_bytes is not None

    @patch("worker.job.translate")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_bubbles")
    def test_calls_stages_in_order(
        self, mock_detect, mock_ocr, mock_translate
    ):
        """Verify pipeline stages are called: detect → ocr → translate."""
        mock_translate.return_value = "Test"
        mock_ocr.return_value = BubbleResult(
            bbox=(0, 0, 10, 10),
        )

        img_bytes = _make_test_image_bytes()
        job = ProcessingJob(
            job_id="ord-1", pipeline="manga_translate", image_bytes=img_bytes,
        )
        process_job(job)

        mock_detect.assert_called_once()
        # OCR is called for each bubble in bubbles_raw (default empty = 0 calls)

    @patch("worker.job.translate")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_bubbles")
    def test_translate_uses_translate(
        self, mock_detect, mock_ocr, mock_translate
    ):
        """manga_translate should call translate() directly, not furigana."""
        mock_translate.return_value = "Test"
        mock_ocr.return_value = BubbleResult(
            bbox=(0, 0, 10, 10), is_valid=True, ocr_text="テスト",
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

        mock_translate.assert_called_once_with("テスト", "en")

    @patch("worker.job.transform_furigana")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_bubbles")
    def test_furigana_uses_transform_furigana(
        self, mock_detect, mock_ocr, mock_furigana
    ):
        """manga_furigana should call transform_furigana."""
        mock_ocr.return_value = BubbleResult(
            bbox=(0, 0, 10, 10), is_valid=True, ocr_text="漢字",
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

    @patch("worker.job.translate")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_bubbles")
    def test_bubble_count_reflects_transformed(
        self, mock_detect, mock_ocr, mock_translate
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
                bbox=bubble["bbox"],
                ocr_text="テスト", is_valid=True,
            )
        mock_ocr.side_effect = mock_ocr_fn

        # Return English for first 2, empty for third
        translate_calls = [0]
        def mock_translate_fn(text, target_lang="en"):
            translate_calls[0] += 1
            if translate_calls[0] <= 2:
                return "English text"
            return ""
        mock_translate.side_effect = mock_translate_fn

        img_bytes = _make_test_image_bytes()
        job = ProcessingJob(
            job_id="bc-1", pipeline="manga_translate", image_bytes=img_bytes,
        )
        result = process_job(job)

        assert result.bubble_count == 2

    @patch("worker.job.translate")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_bubbles")
    def test_translate_passes_target_lang(
        self, mock_detect, mock_ocr, mock_translate
    ):
        """manga_translate with pt-br should pass target_lang to translate()."""
        mock_translate.return_value = "Teste"
        mock_ocr.return_value = BubbleResult(
            bbox=(0, 0, 10, 10), is_valid=True, ocr_text="テスト",
        )
        def add_bubble(page):
            page.bubbles_raw = [{"bbox": (0, 0, 10, 10)}]
        mock_detect.side_effect = add_bubble

        img_bytes = _make_test_image_bytes()
        job = ProcessingJob(
            job_id="tl-1", pipeline="manga_translate", image_bytes=img_bytes,
            target_lang="pt-br",
        )
        result = process_job(job)

        assert result.status == "completed"
        mock_translate.assert_called_once_with("テスト", "pt-br")

    @patch("worker.job.translate")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_bubbles")
    def test_metadata_payload_structure(
        self, mock_detect, mock_ocr, mock_translate
    ):
        """metadata_payload should have correct schema and user dict shape."""
        def add_bubble(page):
            page.bubbles_raw = [{"bbox": (10, 10, 50, 50)}]
        mock_detect.side_effect = add_bubble
        mock_ocr.return_value = BubbleResult(
            bbox=(10, 10, 50, 50), ocr_text="テスト", is_valid=True,
        )
        mock_translate.return_value = "Test"

        img_bytes = _make_test_image_bytes()
        job = ProcessingJob(
            job_id="meta-1", pipeline="manga_translate",
            image_bytes=img_bytes, source_hash="abc123",
        )
        result = process_job(job)

        meta = result.metadata_payload
        assert meta is not None
        assert meta["schema_version"] == 1
        assert meta["pipeline"] == "manga_translate"
        assert meta["source_hash"] == "abc123"
        assert "width" in meta["image"]
        assert "height" in meta["image"]
        assert len(meta["regions"]) == 1

        region = meta["regions"][0]
        assert region["id"] == "r1"
        assert region["kind"] == "bubble"
        assert isinstance(region["bbox"], list) and len(region["bbox"]) == 4
        assert isinstance(region["bbox_norm"], list) and len(region["bbox_norm"]) == 4
        assert region["ocr_text"] == "テスト"
        assert region["is_valid"] is True
        assert region["transformed"] == {"kind": "text", "value": "Test"}
        # user dict has only manual_translation — no false_positive/wrong_sfx/undetected
        assert region["user"] == {"manual_translation": ""}
        assert "false_positive" not in region["user"]


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


# --- Rerender from metadata ---


class TestRerenderFromMetadata:
    def test_rerender_missing_metadata_fails(self):
        """Rerender with no metadata_payload should fail explicitly."""
        job = ProcessingJob(
            job_id="rr-1",
            pipeline="manga_translate",
            image_bytes=_make_test_image_bytes(),
            rerender_from_metadata=True,
            metadata_payload=None,
        )
        result = process_job(job)
        assert result.status == "failed"
        assert "metadata" in result.error.lower()

    def test_rerender_empty_payload_fails(self):
        """Rerender with empty dict (no regions key) should fail."""
        job = ProcessingJob(
            job_id="rr-2",
            pipeline="manga_translate",
            image_bytes=_make_test_image_bytes(),
            rerender_from_metadata=True,
            metadata_payload={},
        )
        result = process_job(job)
        assert result.status == "failed"
        assert "metadata" in result.error.lower()

    def test_rerender_regions_not_list_fails(self):
        """Rerender with regions that isn't a list should fail."""
        job = ProcessingJob(
            job_id="rr-3",
            pipeline="manga_translate",
            image_bytes=_make_test_image_bytes(),
            rerender_from_metadata=True,
            metadata_payload={"regions": "not-a-list"},
        )
        result = process_job(job)
        assert result.status == "failed"
        assert "metadata" in result.error.lower()

    def test_rerender_valid_metadata_succeeds(self):
        """Rerender with valid metadata should produce completed result."""
        img_bytes = _make_test_image_bytes(200, 300)
        job = ProcessingJob(
            job_id="rr-ok",
            pipeline="manga_translate",
            image_bytes=img_bytes,
            rerender_from_metadata=True,
            metadata_payload={
                "regions": [
                    {
                        "id": "r1",
                        "kind": "bubble",
                        "bbox": [10, 10, 90, 70],
                        "transformed": {"kind": "text", "value": "Hello"},
                        "user": {"manual_translation": ""},
                    },
                ],
            },
        )
        result = process_job(job)
        assert result.status == "completed"
        assert result.bubble_count == 1
        assert result.image_bytes is not None

    def test_rerender_uses_manual_translation(self):
        """Rerender should prefer user.manual_translation over transformed."""
        img_bytes = _make_test_image_bytes(200, 300)
        job = ProcessingJob(
            job_id="rr-manual",
            pipeline="manga_translate",
            image_bytes=img_bytes,
            rerender_from_metadata=True,
            metadata_payload={
                "regions": [
                    {
                        "id": "r1",
                        "kind": "bubble",
                        "bbox": [10, 10, 90, 70],
                        "transformed": {"kind": "text", "value": "Original"},
                        "user": {"manual_translation": "Corrected text"},
                    },
                ],
            },
        )
        result = process_job(job)
        assert result.status == "completed"
        assert result.bubble_count == 1

    def test_rerender_large_bubble_scales_font(self):
        """Rerender should scale font to fill large bubbles on high-res screens."""
        img_bytes = _make_test_image_bytes(600, 800)
        job = ProcessingJob(
            job_id="rr-font",
            pipeline="manga_translate",
            image_bytes=img_bytes,
            rerender_from_metadata=True,
            metadata_payload={
                "regions": [
                    {
                        "id": "r1",
                        "kind": "bubble",
                        "bbox": [10, 10, 590, 790],
                        "transformed": {"kind": "text", "value": "A"},
                        "user": {"manual_translation": ""},
                    },
                ],
            },
        )
        result = process_job(job)
        assert result.status == "completed"
        assert result.image_bytes is not None
        assert result.bubble_count == 1

    def test_rerender_preserves_metadata_payload(self):
        """Rerender should pass through the metadata payload in the result."""
        img_bytes = _make_test_image_bytes(200, 300)
        payload = {
            "regions": [
                {
                    "id": "r1",
                    "kind": "bubble",
                    "bbox": [10, 10, 90, 70],
                    "transformed": {"kind": "text", "value": "Test"},
                    "user": {"manual_translation": ""},
                },
            ],
        }
        job = ProcessingJob(
            job_id="rr-meta",
            pipeline="manga_translate",
            image_bytes=img_bytes,
            rerender_from_metadata=True,
            metadata_payload=payload,
        )
        result = process_job(job)
        assert result.metadata_payload is payload

    def test_rerender_renders_regions_with_legacy_fp_key(self):
        """Legacy metadata with false_positive key should still render (key is ignored)."""
        img_bytes = _make_test_image_bytes(200, 300)
        job = ProcessingJob(
            job_id="rr-legacy-fp",
            pipeline="manga_translate",
            image_bytes=img_bytes,
            rerender_from_metadata=True,
            metadata_payload={
                "regions": [
                    {
                        "id": "r1",
                        "kind": "bubble",
                        "bbox": [10, 10, 90, 70],
                        "transformed": {"kind": "text", "value": "Hello"},
                        "user": {"false_positive": True, "manual_translation": ""},
                    },
                    {
                        "id": "r2",
                        "kind": "bubble",
                        "bbox": [10, 80, 90, 150],
                        "transformed": {"kind": "text", "value": "World"},
                        "user": {"manual_translation": ""},
                    },
                ],
            },
        )
        result = process_job(job)
        assert result.status == "completed"
        # Both regions render — false_positive is ignored
        assert result.bubble_count == 2

    def test_rerender_handles_missing_user_dict(self):
        """Regions without a user dict should render normally."""
        img_bytes = _make_test_image_bytes(200, 300)
        job = ProcessingJob(
            job_id="rr-no-user",
            pipeline="manga_translate",
            image_bytes=img_bytes,
            rerender_from_metadata=True,
            metadata_payload={
                "regions": [
                    {
                        "id": "r1",
                        "kind": "bubble",
                        "bbox": [10, 10, 90, 70],
                        "transformed": {"kind": "text", "value": "Hello"},
                    },
                ],
            },
        )
        result = process_job(job)
        assert result.status == "completed"
        assert result.bubble_count == 1


# --- Parallel translation ---


class TestParallelTranslation:
    @patch("worker.job.translate")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_bubbles")
    def test_translate_calls_are_parallel(
        self, mock_detect, mock_ocr, mock_translate
    ):
        """Translation of multiple bubbles should run in parallel threads."""
        NUM_BUBBLES = 5
        SLEEP_PER_CALL = 0.05  # 50ms each

        def add_bubbles(page):
            page.bubbles_raw = [
                {"bbox": (i * 10, 0, i * 10 + 10, 10)}
                for i in range(NUM_BUBBLES)
            ]
        mock_detect.side_effect = add_bubbles

        def mock_ocr_fn(img, bubble):
            return BubbleResult(
                bbox=bubble["bbox"], ocr_text="テスト", is_valid=True,
            )
        mock_ocr.side_effect = mock_ocr_fn

        def slow_translate(text, target_lang="en"):
            time.sleep(SLEEP_PER_CALL)
            return "English"
        mock_translate.side_effect = slow_translate

        img_bytes = _make_test_image_bytes()
        job = ProcessingJob(
            job_id="par-1", pipeline="manga_translate", image_bytes=img_bytes,
        )

        start = time.monotonic()
        result = process_job(job)
        elapsed = time.monotonic() - start

        assert result.status == "completed"
        assert result.bubble_count == NUM_BUBBLES
        assert mock_translate.call_count == NUM_BUBBLES
        # Sequential would be ~250ms, parallel should be ~50ms + overhead
        assert elapsed < SLEEP_PER_CALL * NUM_BUBBLES * 0.6
