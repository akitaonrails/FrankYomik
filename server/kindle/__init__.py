"""Manga processing pipeline.

Public processor names are loaded on first access.  Importing a lightweight
module such as :mod:`kindle.config` therefore does not import Torch or model
backends during test collection and command-line startup.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .processor import BubbleResult, PageResult, PipelineMode

__all__ = [
    "BubbleResult",
    "PageResult",
    "PipelineMode",
    "detect_page_bubbles",
    "load_page",
    "ocr_bubble",
    "render_page",
    "transform_furigana",
    "transform_translate",
]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(".processor", __name__), name)
    globals()[name] = value
    return value
