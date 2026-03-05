# YOLO Bubble Detection — Fine-Tuning Plan

## Problem

The current manga bubble detector (`pipeline/bubble_detector.py`) is a 550-line OpenCV cascade with 7 layers of hand-tuned heuristic filters (~20 magic thresholds). It's brittle:

- **Edge density, bright pixel ratio, circularity, border darkness, mid-tone ratio, dark content analysis, background uniformity** — each threshold was empirically derived from a small sample. Adjusting one can break another.
- Three supplementary recovery passes bolt on complexity: CLAHE pass 2, edge-based supplementary detection, and `text_detector.py` (stroke clustering + small bubble recovery via morphological gradient).
- Color vs grayscale pages need completely different threshold profiles.
- Manga and webtoon detection are entirely separate codepaths with no shared logic.

---

## Approach: Replace Heuristics with YOLO Object Detection

### Phase 1: Evaluate Pre-Trained Models (No Training)

Two production-ready models exist that detect comic/manga bubbles out of the box:

#### 1a. ogkalu/comic-speech-bubble-detector-yolov8m

- **Architecture**: YOLOv8 Medium
- **Training data**: ~8,000 images (manga, webtoon, manhua, western comics)
- **Resolution**: 1024px
- **Classes**: 1 (speech bubble)
- **License**: Apache 2.0
- **HuggingFace**: https://huggingface.co/ogkalu/comic-speech-bubble-detector-yolov8m

```python
from ultralytics import YOLO
from huggingface_hub import hf_hub_download

model_path = hf_hub_download(
    repo_id="ogkalu/comic-speech-bubble-detector-yolov8m",
    filename="comic-speech-bubble-detector.pt"
)
model = YOLO(model_path)
results = model.predict("manga_page.png", conf=0.5, imgsz=1024)

for box in results[0].boxes:
    x1, y1, x2, y2 = box.xyxy[0].tolist()
    print(f"Bubble: {box.conf[0]:.2f} [{x1:.0f},{y1:.0f},{x2:.0f},{y2:.0f}]")
```

#### 1b. ogkalu/comic-text-and-bubble-detector (RT-DETR-v2)

- **Architecture**: RT-DETR-v2, ResNet-50-vd backbone (42.9M params)
- **Training data**: ~11,000 images
- **Classes**: 3 — `bubble`, `text_bubble` (text inside bubbles), `text_free` (narration/SFX)
- **License**: Apache 2.0
- **HuggingFace**: https://huggingface.co/ogkalu/comic-text-and-bubble-detector

```python
from transformers import RTDetrV2ForObjectDetection, RTDetrImageProcessor
from PIL import Image
import torch

processor = RTDetrImageProcessor.from_pretrained("ogkalu/comic-text-and-bubble-detector")
model = RTDetrV2ForObjectDetection.from_pretrained("ogkalu/comic-text-and-bubble-detector")

image = Image.open("manga_page.png")
inputs = processor(images=image, return_tensors="pt")
with torch.no_grad():
    outputs = model(**inputs)

target_sizes = torch.tensor([(image.height, image.width)])
results = processor.post_process_object_detection(outputs, target_sizes=target_sizes, threshold=0.5)
# Classes: {0: "bubble", 1: "text_bubble", 2: "text_free"}
```

The 3-class RT-DETR model is especially interesting because it distinguishes bubble outlines from text-inside-bubbles from free text — this could replace both `bubble_detector.py` AND `text_detector.py` in a single inference pass.

#### 1c. comic-text-detector (DBNet + YOLOv5)

- **Repo**: https://github.com/dmMaze/comic-text-detector
- **Architecture**: DBNet (text segmentation) + YOLOv5 (text block detection)
- **Training data**: ~13,000 images (Manga109-s + DCM + synthetic)
- **Output**: Bounding boxes + text line segmentation + pixel masks
- **Weights**: https://github.com/zyddnys/manga-image-translator/releases/tag/beta-0.2.1
- Detects **text regions**, not bubbles. Useful as a complement, not replacement.

#### Phase 1 Action Plan

1. Download ogkalu YOLOv8m and RT-DETR-v2 models
2. Run both on `docs/shounen*.png` (10 pages) and `docs/adult*.png` (5 pages)
3. Compare detection results against current OpenCV detector output
4. Measure: recall (missed bubbles), precision (false positives), inference speed
5. If recall >= 80% on our test pages, proceed directly to integration

**Expected benefit**: Even without fine-tuning, these models were trained on 8-11K diverse comic images. They should handle the art style diversity that our hand-tuned thresholds struggle with. The 7-filter heuristic cascade and its magic numbers would be eliminated entirely.

