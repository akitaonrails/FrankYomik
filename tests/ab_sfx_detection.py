#!/usr/bin/env python3
"""A/B test: compare SFX detection approaches on difficult webtoon pages.

Test pages (chapter 297): 038, 043, 061, 062, 063
These have large artistic Korean SFX that EasyOCR currently misses.

Expected SFX per page (ground truth from visual inspection):
  038: 쾅 (BOOM/CRASH) — gray brush calligraphy on white
  043: 타앙 (BANG) — white outlined on dark bg, + 쾅 black brush at bottom
  061: 처이잉 (SWOOSH) — white brush on blue action scene
  062: 쾅 (BOOM) — huge white stylized strokes with speed lines
  063: 와앙 (WHAM) — massive white brush on explosion

Approaches tested:
  A) EasyOCR current — baseline (text_threshold=0.5, low_text=0.3)
  B) EasyOCR aggressive — much lower thresholds
  C) VLM (Qwen2.5-VL:32b) — send image, ask for SFX text + bbox
  D) Hybrid: VLM text + contour-based positioning

CONCLUSION (2026-02-25):
  None of the 4 approaches reliably detect large artistic brush-stroke SFX.
  - A (current EasyOCR): catches nothing useful on these pages
  - B (aggressive EasyOCR): floods with garbage (@, $, {, %) — worse than A
  - C (VLM): correctly read 1/5 pages (061: 치이익≈처이잉), partially 1/5
    (062: 콰), wrong on 3/5. Slow: 10-24s per page.
  - D (hybrid): VLM text same quality as C, contour positioning unreliable
    (picks background regions, panel borders)
  These SFX are essentially artwork (motion blur, transparency, integrated
  into action scenes) rather than text. Not worth integrating any approach.
  The existing pipeline correctly catches "text-like" SFX (e.g. page 057).
  Full results in tests/ab_sfx_detection_results.json.
"""

import base64
import json
import logging
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import requests
from PIL import Image

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.config import OLLAMA_BASE_URL
from webtoon.ocr import TextDetection

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# --- Test data ---

CHAPTER_DIR = PROJECT_ROOT / "webtoon_data" / "747269" / "chapter_297"

TEST_PAGES = {
    "038": {"expected_sfx": ["쾅"], "description": "gray brush calligraphy on white bg"},
    "043": {"expected_sfx": ["타앙", "쾅"], "description": "white outlined on dark + black brush"},
    "061": {"expected_sfx": ["처이잉"], "description": "white brush on blue action scene"},
    "062": {"expected_sfx": ["쾅"], "description": "huge white stylized with speed lines"},
    "063": {"expected_sfx": ["와앙"], "description": "massive white brush on explosion"},
}


def load_page(page_num: str) -> tuple[np.ndarray, Image.Image]:
    """Load a page as both OpenCV (BGR) and Pillow (RGB)."""
    path = CHAPTER_DIR / f"{page_num}.jpg"
    img_cv = cv2.imread(str(path))
    img_pil = Image.open(path).convert("RGB")
    return img_cv, img_pil


def image_to_base64(img_pil: Image.Image) -> str:
    """Convert Pillow image to base64 JPEG string."""
    import io
    buf = io.BytesIO()
    img_pil.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ─── Approach A: EasyOCR (current settings) ──────────────────────────

def approach_a_easyocr_current(img_pil: Image.Image) -> list[dict]:
    """Run EasyOCR with current production settings."""
    from webtoon.ocr import detect_and_read
    detections = detect_and_read(img_pil)
    return [
        {
            "text": d.text,
            "confidence": round(d.confidence, 3),
            "bbox": d.bbox_rect,
            "height": d.bbox_rect[3] - d.bbox_rect[1],
        }
        for d in detections
    ]


# ─── Approach B: EasyOCR aggressive thresholds ───────────────────────

