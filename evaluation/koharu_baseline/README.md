# Frank/Koharu comparison baseline

This directory contains a focused, reproducible comparison of the processing stages whose relative quality is still uncertain on Frank's actual inputs.

It is deliberately separate from unit and integration tests:

- Running it can download large model weights and consume substantial GPU memory.
- Purchased manga and authenticated reader captures remain ignored local assets.
- It compares pinned model implementations without adding Koharu packages to Frank's worker image.
- Generated outputs and the identity mapping for blind reviews are ignored by Git.

No command runs automatically under `pytest`, Go tests, Flutter tests, or release checks.

The homelab Strix Halo host is a validated runner. The detector, Manga OCR, Baberu, Hayai, LaMa, and AOT completed GPU smoke tests there. See [the remote execution assessment](REMOTE_STRIX_HALO.md) for the measured results, capacity, model download sizes, and safe execution gate.

The completed comparative results and adoption decisions are in [`docs/koharu_benchmark_results.md`](../../docs/koharu_benchmark_results.md). The tracked metadata now includes 37 verified OCR crops, 8 controlled reference-based inpainting cases, and the full detection disagreement audit. Private images and generated outputs remain ignored.

## What should be compared

### 1. Detection and segmentation

**Uncertain claim:** Koharu's RF-DETR instance segmentation produces better balloon, text, and SFX regions than Frank's RT-DETR boxes plus mask heuristics on the content Frank actually processes.

Evidence:

- Labeled monochrome manga pages.
- Labeled full-color manga pages.
- Labeled Korean webtoon pages.
- Per-class precision, recall, F1, box IoU, and mask IoU.
- Separate reporting by page kind so strong manga results cannot hide webtoon regressions.

The first decision threshold is balloon recall and mask IoU. Text and SFX scores explain downstream behavior, but they must not obscure balloon quality.

### 2. OCR recognition

**Uncertain claim:** One of Koharu's recognizers improves Japanese or Korean transcription after both systems receive the exact same crop.

Evidence:

- Human-verified text crops, independent of either detector.
- Normalized character error rate.
- Exact-match rate.
- Results separated by language and by ordinary dialogue versus difficult typography.

This isolates recognition from detection. A separate end-to-end pass can later measure the effect of detector crops.

### 3. Inpainting model quality

**Uncertain claim:** Koharu's AOT, RORem, FLUX.2 Klein, or LaMa implementation reconstructs textured balloons and artwork more acceptably than Frank's current backends when given the same source and binary mask.

Evidence:

- Identical source crop and mask for every model.
- Fixed seed for generative models where supported.
- Model output composited only inside the evaluation mask.
- Masked pixel error when a clean reference exists.
- Randomized blind human review on real pages.
- Inference time recorded as a secondary measure.

Real manga artwork usually has no known clean image behind the lettering. No-reference scores are not used to decide image quality because they reward smooth but invented output. The blind review is the decision evidence for those cases.

## What should be implemented without an A/B test

These findings already have a direct technical justification and are outside this benchmark:

- Fix Frank's left-to-right manga detection sort.
- Batch page translation with stable region IDs and page context.
- Add per-book terminology and translation instructions.
- Preserve masks, reading order, typography, and provenance in cache metadata.
- Fill nearly uniform balloon interiors before invoking an inpainting model.
- Tile large cleanup masks with surrounding context.
- Add an optional review interface after the page document exists.

Translation model quality is not a Koharu-versus-Frank property. Koharu exposes more providers, while Frank currently uses one local model and a fallback. The useful Koharu improvement is page-level translation orchestration, which can be implemented directly and then covered by product-specific translation evaluation.

Koharu's studio UI, project management, general canvas, PSD workflow, and broad provider catalog are outside Frank's goals.

## Directory layout

```text
evaluation/koharu_baseline/
  config.json                 pinned repositories, binaries, and thresholds
  config.local.json           optional ignored machine overrides
  dataset.json                case index without copyrighted content
  assets/                     ignored private inputs
    pages/                    full pages for detection
    ocr/                      exact recognition crops
    inpainting/               source, mask, and optional clean reference
  annotations/
    detection.json            instance boxes and mask references
    ocr.json                  verified transcriptions
    inpainting.json           case descriptions and reference status
    masks/                    ignored ground-truth instance masks
  outputs/                    ignored inference results grouped by run ID
  reviews/
    blind/                    randomized candidates and rating forms
    private/                  ignored candidate-to-system mappings
  scripts/
    benchmark.py              preparation, execution, scoring, blind review
    frank_adapter.py          stable file contract around Frank stages
```

