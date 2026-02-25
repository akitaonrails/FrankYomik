"""Configuration constants for the manga processing pipeline."""

import os

# Ollama settings
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
BUBBLE_DETECT_MODEL = "qwen2.5vl:32b"

# Font paths (Noto CJK on Arch Linux)
FONT_JP = "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"
FONT_JP_BOLD = "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc"
FONT_EN = "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"

# Bubble detection thresholds
MIN_BUBBLE_AREA = 2000        # Minimum contour area to consider a bubble
MAX_BUBBLE_AREA_RATIO = 0.25  # Max fraction of page area for a single bubble
BUBBLE_PADDING = 5            # Pixels to pad inside bubble for text rendering

# OCR settings
MANGA_OCR_DEVICE = "cpu"  # Force CPU to avoid VRAM conflicts with Ollama

# Text rendering
FURIGANA_SIZE_RATIO = 0.45   # Furigana font size relative to main text
MIN_FONT_SIZE = 10
MAX_FONT_SIZE = 60
TEXT_MARGIN = 8               # Margin inside bubble for text placement

# English font normalization (base_size = page_height / divisor, clamped)
EN_BASE_FONT_DIVISOR = 55
EN_BASE_FONT_MIN = 14
EN_BASE_FONT_MAX = 24

# File paths
DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs")
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "output")
ADULT_INPUT = os.path.join(DOCS_DIR, "adult.png")
SHOUNEN_INPUT = os.path.join(DOCS_DIR, "shounen.png")