def approach_b_easyocr_aggressive(img_pil: Image.Image) -> list[dict]:
    """Run EasyOCR with much lower thresholds to catch stylized text."""
    import easyocr

    img_array = np.array(img_pil)

    # Get the shared reader
    from webtoon.ocr import _get_reader
    reader = _get_reader()

    results = []

    # Pass 1: original with very low thresholds
    for text_thresh, low_txt, label in [
        (0.3, 0.2, "aggressive"),
        (0.2, 0.1, "ultra-aggressive"),
    ]:
        raw = reader.readtext(
            img_array,
            text_threshold=text_thresh,
            low_text=low_txt,
        )
        for bbox_poly, text, conf in raw:
            if not text.strip():
                continue
            xs = [pt[0] for pt in bbox_poly]
            ys = [pt[1] for pt in bbox_poly]
            results.append({
                "text": text.strip(),
                "confidence": round(conf, 3),
                "bbox": (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))),
                "height": int(max(ys)) - int(min(ys)),
                "pass": label,
            })

    # Pass 2: inverted image with aggressive settings
    gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
    inv = 255 - gray
    clahe = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(inv)

    raw = reader.readtext(enhanced, text_threshold=0.3, low_text=0.2)
    for bbox_poly, text, conf in raw:
        if not text.strip():
            continue
        xs = [pt[0] for pt in bbox_poly]
        ys = [pt[1] for pt in bbox_poly]
        results.append({
            "text": text.strip(),
            "confidence": round(conf, 3),
            "bbox": (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))),
            "height": int(max(ys)) - int(min(ys)),
            "pass": "inverted-aggressive",
        })

    # Deduplicate by text
    seen = set()
    deduped = []
    for r in results:
        key = r["text"]
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    return deduped


# ─── Approach C: VLM direct (Qwen2.5-VL) ─────────────────────────────

def approach_c_vlm_direct(img_pil: Image.Image) -> list[dict]:
    """Send image to Qwen2.5-VL, ask for SFX text and approximate position."""
    b64 = image_to_base64(img_pil)
    w, h = img_pil.size

    prompt = (
        "This is a panel from a Korean webtoon comic. "
        "Look for Korean sound effect text (SFX/onomatopoeia) drawn in large, "
        "artistic brush strokes integrated into the artwork. These are NOT "
        "dialogue in speech bubbles — they are stylized text like 쾅, 타앙, "
        "처이잉, 와앙, 쿵 drawn directly on the art.\n\n"
        "For each SFX you find, output a JSON array with objects like:\n"
        '  {"text": "쾅", "position": "center", "size": "large"}\n\n'
        "position should be one of: top-left, top-center, top-right, "
        "center-left, center, center-right, bottom-left, bottom-center, bottom-right\n\n"
        "If there are no SFX, output: []\n"
        "Output ONLY the JSON array, nothing else."
    )

    payload = {
        "model": "qwen2.5vl:32b",
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [b64],
            }
        ],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 512},
    }

    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        raw = resp.json().get("message", {}).get("content", "")
        log.info("  VLM raw response: %s", raw[:200])

        # Try to parse JSON from response
        import re
        # Strip thinking tags if present
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        # Find JSON array in response
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            items = json.loads(match.group())
            return [
                {
                    "text": item.get("text", ""),
                    "position": item.get("position", "center"),
                    "size": item.get("size", "unknown"),
                    "bbox": _position_to_bbox(
                        item.get("position", "center"), w, h,
                        item.get("size", "large")
                    ),
                }
                for item in items
                if item.get("text")
            ]
        return [{"text": raw.strip(), "position": "unknown", "raw": True}]
    except Exception as e:
        log.error("  VLM call failed: %s", e)
        return [{"error": str(e)}]


