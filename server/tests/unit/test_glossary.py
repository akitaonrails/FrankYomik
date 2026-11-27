"""Tests for per-series furigana reading overrides and translation glossaries."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from kindle.furigana import _READING_OVERRIDES, annotate
from kindle.processor import BubbleResult, MangaRenderPlan, transform_furigana
from kindle.translator import _translate_verified as translate_ja_verified
from pipeline.glossary import (
    EMPTY_SERIES_GLOSSARY,
    MAX_READING_OVERRIDES,
    parse_glossaries,
)
from pipeline.translation import MAX_TERMS, PageTranslationResult, TranslationValue
from webtoon.translator import _translate_verified as translate_ko_verified
from worker.job import ProcessingJob, process_job, production_dependencies


def _png_bytes(width: int = 80, height: int = 80) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


class TestParseGlossaries:
    def test_empty_or_none_returns_empty_catalog(self):
        assert parse_glossaries(None).for_series("Anything") is EMPTY_SERIES_GLOSSARY
        assert parse_glossaries({}).for_series("Anything") is EMPTY_SERIES_GLOSSARY

    def test_lookup_by_normalized_title_and_slug(self):
        catalog = parse_glossaries({
            "Jujutsu Kaisen": {
                "reading_overrides": {"五条": "ゴジョウ"},
                "terms": {"領域展開": "Domain Expansion", "呪力": "Cursed Energy"},
            },
        })

        by_title = catalog.for_series("  jujutsu   kaisen ")
        by_slug = catalog.for_series("jujutsu-kaisen")
        assert by_title.reading_overrides_dict() == {"五条": "ゴジョウ"}
        assert by_slug == by_title
        assert catalog.for_series("Other Manga") is EMPTY_SERIES_GLOSSARY
        assert catalog.for_series("") is EMPTY_SERIES_GLOSSARY

    def test_matching_terms_filters_to_present_page_text(self):
        catalog = parse_glossaries({
            "Series A": {
                "terms": {
                    "領域展開": "Domain Expansion",
                    "呪力": "Cursed Energy",
                    "無量空処": "Unlimited Void",
                },
            },
        })
        glossary = catalog.for_series("Series A")

        matched = glossary.matching_terms(["領域展開！", "これが無量空処だ"])
        assert matched == (
            ("領域展開", "Domain Expansion"),
            ("無量空処", "Unlimited Void"),
        )
        assert glossary.matching_terms(["関係ない台詞"]) == ()

    @pytest.mark.parametrize("raw", [
        ["not", "a", "mapping"],
        {"": {"terms": {"a": "b"}}},
        {"Series": "not-a-mapping"},
        {"Series": {"unknown_key": {}}},
        {"Series": {"terms": {"": "Target"}}},
        {"Series": {"terms": {"Source": ""}}},
        {"Series": {"reading_overrides": {1: "カナ"}}},
    ])
    def test_rejects_malformed_glossaries(self, raw):
        with pytest.raises(ValueError):
            parse_glossaries(raw)

    def test_rejects_excess_entries(self):
        too_many_terms = {f"term{i}": f"val{i}" for i in range(MAX_TERMS + 1)}
        with pytest.raises(ValueError, match="terms exceeds maximum"):
            parse_glossaries({"Series": {"terms": too_many_terms}})

        too_many_overrides = {
            f"語{i}": "ヨミ" for i in range(MAX_READING_OVERRIDES + 1)
        }
        with pytest.raises(ValueError, match="reading_overrides exceeds maximum"):
            parse_glossaries({"Series": {"reading_overrides": too_many_overrides}})


class TestFuriganaReadingOverrides:
    def test_single_morpheme_override_without_mutating_global_defaults(self):
        before = dict(_READING_OVERRIDES)
        segments = annotate("私", reading_overrides={"私": "オレ"})
        assert segments == [{
            "text": "私",
            "furigana": "おれ",
            "needs_furigana": True,
        }]
        assert _READING_OVERRIDES == before
        default_segments = annotate("私")
        assert default_segments[0]["furigana"] == "わたし"

    def test_multi_morpheme_character_name_override(self):
        segments = annotate(
            "五条先生です",
            reading_overrides={"五条": "ゴジョウ"},
        )
        assert any(
            seg["text"] == "五条" and seg["furigana"] == "ごじょう"
            for seg in segments
        )

    def test_transform_furigana_forwards_overrides(self):
        br = BubbleResult(bbox=(0, 0, 50, 50), ocr_text="私", is_valid=True)
        transform_furigana(br, reading_overrides={"私": "ボク"})
        assert br.transformed == [{
            "text": "私",
            "furigana": "ぼく",
            "needs_furigana": True,
        }]


class TestTranslatorGlossaryInjection:
    def test_japanese_single_translator_includes_glossary_terms(self):
        captured: dict[str, object] = {}

        def fake_post(payload):
            captured["prompt"] = payload["messages"][0]["content"]
            return "Domain Expansion!"

        with patch("kindle.translator._post_chat_content", side_effect=fake_post):
            out = translate_ja_verified(
                "領域展開！",
                "en",
                terms=(("領域展開", "Domain Expansion"),),
            )
        assert out == "Domain Expansion!"
        assert "Glossary" in str(captured["prompt"])
        assert "領域展開 -> Domain Expansion" in str(captured["prompt"])

    def test_korean_single_translator_includes_glossary_terms(self):
        captured: dict[str, object] = {}

        def fake_post(payload):
            captured["prompt"] = payload["messages"][0]["content"]
            return "Arise."

        with patch("webtoon.translator._post_chat_content", side_effect=fake_post):
            out = translate_ko_verified(
                "일어나라.",
                "en",
                terms=(("일어나라", "Arise"),),
            )
        assert out == "Arise."
        assert "Glossary" in str(captured["prompt"])
        assert "일어나라 -> Arise" in str(captured["prompt"])


class TestJobGlossaryWiring:
    def test_manga_translate_passes_matching_series_terms(self):
        catalog = parse_glossaries({
            "Jujutsu Kaisen": {
                "terms": {
                    "領域展開": "Domain Expansion",
                    "未使用": "Unused Term",
                },
            },
        })
        captured_requests = []

        def fake_translate_page(req):
            captured_requests.append(req)
            return PageTranslationResult(
                values=tuple(
                    TranslationValue(item.id, "Domain Expansion!")
                    for item in req.items
                ),
                batch_attempts=1,
            )

        def fake_cleanup(page, mode):
            page.output_img = page.img_pil.copy()
            page.render_plan = MangaRenderPlan(
                base_font_size=14,
                font_sizes={0: 14},
                page_font_cap=None,
                source_outlier_threshold=None,
                page_font_floor=None,
                artwork_inpainted=frozenset(),
            )

        base = production_dependencies()
        deps = base.__class__(
            **{
                **base.__dict__,
                "config_value": lambda key: (
                    catalog if key == "GLOSSARIES" else base.config_value(key)
                ),
                "detect_manga": lambda page: setattr(
                    page, "bubbles_raw",
                    [{"bbox": (10, 10, 60, 60), "is_artwork_text": False}],
                ),
                "ocr_manga": lambda page, bubble: BubbleResult(
                    bbox=bubble["bbox"], ocr_text="領域展開！", is_valid=True,
                ),
                "translate_page": fake_translate_page,
                "cleanup_manga": fake_cleanup,
                "render_manga": lambda page, mode: _png_bytes(),
            }
        )

        job = ProcessingJob(
            job_id="job-glossary-translate",
            pipeline="manga_translate",
            image_bytes=_png_bytes(),
            title="Jujutsu Kaisen",
            source_hash="a" * 64,
        )
        result = process_job(job, dependencies=deps)
        assert result.status == "completed"
        assert len(captured_requests) == 1
        assert captured_requests[0].terms == (("領域展開", "Domain Expansion"),)

    def test_manga_furigana_passes_series_reading_overrides(self):
        catalog = parse_glossaries({
            "Jujutsu Kaisen": {
                "reading_overrides": {"五条": "ゴジョウ"},
            },
        })
        captured_overrides = []

        def fake_transform(br, **kwargs):
            captured_overrides.append(kwargs.get("reading_overrides"))
            br.transformed = [{
                "text": "五条", "furigana": "ごじょう", "needs_furigana": True,
            }]

        base = production_dependencies()
        deps = base.__class__(
            **{
                **base.__dict__,
                "config_value": lambda key: (
                    catalog if key == "GLOSSARIES" else base.config_value(key)
                ),
                "detect_manga": lambda page: setattr(
                    page, "bubbles_raw",
                    [{"bbox": (10, 10, 60, 60), "is_artwork_text": False}],
                ),
                "ocr_manga": lambda page, bubble: BubbleResult(
                    bbox=bubble["bbox"], ocr_text="五条", is_valid=True,
                ),
                "transform_furigana": fake_transform,
                "cleanup_manga": lambda page, mode: setattr(
                    page, "output_img", page.img_pil.copy(),
                ),
                "render_manga": lambda page, mode: _png_bytes(),
            }
        )

        job = ProcessingJob(
            job_id="job-glossary-furigana",
            pipeline="manga_furigana",
            image_bytes=_png_bytes(),
            title="Jujutsu Kaisen",
            source_hash="b" * 64,
        )
        result = process_job(job, dependencies=deps)
        assert result.status == "completed"
        assert captured_overrides == [{"五条": "ゴジョウ"}]

    def test_book_furigana_passes_series_reading_overrides(self):
        catalog = parse_glossaries({
            "My Novel": {
                "reading_overrides": {"主人公": "ヒーロー"},
            },
        })
        captured_overrides = []

        def fake_annotate(text, **kwargs):
            captured_overrides.append(kwargs.get("reading_overrides"))
            return [{"text": text, "furigana": "ひーろー", "needs_furigana": True}]

        fake_layout = MagicMock(is_prose=True, columns=[])

        def fake_read_columns(img, layout, ocr, annotate, progress=None):
            annotate("主人公")
            return []

        base = production_dependencies()
        deps = base.__class__(
            **{
                **base.__dict__,
                "config_value": lambda key: (
                    catalog if key == "GLOSSARIES" else base.config_value(key)
                ),
                "analyze_layout": lambda img: fake_layout,
                "read_columns": fake_read_columns,
                "annotate": fake_annotate,
                "render_book": lambda img, layout, readings, scale=1.0: img,
            }
        )

        job = ProcessingJob(
            job_id="job-glossary-book",
            pipeline="book_furigana",
            image_bytes=_png_bytes(),
            title="My Novel",
            source_hash="c" * 64,
        )
        result = process_job(job, dependencies=deps)
        assert result.status == "completed"
        assert captured_overrides == [{"主人公": "ヒーロー"}]