## Safety boundary

Only this command runs model inference:

```bash
python scripts/benchmark.py run STAGE ...
```

`prepare`, `add-ocr`, `add-inpainting`, `validate`, `build-commands`, `score`, and `blind` do not initialize a model or CUDA. `build-commands` prints a command; it does not compile Koharu.

## One-time preparation

Run commands from this directory.

### 1. Seed the local corpus

Copy the existing repository manga samples into the ignored asset directory:

```bash
python scripts/benchmark.py prepare --seed-docs
```

Add a private page set without committing it:

```bash
python scripts/benchmark.py prepare --add-pages /path/to/kindle-pages --kind manga
python scripts/benchmark.py prepare --add-pages /path/to/naver-pages --kind webtoon
```

The command records dimensions and SHA-256 hashes in `dataset.json`, copies the files under `assets/pages/`, and creates empty detection annotation entries.

Recommended minimum corpus:

| Group | Minimum | Important cases |
| --- | ---: | --- |
| Monochrome manga | 30 pages | conventional, borderless, open, edge-touching, narration, dense panels |
| Full-color manga | 15 pages | colored balloons, gradients, dark backgrounds, decorative borders |
| Korean webtoon | 30 crops/pages | long strips, translucent boxes, text on artwork, stylized SFX |

Use a held-out subset when tuning confidence thresholds. Do not tune and report final metrics on the same pages.

### 2. Label detection ground truth

For each page in `annotations/detection.json`, replace the empty entry with:

```json
{
  "annotation_status": "verified",
  "instances": [
    {
      "id": "bubble-001",
      "class": "bubble",
      "bbox": [120, 80, 540, 610],
      "mask": "annotations/masks/manga__page__bubble-001.png",
      "ignore": false
    },
    {
      "id": "text-001",
      "class": "text",
      "bbox": [220, 140, 420, 520],
      "mask": "annotations/masks/manga__page__text-001.png",
      "ignore": false
    }
  ]
}
```

Coordinates use `[left, top, right, bottom]`, with right and bottom exclusive. Masks are page-sized single-channel PNGs with zero for background and any value of 127 or greater for the instance.

Allowed classes are `bubble`, `text`, `onomatopoeia`, and `panel`. Set `ignore` for ambiguous regions that should not count as a false negative. Label the visible semantic region rather than copying either model's output.

The scorer matches predictions to ground truth greedily by confidence at box IoU 0.5, then reports mask IoU for matched instances that have masks on both sides.

### 3. Add verified OCR crops

Use crops that contain one logical text region. Record the transcription exactly as a fluent reader sees it:

```bash
python scripts/benchmark.py add-ocr \
  --id ja_dialogue_001 \
  --image /path/to/crop.png \
  --language ja \
  --text '今日はどうしたの？' \
  --verified
```

For Korean, use `--language ko`. Include at least:

- 50 ordinary Japanese dialogue crops.
- 20 difficult Japanese crops: small type, reversed colors, furigana, or textured background.
- 30 ordinary Korean dialogue crops.
- 20 Korean narration or difficult typography crops.

Two readers should resolve uncertain transcriptions before marking them verified.

### 4. Add inpainting cases

Each input is a bounded crop, and the mask has identical dimensions:

```bash
python scripts/benchmark.py add-inpainting \
  --id artwork_001 \
  --image /path/to/source.png \
  --mask /path/to/removal-mask.png \
  --kind artwork \
  --description 'SFX over hair and speed lines'
```

When a clean reference exists, add `--reference /path/to/clean.png`. Synthetic cases can be created by starting from a clean crop, drawing source text over it, and retaining the clean crop as the reference. Keep real and synthetic results separate during interpretation.

Include flat balloons, textured balloons, artwork text, and SFX. Flat balloons mainly verify that model inference is unnecessary; the consequential cases are gradients, transparency, line art, hair, faces, and motion effects.

### 5. Validate without GPU use

```bash
python scripts/benchmark.py validate
python scripts/benchmark.py validate --require-labels
```

The strict form fails on unlabeled pages and unverified OCR text.

## Prepare Koharu binaries

The suite expects the existing checkout at `~/Projects/koharu` and commit `2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3`. Override paths in ignored `config.local.json` if needed.

Print the required build command:

```bash
python scripts/benchmark.py build-commands
```

Review it, move to the Koharu checkout, verify the commit, and run it when GPU and network use are acceptable. Building can initialize a large dependency graph; the first inference for each model can download weights.