### Phase 2: Fine-Tune with Paired Original/Translated Pages

If Phase 1 models miss bubbles specific to our manga/webtoon sources, fine-tuning on domain-specific data will close the gap.

#### Key Insight: Automatic Label Generation

Fan-translated manga pages are essentially **free annotations**. By diffing original Japanese pages against their English fan-translations:

```
Original page (JP text in bubbles)  <->  Fan-translated page (EN text in bubbles)
                    | image diff |
         Regions that changed = bubble locations (ground truth bboxes)
```

A structural diff (SSIM or pixel diff + morphological cleanup) automatically extracts:
- **Bubble bounding boxes** — regions where text was replaced
- **Bubble masks** — pixel-level contours of modified regions
- **Original text content** — the Japanese text that occupied those bubbles

This provides labeled training data with near-zero manual annotation.

#### Label Generator Script

```python
# Pseudocode for auto-labeling
def generate_labels(original_path, translated_path):
    orig = cv2.imread(original_path)
    trans = cv2.imread(translated_path)

    # Structural diff
    diff = cv2.absdiff(orig, trans)
    gray_diff = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray_diff, 30, 255, cv2.THRESH_BINARY)

    # Morphological cleanup
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=3)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)

    # Extract bounding boxes
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    labels = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if w * h > 500:  # minimum area
            # YOLO format: class cx cy w h (normalized)
            labels.append(yolo_format(0, x, y, w, h, img_w, img_h))
    return labels
```

#### Fine-Tuning Approach

Start from the ogkalu YOLOv8m weights (already domain-specific) rather than generic COCO:

```python
from ultralytics import YOLO
from huggingface_hub import hf_hub_download

# Start from domain-specific weights
model_path = hf_hub_download(
    repo_id="ogkalu/comic-speech-bubble-detector-yolov8m",
    filename="best.pt"
)
model = YOLO(model_path)

# Fine-tune on our paired data
results = model.train(
    data="bubbles.yaml",
    epochs=50,           # fewer epochs needed from domain baseline
    imgsz=1024,
    batch=8,
    patience=30,
    freeze=10,           # freeze backbone, train only detection head
    cos_lr=True,
    amp=True,
)
```

#### Training Data Requirements

- **Minimum**: ~200-500 labeled pages (fine-tuning from domain model needs less data)
- **Recommended**: ~1,500+ images per class for best results
- **Background images**: Include 5-10% pages with no bubbles (reduces false positives)
- Manual review pass to fix auto-label edge cases (~30 min for 200 pages)

#### Training Time Estimates

| GPU | Dataset | Model | Epochs | Time |
|-----|---------|-------|--------|------|
| RTX 4090 | 1,000 images | YOLOv8m | 100 | 1-2 hours |
| RTX 3090 | 1,000 images | YOLOv8m | 100 | 2-4 hours |
| RTX 3060 | 1,000 images | YOLOv8s | 100 | 4-8 hours |

### Phase 3: Unified Manga + Webtoon Model

A single YOLO model trained on both manga and webtoon data could:
- Detect bubbles regardless of format (black-and-white manga, color webtoon)
- Classify bubble type (speech, thought, narration, SFX) as separate YOLO classes
- Replace both `pipeline/bubble_detector.py` and `webtoon/bubble_detector.py`
- Handle new formats (manhwa, manhua) without new codepaths

---

## Training Data Sources

### Source 1: MangaDex API (Paired JP/EN Manga Pages)

**Best source for paired data.** REST API, no auth needed for reads, well-documented.

**API base**: `https://api.mangadex.org`
**Rate limit**: ~5 requests/second/IP
**Auth**: Not required for search, feeds, image downloads

#### Workflow

```python
import requests, time, os

BASE = "https://api.mangadex.org"
HEADERS = {"User-Agent": "FrankManga/1.0"}

# 1. Find manga with both JA and EN chapters
resp = requests.get(f"{BASE}/manga", headers=HEADERS, params={
    "originalLanguage[]": "ja",
    "availableTranslatedLanguage[]": "en",
    "limit": 10,
    "contentRating[]": ["safe", "suggestive", "erotica"],
})
manga_list = resp.json()["data"]

# 2. Get chapter aggregate for a manga (see which chapters exist in both languages)
manga_id = manga_list[0]["id"]
ja_agg = requests.get(f"{BASE}/manga/{manga_id}/aggregate",
    headers=HEADERS, params={"translatedLanguage[]": "ja"}).json()
en_agg = requests.get(f"{BASE}/manga/{manga_id}/aggregate",
    headers=HEADERS, params={"translatedLanguage[]": "en"}).json()

# 3. Match chapter numbers that exist in both languages
# Compare ja_agg["volumes"][vol]["chapters"] vs en_agg["volumes"][vol]["chapters"]
# Each chapter entry has an "id" (UUID) for downloading

# 4. Download pages from a chapter
chapter_id = "some-uuid"
server = requests.get(f"{BASE}/at-home/server/{chapter_id}", headers=HEADERS).json()
base_url = server["baseUrl"]
ch_hash = server["chapter"]["hash"]
for fname in server["chapter"]["data"]:
    # DO NOT send auth headers for image downloads
    img = requests.get(f"{base_url}/data/{ch_hash}/{fname}")
    # Save to disk
```

