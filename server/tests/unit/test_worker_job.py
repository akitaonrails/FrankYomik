"""Tests for worker job processing and routing."""

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from kindle.image_utils import encode_image_pil
from pipeline.revision import DEFAULT_PIPELINE_REVISION
from pipeline.translation import PageTranslationResult, TranslationValue
from worker.job import VALID_PIPELINES, ProcessingJob, ProcessingResult, process_job


@dataclass
class BubbleResult:
    bbox: tuple[int, int, int, int]
    ocr_text: str = ""
    is_valid: bool = False
    transformed: object = None
    is_artwork_text: bool = False
    mask: object = None
    source_font_size: int | None = None


def _make_test_image_bytes(width: int = 100, height: int = 80) -> bytes:
    """Create a simple white test image as PNG bytes."""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    return encode_image_pil(img, fmt="PNG")


def _translated_page(request, text: str = "Test") -> PageTranslationResult:
    return PageTranslationResult(tuple(
        TranslationValue(item.id, text) for item in request.items
    ), batch_attempts=1)


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
        assert job.pipeline_revision == DEFAULT_PIPELINE_REVISION

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
        # The exact set, so adding one is a deliberate change here and in the
        # Go handler that validates the same names.
        assert VALID_PIPELINES == {
            "manga_translate",
            "manga_furigana",
            "book_furigana",
            "webtoon",
        }

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
    @patch("worker.job.translate_page")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_bubbles")
    def test_translate_pipeline_returns_png(
        self, mock_detect, mock_ocr, mock_translate
    ):
        mock_translate.side_effect = _translated_page
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

    @patch("worker.job.translate_page")
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

    @patch("worker.job.translate_page")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_bubbles")
    def test_translate_uses_translate(
        self, mock_detect, mock_ocr, mock_translate
    ):
        """manga_translate should call the typed page boundary."""
        mock_translate.side_effect = _translated_page
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

        request = mock_translate.call_args.args[0]
        assert request.target_language == "en"
        assert [item.source_text for item in request.items] == ["テスト"]

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

    @patch("worker.job.translate_page")
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

        # The third result is ineligible and is not sent for translation.
        call_count = [0]
        def mock_ocr_fn(img, bubble):
            call_count[0] += 1
            return BubbleResult(
                bbox=bubble["bbox"], ocr_text="テスト",
                is_valid=call_count[0] <= 2,
            )
        mock_ocr.side_effect = mock_ocr_fn
        mock_translate.side_effect = lambda request: _translated_page(
            request, "English text",
        )

        img_bytes = _make_test_image_bytes()
        job = ProcessingJob(
            job_id="bc-1", pipeline="manga_translate", image_bytes=img_bytes,
        )
        result = process_job(job)

        assert result.bubble_count == 2

    @patch("worker.job.render_cleaned_manga_page")
    @patch("worker.job.translate_page", side_effect=RuntimeError("digest mismatch"))
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_bubbles")
    def test_translation_failure_fails_job_without_result_or_metadata(
        self, mock_detect, mock_ocr, _mock_translate, mock_render,
    ):
        mock_detect.side_effect = lambda page: page.bubbles_raw.append(
            {"bbox": (0, 0, 10, 10)}
        )
        mock_ocr.return_value = BubbleResult(
            bbox=(0, 0, 10, 10), ocr_text="テスト", is_valid=True,
        )

        result = process_job(ProcessingJob(
            job_id="translation-fail",
            pipeline="manga_translate",
            image_bytes=_make_test_image_bytes(),
        ))

        assert result.status == "failed"
        assert "digest mismatch" in result.error
        assert result.image_bytes is None
        assert result.metadata_payload is None
        mock_render.assert_not_called()

    @patch("worker.job.translate_page")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_bubbles")
    def test_translate_passes_target_lang(
        self, mock_detect, mock_ocr, mock_translate
    ):
        """manga_translate with pt-br should pass target_lang to translate()."""
        mock_translate.side_effect = lambda request: _translated_page(
            request, "Teste",
        )
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
        assert mock_translate.call_args.args[0].target_language == "pt-br"

    @patch("worker.job.translate_page")
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
        mock_translate.side_effect = _translated_page

        img_bytes = _make_test_image_bytes()
        job = ProcessingJob(
            job_id="meta-1", pipeline="manga_translate",
            image_bytes=img_bytes, source_hash="a" * 64,
        )
        result = process_job(job)

        meta = result.metadata_payload
        assert meta is not None
        assert meta["schema_version"] == 2
        assert meta["page_id"].startswith("manga_translate:")
        assert meta["pipeline"] == "manga_translate"
        assert meta["pipeline_revision"] == DEFAULT_PIPELINE_REVISION
        assert meta["source_hash"] == "a" * 64
        assert "width" in meta["image"]
        assert "height" in meta["image"]
        assert len(meta["regions"]) == 1

        region = meta["regions"][0]
        assert region["id"].startswith("r_")
        assert region["kind"] == "bubble"
        assert isinstance(region["bbox"], list) and len(region["bbox"]) == 4
        assert region["reading_order"] == 0
        assert region["ocr"]["text"] == "テスト"
        assert region["ocr"]["valid"] is True
        assert region["ocr"]["provenance"]["backend"] == "manga-ocr"
        assert region["ocr"]["provenance"]["revision"] != meta["pipeline_revision"]
        assert region["ocr"]["provenance"]["model"].startswith(
            "kha-white/manga-ocr-base@")
        assert region["ocr"]["provenance"]["config_hash"].startswith("sha256:")
        assert region["translation"]["text"] == "Test"
        assert region["translation"]["target_language"] == "en"
        assert region["detection_provenance"]["backend"] == "rtdetr-v2"
        assert region["detection_provenance"]["revision"] != meta["pipeline_revision"]
        assert region["cleanup"]["method"] == "stroke_clear"
        assert region["render"]["provenance"]["backend"] == "manga-renderer"
        # user dict has only manual_translation — no false_positive/wrong_sfx/undetected
        assert region["user"] == {"manual_translation": ""}
        assert "false_positive" not in region["user"]

    @patch("kindle.processor.estimate_source_vertical_font_size", return_value=22)
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_bubbles")
    def test_furigana_metadata_records_source_font_size(
        self, mock_detect, mock_ocr, mock_estimate
    ):
        def add_bubble(page):
            page.bubbles_raw = [{"bbox": (10, 10, 80, 100)}]
        mock_detect.side_effect = add_bubble
        mock_ocr.return_value = BubbleResult(
            bbox=(10, 10, 80, 100), ocr_text="漢字", is_valid=True,
            transformed=[{"text": "漢字", "furigana": "かんじ",
                          "needs_furigana": True}],
        )

        result = process_job(ProcessingJob(
            job_id="meta-furi", pipeline="manga_furigana",
            image_bytes=_make_test_image_bytes(120, 140), source_hash="a" * 64,
        ))

        assert result.status == "completed"
        assert result.metadata_payload["regions"][0]["source_font_size"] == 22

    @patch("worker.job.translate_page")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_bubbles")
    def test_stable_region_ids_do_not_depend_on_reading_order(
        self, mock_detect, mock_ocr, mock_translate
    ):
        original = [(20, 20, 40, 40), (60, 20, 80, 40)]

        def ocr(_image, bubble):
            return BubbleResult(
                bbox=bubble["bbox"], ocr_text="テスト", is_valid=True)

        mock_ocr.side_effect = ocr
        mock_translate.side_effect = lambda request: _translated_page(
            request, "English",
        )
        image = _make_test_image_bytes(100, 100)

        mock_detect.side_effect = lambda page: setattr(
            page, "bubbles_raw", [{"bbox": bbox} for bbox in original])
        first = process_job(ProcessingJob(
            job_id="stable-1", pipeline="manga_translate", image_bytes=image))

        inserted = [(2, 2, 12, 12), *original]
        mock_detect.side_effect = lambda page: setattr(
            page, "bubbles_raw", [{"bbox": bbox} for bbox in inserted])
        second = process_job(ProcessingJob(
            job_id="stable-2", pipeline="manga_translate", image_bytes=image))

        first_ids = {
            tuple(region["bbox"]): region["id"]
            for region in first.metadata_payload["regions"]
        }
        second_ids = {
            tuple(region["bbox"]): region["id"]
            for region in second.metadata_payload["regions"]
        }
        assert second_ids[original[0]] == first_ids[original[0]]
        assert second_ids[original[1]] == first_ids[original[1]]


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

    @patch("worker.job._webtoon_render")
    @patch("worker.job._webtoon_cleanup")
    @patch("worker.job.translate_page", side_effect=RuntimeError("Ollama down"))
    @patch("worker.job._webtoon_prepare_translation")
    @patch("worker.job._webtoon_ocr")
    @patch("worker.job._webtoon_detect")
    @patch("worker.job._webtoon_load_page")
    def test_translation_failure_fails_job_without_result_or_metadata(
        self, mock_load, _mock_detect, _mock_ocr, _mock_prepare,
        _mock_translate, _mock_cleanup, mock_render,
    ):
        bubble = SimpleNamespace(
            bbox=(10, 10, 60, 60), combined_text="안녕", semantic_id=None,
        )
        mock_load.return_value = SimpleNamespace(
            img_pil=Image.new("RGB", (100, 80), "white"),
            regions=[SimpleNamespace(
                bubble=bubble, is_valid=True, english="", source_text="안녕",
            )],
            sfx_detections=[],
            sfx_translations=[],
            sfx_translation_eligible=[],
            semantic_detections=[],
        )
        _mock_cleanup.side_effect = lambda page: setattr(
            page, "output_img", page.img_pil.copy(),
        )

        result = process_job(ProcessingJob(
            job_id="webtoon-translation-fail",
            pipeline="webtoon",
            image_bytes=_make_test_image_bytes(),
        ))

        assert result.status == "failed"
        assert "Ollama down" in result.error
        assert result.image_bytes is None
        assert result.metadata_payload is None
        mock_render.assert_not_called()

    @patch("worker.job._webtoon_render", return_value=b"rendered")
    @patch("worker.job._webtoon_cleanup")
    @patch("worker.job.translate_page")
    @patch("worker.job._webtoon_prepare_translation")
    @patch("worker.job._webtoon_ocr")
    @patch("worker.job._webtoon_detect")
    @patch("worker.job._webtoon_load_page")
    def test_page_without_eligible_text_completes(
        self, mock_load, _mock_detect, _mock_ocr, _mock_prepare,
        mock_translate, _mock_cleanup, mock_render,
    ):
        mock_load.return_value = SimpleNamespace(
            img_pil=Image.new("RGB", (100, 80), "white"),
            regions=[],
            sfx_detections=[],
            sfx_translations=[],
            sfx_translation_eligible=[],
            semantic_detections=[],
        )
        _mock_cleanup.side_effect = lambda page: setattr(
            page, "output_img", page.img_pil.copy(),
        )

        result = process_job(ProcessingJob(
            job_id="webtoon-no-text",
            pipeline="webtoon",
            image_bytes=_make_test_image_bytes(),
            source_hash="a" * 64,
        ))

        assert result.status == "completed"
        assert result.image_bytes.startswith(b"\x89PNG")
        assert result.metadata_payload is not None
        assert result.metadata_payload["regions"] == []
        mock_translate.assert_not_called()
        mock_render.assert_not_called()


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
        assert result.cacheable is False

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

    @patch("worker.job.render_english")
    @patch("worker.job.extract_bubble_mask_manga", return_value=None)
    def test_rerender_schema_v2_uses_nested_translation_and_user_edit(
        self, _mock_mask, mock_render
    ):
        img_bytes = _make_test_image_bytes(200, 300)
        payload = {
            "schema_version": 2,
            "page_id": "manga_translate:source",
            "pipeline": "manga_translate",
            "pipeline_revision": DEFAULT_PIPELINE_REVISION,
            "source_hash": "b" * 64,
            "image": {"width": 200, "height": 300},
            "regions": [{
                "id": "r_stable",
                "kind": "bubble",
                "bbox": [10, 10, 90, 70],
                "reading_order": 0,
                "ocr": {
                    "text": "原文", "language": "ja", "valid": True,
                    "alternatives": [],
                    "provenance": {"backend": "manga-ocr", "revision": "v1"},
                },
                "translation": {
                    "text": "Original", "target_language": "en",
                    "provenance": {"backend": "translator", "revision": "v1"},
                },
                "user": {"manual_translation": "Corrected text"},
            }],
            "stage_provenance": {},
        }
        result = process_job(ProcessingJob(
            job_id="rr-v2",
            pipeline="manga_translate",
            image_bytes=img_bytes,
            source_hash="b" * 64,
            rerender_from_metadata=True,
            metadata_payload=payload,
        ))

        assert result.status == "completed"
        assert result.metadata_payload is payload
        assert mock_render.call_args.args[2] == "Corrected text"

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

    @patch("worker.job.render_furigana_vertical")
    def test_rerender_furigana_uses_source_font_size(self, mock_render):
        """Furigana rerender should preserve per-bubble source scale caps."""
        img_bytes = _make_test_image_bytes(200, 300)
        segments = [{"text": "漢字", "furigana": "かんじ", "needs_furigana": True}]
        job = ProcessingJob(
            job_id="rr-furi-source",
            pipeline="manga_furigana",
            image_bytes=img_bytes,
            rerender_from_metadata=True,
            metadata_payload={
                "regions": [
                    {
                        "id": "r1",
                        "kind": "bubble",
                        "bbox": [10, 10, 90, 90],
                        "ocr_text": "漢字",
                        "source_font_size": 22,
                        "transformed": {
                            "kind": "furigana_segments",
                            "value": segments,
                        },
                        "user": {"manual_translation": ""},
                    },
                ],
            },
        )

        result = process_job(job)

        assert result.status == "completed"
        assert result.bubble_count == 1
        assert mock_render.call_args.kwargs["source_font_size"] == 22

    @patch("worker.job.render_furigana_vertical")
    def test_rerender_furigana_passes_page_font_limits(self, mock_render):
        """Rerender should use soft page caps for normal dialogue variance."""
        img_bytes = _make_test_image_bytes(240, 320)
        segments = [{"text": "漢字", "furigana": "かんじ", "needs_furigana": True}]
        regions = []
        for idx, source_size in enumerate((20, 22, 45), start=1):
            y = idx * 50
            regions.append({
                "id": f"r{idx}",
                "kind": "bubble",
                "bbox": [10, y, 90, y + 45],
                "ocr_text": "漢字",
                "source_font_size": source_size,
                "transformed": {"kind": "furigana_segments", "value": segments},
                "user": {"manual_translation": ""},
            })
        job = ProcessingJob(
            job_id="rr-furi-page-cap",
            pipeline="manga_furigana",
            image_bytes=img_bytes,
            rerender_from_metadata=True,
            metadata_payload={"regions": regions},
        )

        result = process_job(job)

        assert result.status == "completed"
        assert result.bubble_count == 3
        for call_args in mock_render.call_args_list:
            assert call_args.kwargs["page_font_cap"] == 26
            assert call_args.kwargs["source_outlier_threshold"] == 34
            assert call_args.kwargs["page_font_floor"] == 19


# --- Parallel translation ---


class TestPageTranslation:
    @patch("worker.job.translate_page")
    @patch("worker.job.ocr_bubble")
    @patch("worker.job.detect_page_bubbles")
    def test_multiple_regions_use_one_page_call(
        self, mock_detect, mock_ocr, mock_translate
    ):
        """A normal multi-region page uses one structured provider call."""
        NUM_BUBBLES = 5

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

        mock_translate.side_effect = lambda request: _translated_page(
            request, "English",
        )

        img_bytes = _make_test_image_bytes()
        job = ProcessingJob(
            job_id="par-1", pipeline="manga_translate", image_bytes=img_bytes,
        )

        result = process_job(job)

        assert result.status == "completed"
        assert result.bubble_count == NUM_BUBBLES
        assert mock_translate.call_count == 1
        assert len(mock_translate.call_args.args[0].items) == NUM_BUBBLES
