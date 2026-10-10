"""Production stage-boundary contract tests."""

from typing import get_type_hints

from pipeline import PipelineRuntime
from pipeline.protocols import (
    MangaCleanupStage,
    MangaOcrStage,
    MangaRenderStage,
    PageCleanupStage,
    PageDetectionStage,
    PageOcrStage,
    PageRenderStage,
    WebtoonTranslationStage,
)
from worker.job import PipelineDependencies, production_dependencies


def test_production_orchestration_bundle_implements_runtime_protocol():
    assert isinstance(production_dependencies(), PipelineRuntime)


def test_production_stage_callables_are_explicit_and_callable():
    runtime = production_dependencies()
    expected_types = {
        "detect_manga": PageDetectionStage,
        "ocr_manga": MangaOcrStage,
        "cleanup_manga": MangaCleanupStage,
        "render_manga": MangaRenderStage,
        "detect_webtoon": PageDetectionStage,
        "ocr_webtoon": PageOcrStage,
        "translate_webtoon": WebtoonTranslationStage,
        "cleanup_webtoon": PageCleanupStage,
        "render_webtoon": PageRenderStage,
    }
    annotations = get_type_hints(PipelineDependencies)

    for name, expected_type in expected_types.items():
        assert annotations[name] is expected_type, name
        assert callable(getattr(runtime, name)), name