def _position_to_bbox(position: str, img_w: int, img_h: int,
                      size: str = "large") -> tuple[int, int, int, int]:
    """Convert a position label to an approximate bbox."""
    # Size determines how much of the image the SFX covers
    if size == "large":
        sw, sh = int(img_w * 0.5), int(img_h * 0.3)
    elif size == "medium":
        sw, sh = int(img_w * 0.35), int(img_h * 0.2)
    else:
        sw, sh = int(img_w * 0.25), int(img_h * 0.15)

    # Position grid
    positions = {
        "top-left":      (img_w * 0.25, img_h * 0.2),
        "top-center":    (img_w * 0.5,  img_h * 0.2),
        "top-right":     (img_w * 0.75, img_h * 0.2),
        "center-left":   (img_w * 0.25, img_h * 0.45),
        "center":        (img_w * 0.5,  img_h * 0.45),
        "center-right":  (img_w * 0.75, img_h * 0.45),
        "bottom-left":   (img_w * 0.25, img_h * 0.75),
        "bottom-center": (img_w * 0.5,  img_h * 0.75),
        "bottom-right":  (img_w * 0.75, img_h * 0.75),
    }
    cx, cy = positions.get(position, (img_w * 0.5, img_h * 0.45))
    x1 = max(0, int(cx - sw // 2))
    y1 = max(0, int(cy - sh // 2))
    x2 = min(img_w, int(cx + sw // 2))
    y2 = min(img_h, int(cy + sh // 2))
    return (x1, y1, x2, y2)


# ─── Approach D: Hybrid VLM + contour positioning ────────────────────

def approach_d_hybrid(img_pil: Image.Image, img_cv: np.ndarray) -> list[dict]:
    """VLM reads the SFX text, contour analysis finds the position."""

    # Step 1: Ask VLM what SFX text is in the image (text-only answer)
    b64 = image_to_base64(img_pil)

    prompt = (
        "This is a Korean webtoon panel. What Korean sound effect (SFX) text "
        "is drawn in large artistic brush strokes on this image? "
        "These are onomatopoeia like 쾅, 타앙, 처이잉, 와앙, 쿵.\n"
        "Output ONLY the Korean SFX text, one per line. "
        "If there are no SFX, output: NONE"
    )

    payload = {
        "model": "qwen2.5vl:32b",
        "messages": [
            {"role": "user", "content": prompt, "images": [b64]}
        ],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 256},
    }

    sfx_texts = []
    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        raw = resp.json().get("message", {}).get("content", "")
        import re
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        raw = raw.strip()
        log.info("  VLM text response: %s", raw[:200])

        if raw.upper() != "NONE":
            for line in raw.split("\n"):
                line = line.strip().strip("-•*").strip()
                if line and any(0xAC00 <= ord(c) <= 0xD7AF for c in line):
                    sfx_texts.append(line)
    except Exception as e:
        log.error("  VLM call failed: %s", e)
        return [{"error": str(e)}]

    if not sfx_texts:
        return []

    # Step 2: Find large text-like contours in the image
    regions = _find_large_text_regions(img_cv)

    results = []
    for i, text in enumerate(sfx_texts):
        result = {"text": text, "source": "vlm"}
        if i < len(regions):
            result["bbox"] = regions[i]
            result["positioning"] = "contour"
        else:
            # Fallback: center of image
            h, w = img_cv.shape[:2]
            result["bbox"] = _position_to_bbox("center", w, h, "large")
            result["positioning"] = "fallback-center"
        results.append(result)

    return results


def _find_large_text_regions(img_cv: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Find large text-like regions using contour analysis.

    Looks for large bright or dark regions with text-like aspect ratios.
    Returns bounding boxes sorted by area (largest first).
    """
    h, w = img_cv.shape[:2]
    min_area = (w * h) * 0.01  # At least 1% of image area

    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
    regions = []

    # Try both bright-on-dark and dark-on-bright
    for thresh_mode in [cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV]:
        _, binary = cv2.threshold(gray, 200, 255, thresh_mode)

        # Morphological operations to connect text strokes
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (20, 20))
        dilated = cv2.dilate(binary, kernel, iterations=2)

        contours, _ = cv2.findContours(
            dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < min_area:
                continue

            x, y, cw, ch = cv2.boundingRect(cnt)

            # Skip if it fills most of the image (it's the background)
            if cw * ch > w * h * 0.7:
                continue

            # Skip very thin regions (speed lines, panel borders)
            if cw < 50 or ch < 50:
                continue

            regions.append((x, y, x + cw, y + ch))

    # Sort by area descending
    regions.sort(key=lambda r: (r[2] - r[0]) * (r[3] - r[1]), reverse=True)

    # Deduplicate overlapping regions (keep largest)
    from webtoon.ocr import _iou
    deduped = []
    for r in regions:
        if not any(_iou(r, existing) > 0.3 for existing in deduped):
            deduped.append(r)

    return deduped[:5]  # Top 5 at most


# ─── Runner ───────────────────────────────────────────────────────────

def run_test():
    results = {}

    for page_num, info in TEST_PAGES.items():
        print(f"\n{'='*70}")
        print(f"Page {page_num}: {info['description']}")
        print(f"Expected SFX: {', '.join(info['expected_sfx'])}")
        print(f"{'='*70}")

        img_cv, img_pil = load_page(page_num)

        page_results = {}

        # --- A: EasyOCR current ---
        print(f"\n  [A] EasyOCR (current settings)...")
        t0 = time.time()
        a_results = approach_a_easyocr_current(img_pil)
        t_a = time.time() - t0
        page_results["A_easyocr_current"] = {
            "detections": a_results,
            "time_s": round(t_a, 2),
        }
        print(f"      Time: {t_a:.2f}s | Found {len(a_results)} detections")
        for d in a_results:
            print(f"      - '{d['text']}' conf={d['confidence']} h={d['height']}px")

        # --- B: EasyOCR aggressive ---
        print(f"\n  [B] EasyOCR (aggressive thresholds)...")
        t0 = time.time()
        b_results = approach_b_easyocr_aggressive(img_pil)
        t_b = time.time() - t0
        page_results["B_easyocr_aggressive"] = {
            "detections": b_results,
            "time_s": round(t_b, 2),
        }
        print(f"      Time: {t_b:.2f}s | Found {len(b_results)} detections")
        for d in b_results:
            print(f"      - '{d['text']}' conf={d['confidence']} h={d['height']}px"
                  f" [{d.get('pass', '')}]")

        # --- C: VLM direct ---
        print(f"\n  [C] VLM direct (Qwen2.5-VL)...")
        t0 = time.time()
        c_results = approach_c_vlm_direct(img_pil)
        t_c = time.time() - t0
        page_results["C_vlm_direct"] = {
            "detections": c_results,
            "time_s": round(t_c, 2),
        }
        print(f"      Time: {t_c:.2f}s | Found {len(c_results)} results")
        for d in c_results:
            print(f"      - '{d.get('text', '?')}' pos={d.get('position', '?')}"
                  f" size={d.get('size', '?')}")

        # --- D: Hybrid ---
        print(f"\n  [D] Hybrid (VLM text + contour positioning)...")
        t0 = time.time()
        d_results = approach_d_hybrid(img_pil, img_cv)
        t_d = time.time() - t0
        page_results["D_hybrid"] = {
            "detections": d_results,
            "time_s": round(t_d, 2),
        }
        print(f"      Time: {t_d:.2f}s | Found {len(d_results)} results")
        for d in d_results:
            print(f"      - '{d.get('text', '?')}' bbox={d.get('bbox', '?')}"
                  f" [{d.get('positioning', '?')}]")

        results[page_num] = page_results

    # --- Summary ---
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"\n{'Page':<6} {'Expected':<12} {'A:Current':<15} {'B:Aggressive':<15}"
          f" {'C:VLM':<15} {'D:Hybrid':<15}")
    print("-" * 78)

    for page_num, info in TEST_PAGES.items():
        expected = ", ".join(info["expected_sfx"])
        pr = results[page_num]

        def _found(approach_key):
            dets = pr[approach_key]["detections"]
            texts = [d.get("text", "?") for d in dets if "error" not in d]
            t = pr[approach_key]["time_s"]
            if texts:
                return f"{', '.join(texts[:2])} ({t}s)"
            return f"MISS ({t}s)"

        print(f"{page_num:<6} {expected:<12} {_found('A_easyocr_current'):<15}"
              f" {_found('B_easyocr_aggressive'):<15}"
              f" {_found('C_vlm_direct'):<15}"
              f" {_found('D_hybrid'):<15}")

    # Save full results to JSON
    output_path = PROJECT_ROOT / "tests" / "ab_sfx_detection_results.json"

    # Convert tuples to lists for JSON serialization
    def _jsonify(obj):
        if isinstance(obj, tuple):
            return list(obj)
        if isinstance(obj, dict):
            return {k: _jsonify(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_jsonify(i) for i in obj]
        return obj

    with open(output_path, "w") as f:
        json.dump(_jsonify(results), f, indent=2, ensure_ascii=False)
    print(f"\nFull results saved to: {output_path}")


if __name__ == "__main__":
    run_test()