**Page alignment caveat**: JP and EN chapters are uploaded by different groups. Page counts may differ (combined pages, translator notes). Verify alignment with perceptual hashing or manual spot-check.

**Terms**: Must credit MangaDex and scanlation groups. No monetization. No hotlinking.

### Source 2: Webtoons (Paired KR/EN Webtoon Pages)

**Korean originals**: `comic.naver.com` (existing scraper at `webtoon/scraper.py`)
**English translations**: `webtoons.com`

#### Key Technical Details

| Platform | Image selector | CDN | Referer needed |
|----------|---------------|-----|----------------|
| Naver (KR) | `img.toon_image` | `image-comic.pstatic.net` | Yes |
| webtoons.com (EN) | `img._images` (`data-url` attr) | `webtoon-phinf.pstatic.net` | Yes |
| webtoons.com (EN) | same | `swebtoon-phinf.pstatic.net` | **No** (bypass) |

**CDN bypass**: Replace `webtoon-phinf` with `swebtoon-phinf` in image URLs to skip Referer requirement.

#### Episode Matching

There is **no shared ID** between Naver (`titleId`) and webtoons.com (`title_no`). Must maintain a manual mapping:

```yaml
# series_mapping.yaml
series:
  - name: "Tower of God"
    naver_title_id: "183559"
    webtoons_title_no: "95"
    episode_offset: 0

  - name: "Lookism"
    naver_title_id: "641253"
    webtoons_title_no: "1049"
    episode_offset: 0

  - name: "God of High School"
    naver_title_id: "318995"
    webtoons_title_no: "66"
    episode_offset: 0

  - name: "Noblesse"
    naver_title_id: "25455"
    webtoons_title_no: "87"
    episode_offset: 0
```

**Image strip alignment**: Within an episode, image strips are sequentially ordered and generally match 1:1 between Korean and English. Verify by comparing strip count and image dimensions. If strip counts differ, stitch into one tall image and re-split.

#### Validation

```python
import imagehash
from PIL import Image

def validate_pair(kr_path, en_path, max_hash_diff=15):
    kr_hash = imagehash.phash(Image.open(kr_path))
    en_hash = imagehash.phash(Image.open(en_path))
    diff = kr_hash - en_hash
    return diff <= max_hash_diff  # same artwork, different text
```

### Source 3: Manga109 Dataset (Pre-Annotated Ground Truth)

**147,918 text bounding box annotations** across 21,142 pages from 109 manga volumes. Academic use.

- **Access**: Apply at http://www.manga109.org/en/download.html (or Manga109-s on HuggingFace for commercial use)
- **Annotations**: Custom XML with `xmin, ymin, xmax, ymax` pixel coordinates + Unicode text content
- **Python API**: `pip install manga109api`
- **CVPR 2025 extension** (Xie et al.) adds 6-class segmentation masks including explicit **balloon** annotations

#### Converting to YOLO Format

```python
import manga109api

p = manga109api.Parser(root_dir="/path/to/Manga109")

for book in p.books:
    annotation = p.get_annotation(book=book)
    for page in annotation["page"]:
        page_w, page_h = page["@width"], page["@height"]
        labels = []
        for text in page.get("text", []):
            x_center = ((text["@xmin"] + text["@xmax"]) / 2) / page_w
            y_center = ((text["@ymin"] + text["@ymax"]) / 2) / page_h
            width = (text["@xmax"] - text["@xmin"]) / page_w
            height = (text["@ymax"] - text["@ymin"]) / page_h
            labels.append(f"0 {x_center:.6f} {y_center:.6f} {width:.6f} {height:.6f}")
        # Write to .txt file alongside image
```

### Source 4: Roboflow Datasets (Ready-to-Use YOLO Labels)

| Dataset | Images | Export formats |
|---------|--------|---------------|
| manga-bubble-detect | 4,492 | YOLO, COCO, VOC |
| manga-speech-bubble-detection | 816 | YOLO, COCO, VOC |

