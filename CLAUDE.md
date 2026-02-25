# Frank Manga - Technical Reference

## What This Is

Proof of concept for processing manga page images in two modes:
1. **Furigana pipeline** (`adult*.png`): Add hiragana readings next to kanji in speech bubbles
2. **Translation pipeline** (`shounen*.png`): Replace Japanese dialogue with English translations

Goal: validate the approach before building a Chromium extension for read.amazon.co.jp.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Requires Ollama running locally with qwen2.5vl:32b loaded
python process_manga.py furigana          # adult*.png → output/furigana/
python process_manga.py translate         # shounen*.png → output/translate/
python process_manga.py all              # both pipelines
python process_manga.py all --debug      # both + debug bounding box images
```

## Architecture

```
Image → [Bubble Detection] → [OCR] → [Validation] → ┬→ [Furigana] → [Vertical Render]
                                                      └→ [Translate] → [English Render]
```

### Pipeline Modules (`pipeline/`)

| Module | Purpose | Key Details |
|--------|---------|-------------|
| `config.py` | Constants | Model names, font paths, thresholds |
| `bubble_detector.py` | Speech bubble detection | Pure OpenCV contour analysis with 5-layer false positive filtering |
| `ocr.py` | Text extraction | manga-ocr (CPU-only to avoid VRAM conflicts) + Japanese validation |
| `furigana.py` | Kanji → hiragana | pykakasi wrapper, returns annotated segments |
| `translator.py` | JP → EN translation | Ollama `qwen2.5vl:32b`, fallback to Google Translate |
| `text_renderer.py` | Text rendering | Vertical JP with furigana, horizontal EN with hyphenation, vertical SFX |
| `image_utils.py` | Image I/O | OpenCV/Pillow loaders, crop, clear, base64 conversion |

## Critical Technical Decisions

### Bubble Detection (bubble_detector.py)

Uses pure OpenCV — VLM-based detection (Qwen2.5-VL) was tried first but produced unreliable coordinates.

**Detection flow**: Binary threshold (>200) → morphological cleanup → RETR_TREE contour hierarchy → filter by area, aspect ratio, convex hull solidity (>0.6), interior brightness (>200).

**Five false-positive filters** (reject faces, clothing, white backgrounds):

1. **Edge density < 0.10** — Bubbles have sparse edges (just text strokes); faces have many (hair, eyes, nose)
2. **Bright pixel ratio > 0.80** — Bubbles are >240 white; faces have gradients
3. **Mid-tone ratio < 0.10** — Key discriminator. Faces have many pixels in 80-220 range (skin gradients). Bubbles are bimodal: mostly >240 white + some <80 black text, almost nothing in between
4. **Circularity > 0.15** — Bubbles are round/elliptical; faces and clothing are irregular shapes. Formula: `4π × area / perimeter²`
5. **Border darkness < 140** — Speech bubbles have dark ink outlines; faces don't

**Why NOT std dev**: Interior pixel standard deviation was tried but rejects real bubbles — text strokes (black on white) create high variance even in legitimate speech bubbles.

### OCR Validation (ocr.py)

`is_valid_japanese()` rejects noise from non-text detections. Checks that >50% of characters are in Japanese Unicode ranges (hiragana, katakana, CJK, fullwidth). This catches single-character gibberish manga-ocr produces from face/background regions.

### Translation (translator.py)

- Model: `qwen2.5vl:32b` (same as bubble detection config)
- **NOT qwen3-vl** — returns empty responses for translation tasks
- Text-only requests (no image context needed for translation)
- Strips `<think>...</think>` tags and XML artifacts from output
- Fallback: `deep-translator` (Google Translate) on Ollama failure

### Text Rendering (text_renderer.py)

**English text layout selection**:
- Sound effects (detected by regex: repeated chars, exclamation words, pure punctuation) → vertical stacked letters
- All other dialogue → horizontal with word-wrap

**Hyphenation** for narrow/tall bubbles (aspect ratio > 1.2): Splits words at syllable boundaries (consonant-vowel transitions). Important because manga bubbles are designed for vertical Japanese text and are often too narrow for horizontal English.

**Font sizing**: Binary search for largest font that fits the bubble dimensions.

**Vertical Japanese with furigana**: Character-by-character rendering (Pillow's `direction="ttb"` has bugs with JP). Columns flow right-to-left. Furigana rendered at ~45% size to the right of each kanji.

## External Dependencies

- **Ollama** must be running locally with `qwen2.5vl:32b` model loaded (~21GB VRAM)
- **manga-ocr** runs on CPU (forced via `CUDA_VISIBLE_DEVICES=""` before import)
- **Fonts**: Noto CJK at `/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc` (Arch Linux path)

## Test Data

- `docs/adult*.png` (4 pages): Seinen manga without furigana — tests furigana pipeline
- `docs/shounen*.png` (5 pages): Various styles (action, historical, fantasy, One Piece color, European-setting) — tests translation pipeline
- Output: `output/furigana/` and `output/translate/` subdirectories
- Debug mode (`--debug`): Saves `*-debug.png` with red bounding boxes on detected bubbles
