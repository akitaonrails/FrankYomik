"""Typed stage interfaces implemented by concrete pipeline backends."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from .document import Provenance
from .translation import PageTranslationRequest, PageTranslationResult


class PageDetectionStage(Protocol):
    """Detect geometry on a compatibility page object."""

    def __call__(self, page: object) -> None: ...


class PageOcrStage(Protocol):
    """Populate recognized text for previously detected geometry."""

    def __call__(self, page: object) -> None: ...


class MangaOcrStage(Protocol):
    def __call__(self, image: object,
                 detection: Mapping[str, object]) -> object: ...


class MangaCleanupStage(Protocol):
    def __call__(self, page: object, mode: object) -> None: ...


class MangaRenderStage(Protocol):
    def __call__(self, page: object, mode: object) -> bytes: ...


class WebtoonTranslationStage(Protocol):
    def __call__(self, page: object, *, parallel: bool,
                 target_lang: str) -> None: ...


class PageTranslationStage(Protocol):
    def __call__(
        self, request: PageTranslationRequest,
    ) -> PageTranslationResult: ...


class PageCleanupStage(Protocol):
    def __call__(self, page: object) -> None: ...


class PageRenderStage(Protocol):
    def __call__(self, page: object) -> bytes: ...


@runtime_checkable
class PipelineRuntime(Protocol):
    """Concrete stage boundaries required by worker orchestration.

    Callables use existing page objects during the compatibility migration;
    their results are normalized into PageDocument before leaving the worker.
    """

    decode_image: Callable[..., Any]
    encode_image: Callable[..., bytes]
    analyze_layout: Callable[..., Any]
    read_columns: Callable[..., Any]
    render_book: Callable[..., Any]
    extract_manga_mask: Callable[..., Any]
    annotate: Callable[..., Any]
    clear_region: Callable[..., Any]
    clear_strokes: Callable[..., Any]
    ocr_prose: Callable[..., Any]
    create_manga_page: Callable[..., Any]
    detect_manga: PageDetectionStage
    ocr_manga: MangaOcrStage
    cleanup_manga: MangaCleanupStage
    render_manga: MangaRenderStage
    transform_furigana: Callable[..., Any]
    translate_text: Callable[..., str]
    translate_page: PageTranslationStage
    furigana_font_limits: Callable[..., Any]
    estimate_source_font: Callable[..., Any]
    render_translation: Callable[..., Any]
    render_artwork_translation: Callable[..., Any]
    render_furigana: Callable[..., Any]
    pipeline_mode: Callable[..., Any]
    config_value: Callable[..., Any]
    create_webtoon_page: Callable[..., Any]
    detect_webtoon: PageDetectionStage
    ocr_webtoon: PageOcrStage
    translate_webtoon: WebtoonTranslationStage
    prepare_webtoon_translation: Callable[[object], None]
    cleanup_webtoon: PageCleanupStage
    render_webtoon: PageRenderStage
    render_webtoon_document_region: Callable[..., Any]
    render_webtoon_document_sfx: Callable[..., Any]
    webtoon_document_geometry: Callable[..., Any]
    provenance_for: Callable[[str, str, str], Provenance] | None
    language_for: Callable[[str], str] | None
    cleanup_for: Callable[[str, str], str | None] | None
