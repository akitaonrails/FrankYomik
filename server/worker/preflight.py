"""Strict worker startup checks and capability reporting."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from pipeline.revision import get_pipeline_revision

log = logging.getLogger(__name__)

ALL_PIPELINES = (
    "book_furigana",
    "manga_furigana",
    "manga_translate",
    "webtoon",
)


class PreflightError(RuntimeError):
    """A required runtime capability could not be initialized."""


@dataclass(frozen=True, slots=True)
class ModelCheck:
    name: str
    pipelines: frozenset[str]
    load: Callable[[], object]


@dataclass(frozen=True, slots=True)
class PreflightReport:
    pipelines: tuple[str, ...]
    pipeline_revision: str
    models: tuple[str, ...]

    def heartbeat_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "pipelines": list(self.pipelines),
            "pipeline_revision": self.pipeline_revision,
            "models": list(self.models),
            "preflight": "passed",
        }


def _load_detector() -> object:
    from kindle.bubble_detector import _get_model

    return _get_model()


def _load_manga_ocr() -> object:
    from kindle.ocr import _get_ocr

    return _get_ocr()


def _load_japanese_easyocr() -> object:
    from kindle.text_detector import _get_reader

    return _get_reader()


def _load_korean_easyocr() -> object:
    from webtoon.ocr import _get_reader

    return _get_reader()


def _load_furigana_dictionary() -> object:
    from kindle.furigana import _get_tagger

    return _get_tagger()


def _verify_ollama() -> None:
    from kindle.translator import verify_ollama_model

    verify_ollama_model("manga_translate")
    verify_ollama_model("webtoon")


def _verify_fonts() -> tuple[Path, ...]:
    from kindle.config import FONT_EN, FONT_EN_BOLD, FONT_JP, FONT_JP_BOLD, FONT_SFX

    paths = tuple(Path(value) for value in {
        FONT_EN, FONT_EN_BOLD, FONT_JP, FONT_JP_BOLD, FONT_SFX,
    })
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"required fonts unavailable: {', '.join(missing)}")
    return paths


def _load_lama_if_enabled() -> object:
    from kindle.config import MANGA_INPAINT_ENABLED
    from webtoon.config import INPAINT_ENABLED

    if not (MANGA_INPAINT_ENABLED or INPAINT_ENABLED):
        return "disabled"
    from webtoon.inpainter import get_backend_for_name

    backend = get_backend_for_name("lama")
    if backend is None:
        raise RuntimeError("pinned LaMa backend failed to load")
    backend.unload()
    return "verified"


def production_model_checks() -> tuple[ModelCheck, ...]:
    """Return every model or data dependency needed by current pipelines."""
    manga = frozenset({"manga_translate", "manga_furigana"})
    translated = frozenset({"manga_translate", "webtoon"})
    return (
        ModelCheck("rtdetr-v2", manga | {"webtoon"}, _load_detector),
        ModelCheck("manga-ocr", manga | {"book_furigana"}, _load_manga_ocr),
        ModelCheck("easyocr-ja", manga, _load_japanese_easyocr),
        ModelCheck("easyocr-ko", frozenset({"webtoon"}), _load_korean_easyocr),
        ModelCheck(
            "unidic-lite", frozenset({"manga_furigana", "book_furigana"}),
            _load_furigana_dictionary,
        ),
        ModelCheck("ollama-qwen3", translated, _verify_ollama),
        ModelCheck("fonts", frozenset(ALL_PIPELINES), _verify_fonts),
        ModelCheck("simple-lama", manga | {"webtoon"}, _load_lama_if_enabled),
    )


def preflight_worker(
    pipelines: Iterable[str] = ALL_PIPELINES,
    *,
    checks: Iterable[ModelCheck] | None = None,
    pipeline_revision: str | None = None,
) -> PreflightReport:
    """Load and verify all requirements before a worker can consume jobs."""
    selected = tuple(sorted(set(pipelines)))
    unknown = set(selected) - set(ALL_PIPELINES)
    if unknown:
        raise PreflightError(f"unknown worker pipelines: {', '.join(sorted(unknown))}")
    if set(selected) != set(ALL_PIPELINES):
        raise PreflightError(
            "current shared queues require a worker capable of every pipeline"
        )

    completed: list[str] = []
    for check in checks if checks is not None else production_model_checks():
        if not check.pipelines.intersection(selected):
            continue
        log.info("Preflight: %s", check.name)
        try:
            check.load()
        except Exception as error:
            raise PreflightError(
                f"required capability {check.name!r} failed: "
                f"{type(error).__name__}: {error}"
            ) from error
        completed.append(check.name)

    return PreflightReport(
        pipelines=selected,
        pipeline_revision=pipeline_revision or get_pipeline_revision(),
        models=tuple(completed),
    )
