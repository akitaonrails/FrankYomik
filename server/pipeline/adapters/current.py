"""Lazy compatibility adapters for the current manga and webtoon backends."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pipeline.document import Provenance
from pipeline.protocols import (
    MangaCleanupStage,
    MangaOcrStage,
    MangaRenderStage,
    PageCleanupStage,
    PageDetectionStage,
    PageOcrStage,
    PageRenderStage,
    PageTranslationStage,
    WebtoonTranslationStage,
)


# These small adapters preserve the current backend behavior while keeping all
# model and image implementation imports out of orchestration module loading.
# The injected dependency bundle below is the only path used by process_job.
def decode_image_bytes(data):
    from kindle.image_utils import decode_image_bytes as implementation
    return implementation(data)


def encode_image_pil(image):
    from kindle.image_utils import encode_image_pil as implementation
    return implementation(image)


def analyze_book_layout(image):
    from kindle.book_layout import analyze as implementation
    return implementation(image)


def read_book_columns(*args, **kwargs):
    from kindle.book_processor import read_columns as implementation
    return implementation(*args, **kwargs)


def render_book_furigana(*args, **kwargs):
    from kindle.book_renderer import render as implementation
    return implementation(*args, **kwargs)


def extract_bubble_mask_manga(*args, **kwargs):
    from kindle.bubble_detector import extract_bubble_mask_manga as implementation
    return implementation(*args, **kwargs)


def furigana_annotate(text):
    from kindle.furigana import annotate as implementation
    return implementation(text)


def clear_text_in_region(*args, **kwargs):
    from kindle.image_utils import clear_text_in_region as implementation
    return implementation(*args, **kwargs)


def clear_text_strokes(*args, **kwargs):
    from kindle.image_utils import clear_text_strokes as implementation
    return implementation(*args, **kwargs)


def ocr_image(image):
    from kindle.ocr import extract_text as implementation
    return implementation(image)


def detect_page_bubbles(page):
    from kindle.processor import detect_page_bubbles as implementation
    return implementation(page)


def load_page_from_memory(*args, **kwargs):
    from kindle.processor import load_page_from_memory as implementation
    return implementation(*args, **kwargs)


def ocr_bubble(*args, **kwargs):
    from kindle.processor import ocr_bubble as implementation
    return implementation(*args, **kwargs)


def render_page_to_bytes(*args, **kwargs):
    from kindle.processor import render_page_to_bytes as implementation
    return implementation(*args, **kwargs)


def cleanup_manga_page(*args, **kwargs):
    from kindle.processor import cleanup_page as implementation
    return implementation(*args, **kwargs)


def render_cleaned_manga_page(*args, **kwargs):
    from kindle.processor import render_cleaned_page_to_bytes as implementation
    return implementation(*args, **kwargs)


def transform_furigana(result):
    from kindle.processor import transform_furigana as implementation
    return implementation(result)


def translate(*args, **kwargs):
    from kindle.translator import translate as implementation
    return implementation(*args, **kwargs)


def translate_page(*args, **kwargs):
    from kindle.translator import translate_page as implementation
    return implementation(*args, **kwargs)


def compute_furigana_page_font_limits(values):
    from kindle.text_renderer import compute_furigana_page_font_limits as implementation
    return implementation(values)


def estimate_source_vertical_font_size(*args, **kwargs):
    from kindle.text_renderer import (
        estimate_source_vertical_font_size as implementation,
    )
    return implementation(*args, **kwargs)


def render_english(*args, **kwargs):
    from kindle.text_renderer import render_english as implementation
    return implementation(*args, **kwargs)


def render_english_artwork(*args, **kwargs):
    from kindle.text_renderer import render_english_on_artwork as implementation
    return implementation(*args, **kwargs)


def render_furigana_vertical(*args, **kwargs):
    from kindle.text_renderer import render_furigana_vertical as implementation
    return implementation(*args, **kwargs)


def _pipeline_mode(name: str):
    from kindle.processor import PipelineMode
    return PipelineMode(name)


def _config_value(name: str):
    from kindle import config
    return getattr(config, name)


def _webtoon_load_page(*args, **kwargs):
    from webtoon.processor import load_page_from_memory as implementation
    return implementation(*args, **kwargs)


def _webtoon_detect(page):
    from webtoon.processor import detect_bubbles_rtdetr as implementation
    return implementation(page)


def _webtoon_ocr(page):
    from webtoon.processor import ocr_detected_regions as implementation
    return implementation(page)


def _webtoon_translate(*args, **kwargs):
    from webtoon.processor import validate_and_translate as implementation
    return implementation(*args, **kwargs)


def _webtoon_prepare_translation(page):
    from webtoon.processor import prepare_translation_regions as implementation
    return implementation(page)


def _webtoon_render(*args, **kwargs):
    from webtoon.processor import render_cleaned_page_to_bytes as implementation
    return implementation(*args, **kwargs)


def _webtoon_render_document_region(*args, **kwargs):
    from webtoon.processor import render_document_region as implementation
    return implementation(*args, **kwargs)


def _webtoon_render_document_sfx(*args, **kwargs):
    from webtoon.processor import render_document_sfx as implementation
    return implementation(*args, **kwargs)


def _webtoon_document_geometry(*args, **kwargs):
    from webtoon.processor import document_render_geometry as implementation
    return implementation(*args, **kwargs)


def _webtoon_cleanup(page):
    from webtoon.processor import cleanup_page as implementation
    return implementation(page)


@dataclass(frozen=True)
class PipelineDependencies:
    """Injectable implementation of every orchestration boundary."""

    decode_image: Callable = decode_image_bytes
    encode_image: Callable = encode_image_pil
    analyze_layout: Callable = analyze_book_layout
    read_columns: Callable = read_book_columns
    render_book: Callable = render_book_furigana
    extract_manga_mask: Callable = extract_bubble_mask_manga
    annotate: Callable = furigana_annotate
    clear_region: Callable = clear_text_in_region
    clear_strokes: Callable = clear_text_strokes
    ocr_prose: Callable = ocr_image
    create_manga_page: Callable = load_page_from_memory
    detect_manga: PageDetectionStage = detect_page_bubbles
    ocr_manga: MangaOcrStage = ocr_bubble
    cleanup_manga: MangaCleanupStage = cleanup_manga_page
    render_manga: MangaRenderStage = render_cleaned_manga_page
    transform_furigana: Callable = transform_furigana
    translate_text: Callable = translate
    translate_page: PageTranslationStage = translate_page
    furigana_font_limits: Callable = compute_furigana_page_font_limits
    estimate_source_font: Callable = estimate_source_vertical_font_size
    render_translation: Callable = render_english
    render_artwork_translation: Callable = render_english_artwork
    render_furigana: Callable = render_furigana_vertical
    pipeline_mode: Callable = _pipeline_mode
    config_value: Callable = _config_value
    create_webtoon_page: Callable = _webtoon_load_page
    detect_webtoon: PageDetectionStage = _webtoon_detect
    ocr_webtoon: PageOcrStage = _webtoon_ocr
    translate_webtoon: WebtoonTranslationStage = _webtoon_translate
    prepare_webtoon_translation: Callable = _webtoon_prepare_translation
    cleanup_webtoon: PageCleanupStage = _webtoon_cleanup
    render_webtoon: PageRenderStage = _webtoon_render
    render_webtoon_document_region: Callable = _webtoon_render_document_region
    render_webtoon_document_sfx: Callable = _webtoon_render_document_sfx
    webtoon_document_geometry: Callable = _webtoon_document_geometry
    provenance_for: Callable[[str, str, str], Provenance] | None = None
    language_for: Callable[[str], str] | None = None
    cleanup_for: Callable[[str, str], str | None] | None = None