The printed sequence temporarily patches Koharu's detector result serialization to include instance masks. Koharu's normal CLI omits its internal mask field from JSON. After building, the sequence restores the source file while leaving the release binary available to the harness. This evaluation-only patch changes serialization and does not change inference or postprocessing.

The harness invokes release binaries directly so later comparison runs do not include Cargo compilation time.

## Run later, when the GPU is free

Use a stable run ID for related systems.

### Detection

```bash
python scripts/benchmark.py run detection --run-id detection-v1 --system frank
python scripts/benchmark.py run detection --run-id detection-v1 --system koharu
python scripts/benchmark.py score detection --run-id detection-v1
```

Limit an exploratory run to named cases with repeated `--case`:

```bash
python scripts/benchmark.py run detection \
  --run-id smoke-detection \
  --system koharu \
  --case manga__adult \
  --case manga__shounen
```

Frank's normalized detection output includes its RT-DETR balloon and free-text detections plus its Japanese EasyOCR text regions. Speech balloons receive Frank's current connected-component mask. Koharu output is normalized from its JSON boxes and instance masks.

### OCR

Run only engines relevant to the case language when interpreting results:

```bash
python scripts/benchmark.py run ocr --run-id ocr-v1 --system frank
python scripts/benchmark.py run ocr --run-id ocr-v1 --system koharu:manga_ocr
python scripts/benchmark.py run ocr --run-id ocr-v1 --system koharu:baberu_ocr
python scripts/benchmark.py run ocr --run-id ocr-v1 --system koharu:hayai_ocr
python scripts/benchmark.py run ocr --run-id ocr-v1 --system koharu:paddle_ocr_vl
python scripts/benchmark.py score ocr --run-id ocr-v1
```

Koharu's Manga OCR and Frank's Manga OCR may be the same underlying model with different runtimes. Equivalent output is expected and should not justify a migration by itself.

### Inpainting

Use identical cases and masks for each backend:

```bash
python scripts/benchmark.py run inpainting --run-id inpaint-v1 --system frank:lama
python scripts/benchmark.py run inpainting --run-id inpaint-v1 --system koharu:lama
python scripts/benchmark.py run inpainting --run-id inpaint-v1 --system koharu:aot
python scripts/benchmark.py run inpainting --run-id inpaint-v1 --system koharu:rorem
python scripts/benchmark.py run inpainting --run-id inpaint-v1 --system koharu:flux2_klein
python scripts/benchmark.py score inpainting --run-id inpaint-v1
python scripts/benchmark.py blind --run-id inpaint-v1
```

Open `reviews/blind/inpaint-v1/index.html` and enter scores in its `ratings.json`. Do not open `reviews/private/inpaint-v1.mapping.json` until ratings are complete.

After completing the form, reveal and aggregate the system scores:

```bash
python scripts/benchmark.py score inpainting \
  --run-id inpaint-v1 \
  --ratings reviews/blind/inpaint-v1/ratings.json
```

Score candidates from 1 to 5 for:

- Removal completeness.
- Artwork preservation.
- Balloon or region boundary preservation.
- Texture consistency.
- Overall acceptability for reading.

Use at least two reviewers for a decision that would add a large runtime dependency.

## Decision rules

### Detection

Adopt or reproduce Koharu's segmentation approach only if it:

- Improves balloon recall without a material precision regression.
- Improves balloon mask IoU and reduces artwork removal.
- Does not regress the webtoon subset beyond an agreed tolerance.
- Has acceptable latency and memory for the worker.
- Can be shipped under acceptable model and training-data terms.

A model can win for manga and lose for webtoons. Frank can keep separate detectors when the evidence supports it.

### OCR

Change the default only when the candidate lowers character error rate on verified crops and does not create a material latency or memory penalty. A specialized engine can be selected by language or region type instead of becoming one global default.

### Inpainting

Prefer the smallest and fastest backend that reviewers find acceptable. A higher-capacity generative model should win clearly on consequential artwork cases before it is added to production. Flat balloons should bypass model inference.

## Reproducibility notes

- `config.json` pins the reviewed Koharu commit and deterministic generative seed where supported.
- Every copied input records a SHA-256 hash.
- Output directories contain elapsed times, stdout, stderr, normalized results, and a run manifest with both commit IDs.
- Each case runs in an isolated process, so elapsed time includes a cold model load and is diagnostic rather than a production throughput benchmark. After a quality winner emerges, profile that candidate in a warmed worker before adoption.
- Model caches are intentionally outside this directory and are not copied or committed.
- Threshold tuning requires a development split. Final numbers must come from held-out pages.
- The detector weights have custom training-data terms. Benchmark success does not by itself authorize redistribution or production use.
