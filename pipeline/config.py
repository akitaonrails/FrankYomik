"""Configuration constants for the manga processing pipeline."""

import os

import yaml

# --- YAML config loader ---

_PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))


def _load_yaml_config() -> dict:
    """Load config.yaml from project root, returning empty dict on failure."""
    config_path = os.path.join(_PROJECT_ROOT, "config.yaml")
    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return {}


_yaml = _load_yaml_config()
_ollama = _yaml.get("ollama", {})
_fonts = _yaml.get("fonts", {})
_ocr = _yaml.get("ocr", {})

# --- Ollama settings ---
# Environment variable takes highest priority
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", _ollama.get("url", "http://localhost:11434"))
BUBBLE_DETECT_MODEL = "qwen2.5vl:32b"

# Translation model (separate from bubble detection)
TRANSLATE_MODEL = _ollama.get("translate_model", "qwen3:14b")

# Translation options passed to Ollama
_translate_opts = _ollama.get("translate_options", {})
TRANSLATE_OPTIONS = {
    "temperature": _translate_opts.get("temperature", 0.3),
    "num_predict": _translate_opts.get("num_predict", 1024),
}
# think: false for qwen3 models; None means omit from request
TRANSLATE_THINK = _translate_opts.get("think", None)

# --- Font paths (Noto CJK on Arch Linux) ---
FONT_JP = _fonts.get("jp", "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc")
FONT_JP_BOLD = _fonts.get("jp_bold", "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc")
FONT_EN = _fonts.get("en", "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc")

# --- Bubble detection thresholds ---
MIN_BUBBLE_AREA = 2000        # Minimum contour area to consider a bubble
MAX_BUBBLE_AREA_RATIO = 0.25  # Max fraction of page area for a single bubble
BUBBLE_PADDING = 5            # Pixels to pad inside bubble for text rendering

# --- OCR settings ---
MANGA_OCR_DEVICE = _ocr.get("device", "cpu")  # Force CPU to avoid VRAM conflicts with Ollama

# --- Text rendering ---
FURIGANA_SIZE_RATIO = 0.45   # Furigana font size relative to main text
MIN_FONT_SIZE = 10
MAX_FONT_SIZE = 60
TEXT_MARGIN = 8               # Margin inside bubble for text placement

# English font normalization (base_size = page_height / divisor, clamped)
EN_BASE_FONT_DIVISOR = 55
EN_BASE_FONT_MIN = 14
EN_BASE_FONT_MAX = 24

# --- File paths ---
DOCS_DIR = os.path.join(_PROJECT_ROOT, "docs")
OUTPUT_DIR = os.path.join(_PROJECT_ROOT, "output")
