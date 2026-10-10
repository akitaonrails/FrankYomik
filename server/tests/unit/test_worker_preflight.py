"""Worker startup preflight and capability contract tests."""

import pytest

from worker.preflight import (
    ALL_PIPELINES,
    ModelCheck,
    PreflightError,
    preflight_worker,
    production_model_checks,
)


def test_preflight_runs_every_required_check_and_reports_revision():
    calls = []
    checks = tuple(
        ModelCheck(name, frozenset(ALL_PIPELINES), lambda name=name: calls.append(name))
        for name in ("detector", "ocr", "translator", "cleanup")
    )

    report = preflight_worker(
        ALL_PIPELINES, checks=checks, pipeline_revision="pipeline-test",
    )

    assert calls == ["detector", "ocr", "translator", "cleanup"]
    assert report.pipelines == ALL_PIPELINES
    assert report.pipeline_revision == "pipeline-test"
    assert report.models == tuple(calls)
    assert report.heartbeat_payload() == {
        "schema_version": 1,
        "pipelines": list(ALL_PIPELINES),
        "pipeline_revision": "pipeline-test",
        "models": list(calls),
        "preflight": "passed",
    }


def test_preflight_failure_is_named_and_stops_before_later_checks():
    calls = []

    def fail():
        calls.append("broken")
        raise RuntimeError("weights digest mismatch")

    checks = (
        ModelCheck("broken-model", frozenset(ALL_PIPELINES), fail),
        ModelCheck(
            "must-not-run", frozenset(ALL_PIPELINES),
            lambda: calls.append("must-not-run"),
        ),
    )

    with pytest.raises(PreflightError, match="broken-model.*digest mismatch"):
        preflight_worker(
            ALL_PIPELINES, checks=checks, pipeline_revision="pipeline-test",
        )
    assert calls == ["broken"]


def test_partial_capability_set_cannot_enter_shared_queue():
    with pytest.raises(PreflightError, match="every pipeline"):
        preflight_worker(
            ("webtoon",), checks=(), pipeline_revision="pipeline-test",
        )


def test_production_checks_cover_every_pipeline_requirement():
    checks = production_model_checks()
    assert {check.name for check in checks} == {
        "rtdetr-v2", "manga-ocr", "easyocr-ja", "easyocr-ko",
        "unidic-lite", "ollama-qwen3", "fonts", "simple-lama",
    }
    for pipeline in ALL_PIPELINES:
        covered = {check.name for check in checks if pipeline in check.pipelines}
        assert "manga-ocr" in covered if pipeline != "webtoon" else (
            "easyocr-ko" in covered
        )
        assert "fonts" in covered
    assert {"rtdetr-v2", "easyocr-ja", "ollama-qwen3", "simple-lama"} <= {
        check.name for check in checks if "manga_translate" in check.pipelines
    }
    assert {"rtdetr-v2", "easyocr-ko", "ollama-qwen3", "simple-lama"} <= {
        check.name for check in checks if "webtoon" in check.pipelines
    }
    assert "unidic-lite" in {
        check.name for check in checks if "book_furigana" in check.pipelines
    }
