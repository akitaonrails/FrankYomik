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
_text_det = _yaml.get("text_detection", {})
_manga_inp = _yaml.get("manga_inpainting", {})

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

# --- Font paths ---
def _resolve_font(path: str) -> str:
    """Resolve font path: absolute paths pass through, relative resolved from project root."""
    if os.path.isabs(path):
        return path
    return os.path.join(_PROJECT_ROOT, path)


FONT_JP = _resolve_font(_fonts.get("jp", "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"))
FONT_JP_BOLD = _resolve_font(_fonts.get("jp_bold", "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc"))
FONT_EN = _resolve_font(_fonts.get("en", "fonts/KomikaText-Regular.ttf"))
FONT_EN_BOLD = _resolve_font(_fonts.get("en_bold", "fonts/KomikaText-Bold.ttf"))
FONT_SFX = _resolve_font(_fonts.get("sfx", "fonts/BadaBoomBB.ttf"))

# --- Bubble detection (RT-DETR-v2) ---
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

# --- Text detection ---
# EasyOCR-based text detection is no longer needed for manga — RT-DETR-v2
# detects both bubbles and free text. These are kept for backward compat
# if text_detector.py is used directly.
TEXT_DETECTION_ENABLED = _text_det.get("enabled", False)
TEXT_DETECTION_CONFIDENCE = _text_det.get("confidence", 0.3)
TEXT_DETECTION_GPU = _text_det.get("gpu", True)

# --- Manga inpainting (artwork text) ---
MANGA_INPAINT_ENABLED = _manga_inp.get("enabled", False)
MANGA_INPAINT_PAD = _manga_inp.get("pad", 20)

# --- File paths ---
DOCS_DIR = os.path.join(_PROJECT_ROOT, "docs")
OUTPUT_DIR = os.path.join(_PROJECT_ROOT, "output")
