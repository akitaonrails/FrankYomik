"""Configuration for the webtoon processing pipeline.

Reuses Ollama and font settings from pipeline.config, adds webtoon-specific
settings from the 'webtoon' section of config.yaml.
"""

import os

import yaml

from pipeline.config import (
    OLLAMA_BASE_URL,
    TRANSLATE_MODEL,
    TRANSLATE_OPTIONS,
    TRANSLATE_THINK,
    FONT_EN,
)

# Re-export for use within webtoon package
__all__ = [
    "OLLAMA_BASE_URL", "TRANSLATE_MODEL", "TRANSLATE_OPTIONS", "TRANSLATE_THINK",
    "FONT_EN", "FONT_KO",
    "DATA_DIR", "OUTPUT_DIR",
    "SCROLL_PAUSE", "DOWNLOAD_TIMEOUT",
    "EASYOCR_GPU", "EASYOCR_CONFIDENCE_THRESHOLD",
    "EASYOCR_TEXT_THRESHOLD", "EASYOCR_LOW_TEXT",
    "CLUSTER_GAP", "PAD_X", "PAD_Y",
    "FLOOD_FILL_TOLERANCE", "CONTOUR_EXPAND",
]

_PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))


def _load_webtoon_config() -> dict:
    """Load the webtoon section from config.yaml."""
    config_path = os.path.join(_PROJECT_ROOT, "config.yaml")
    try:
        with open(config_path, "r") as f:
            full = yaml.safe_load(f) or {}
        return full.get("webtoon", {})
    except (FileNotFoundError, yaml.YAMLError):
        return {}


_cfg = _load_webtoon_config()
_scraper = _cfg.get("scraper", {})
_easyocr_cfg = _cfg.get("easyocr", {})
_bubble_cfg = _cfg.get("bubble_detection", {})

# --- Paths ---
DATA_DIR = os.path.join(_PROJECT_ROOT, _cfg.get("data_dir", "webtoon_data"))
OUTPUT_DIR = os.path.join(_PROJECT_ROOT, _cfg.get("output_dir", "output/webtoon"))

# --- Scraper ---
SCROLL_PAUSE = _scraper.get("scroll_pause", 0.5)
DOWNLOAD_TIMEOUT = _scraper.get("download_timeout", 30)

# --- EasyOCR ---
EASYOCR_GPU = _easyocr_cfg.get("gpu", True)
EASYOCR_CONFIDENCE_THRESHOLD = _easyocr_cfg.get("confidence_threshold", 0.3)
EASYOCR_TEXT_THRESHOLD = _easyocr_cfg.get("text_threshold", 0.7)
EASYOCR_LOW_TEXT = _easyocr_cfg.get("low_text", 0.4)

# --- Bubble detection ---
CLUSTER_GAP = _bubble_cfg.get("cluster_gap", 40)
PAD_X = _bubble_cfg.get("pad_x", 15)
PAD_Y = _bubble_cfg.get("pad_y", 10)
FLOOD_FILL_TOLERANCE = _bubble_cfg.get("flood_fill_tolerance", 15)
CONTOUR_EXPAND = _bubble_cfg.get("contour_expand", 5)

# --- Font (NotoSansCJK includes Korean glyphs) ---
FONT_KO = FONT_EN