Available at https://universe.roboflow.com — can download directly in YOLO format with train/val/test splits.

### Source 5: SenManga (Not Recommended)

`raw.senmanga.com` has raw JP scans but no API, captcha protection, and fragile CDN URLs. **Skip in favor of MangaDex** which also hosts raw Japanese uploads via `translatedLanguage[]=ja`.

---

## Data Collection Plan

### Target: 1,000+ Paired Pages (Diverse Styles)

```
1. Pick 20-30 popular series across genres:
   - Shounen (action, fantasy)
   - Seinen (mature, realistic art)
   - Shoujo (romance, clean bubbles)
   - Webtoon (color, vertical scroll)

2. For each manga series (MangaDex):
   - Download 3-5 chapters in JA and EN
   - ~20 pages per chapter = ~60-100 pages per series
   - Verify page alignment with perceptual hashing

3. For each webtoon series (Naver + webtoons.com):
   - Download 3-5 episodes in KR and EN
   - ~30-50 image strips per episode
   - Verify strip alignment by count + dimensions

4. Auto-generate YOLO labels from paired diffs
5. Manual QA: review labels, fix edge cases (~2-3 hours)
6. Supplement with Roboflow manga-bubble-detect (4,492 images)
7. Split: 80% train, 10% val, 10% test
```

### Output Directory Structure

```
training_data/
  manga/
    series_name/
      chapter_001/
        ja_001.png        # Japanese original
        en_001.png        # English translation
        ja_001.txt        # Auto-generated YOLO labels
      chapter_002/
        ...
  webtoon/
    series_name/
      episode_001/
        kr_001.jpg        # Korean original
        en_001.jpg        # English translation
        kr_001.txt        # Auto-generated YOLO labels
      ...
  roboflow/                # Pre-labeled supplement
    images/
    labels/
  dataset.yaml             # YOLO dataset config
```

---

## Answer to Key Questions

### Q1: Can baseline YOLO replace the OpenCV cascade without fine-tuning?

**Yes, likely.** The ogkalu YOLOv8m model was trained on 8,000 diverse comic images including manga and webtoons. It should handle the art style diversity that our 7-filter heuristic cascade struggles with. The main risk is false negatives on unusual bubble styles — but even a 80% recall baseline eliminates the entire heuristic cascade, and the remaining 20% can be caught by fine-tuning.

The RT-DETR-v2 model (3 classes: bubble, text_bubble, text_free) could replace both `bubble_detector.py` AND `text_detector.py` in one pass, which would be a massive simplification.

### Q2: What does fine-tuning add on top of baseline YOLO?

Fine-tuning from the ogkalu baseline (already domain-specific) would:
- **Close the recall gap** on bubble styles specific to our target manga/webtoon sources
- **Reduce false positives** on art patterns common in our data but rare in the training set
- **Add multi-class detection** (speech vs thought vs narration vs SFX) if we label for it
- Require only ~200-500 labeled images (vs 1,500+ from scratch) since the backbone already knows "comics"

### Q3: Where to get paired training data?

| Source | Type | Volume | Effort |
|--------|------|--------|--------|
| **MangaDex API** | Paired JP/EN manga pages | Unlimited (thousands of series) | Script + spot-check |
| **Naver + webtoons.com** | Paired KR/EN webtoon strips | Hundreds of series | Extend existing scraper |
| **Manga109** | Pre-annotated (148K bboxes) | 21,142 pages, 109 volumes | Academic application |
| **Roboflow** | Pre-labeled YOLO data | 4,492 images | Direct download |

---

## Files That Would Change

| Current | After |
|---------|-------|
| `pipeline/bubble_detector.py` (550 lines) | `pipeline/bubble_detector.py` (~50 lines, YOLO wrapper) |
| `pipeline/text_detector.py` (387 lines) | Possibly unnecessary — RT-DETR detects all text regions |
| `webtoon/bubble_detector.py` (394 lines) | Merged into unified detector |
| `pipeline/config.py` (20+ thresholds) | Model path + confidence threshold |

## What Does NOT Change

- OCR pipeline (manga-ocr for Japanese, EasyOCR for Korean)
- Translation (Ollama qwen3:14b)
- Text rendering (text_renderer.py)
- Web service architecture (Go API + Redis + Python worker)
- Flutter client

## New Dependencies

```
ultralytics          # YOLOv8/v11 inference + training
huggingface_hub      # Model download
transformers         # RT-DETR-v2 (if using that model)
manga109api          # Manga109 dataset access (optional)
imagehash            # Pair validation (optional)
```
