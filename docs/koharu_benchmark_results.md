# Koharu benchmark results

The accepted findings are scheduled for staged implementation in the
[Koharu-inspired pipeline migration and v2 release plan](koharu_pipeline_migration_plan.md).

## Decision

Frank should adopt three Koharu capabilities while retaining its current product scope and fallbacks:

1. Add a multi-class instance-segmentation interface and evaluate a legally usable detector with Koharu's text, SFX, balloon, and panel semantics.
2. Add Hayai OCR as a fallback for difficult manga crops. Keep Manga OCR for ordinary Japanese dialogue and Frank's prose and furigana paths.
3. Keep LaMa and flat fills for routine cleanup, and add FLUX.2 Klein as an opt-in backend for difficult artwork. Do not adopt AOT or RORem as defaults.

The Koharu detector checkpoint is suitable as an internal quality baseline. It should not become a shipping dependency until its Manga109-based model terms receive legal review.

Koharu's page-batched translation, stable region identifiers, retained masks, and cheap-fill-before-inpaint flow remain direct implementation improvements. They do not require a product-level A/B test before implementation.

## Tested versions and hardware

- Frank commit: `e862dbc61568d6fa89cb9b88597caf2758bc8559`
- Koharu commit: `2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3`
- Host: Ryzen AI Max+ 395 with Radeon 8060S, `gfx1151`, and 64 GiB unified GPU memory
- Koharu runtime: ROCm 10.0.0 and Torch 2.13.0.7 HIP
- Frank runtime: PyTorch 2.9.1 and ROCm 7.2.1
- Corpus: 19 repository manga captures, 37 verified Japanese OCR crops, and 8 controlled inpainting cases

All inference completed on the Strix Halo server. Yomik remained healthy with both production workers registered throughout the benchmark.

## Detection and segmentation

### Runtime and coverage

Koharu completed all 19 pages, producing 786 detections and 786 instance masks. Cached end-to-end detector execution averaged 2.30 seconds per page. Frank's warmed RT-DETR bubble-only pass averaged about 0.08 seconds per page after its initial model load; this excludes Frank's much slower EasyOCR recovery pass.

The systems use different class meanings. Koharu predicts text, onomatopoeia, balloon, and panel instances separately. Frank combines text-container meanings into speech-bubble and artwork-text detections. Directly scoring class names against each other would misrepresent both systems.

Instead, the review matched any Frank translation region against Koharu text or SFX regions when either box center fell inside the other box, then visually inspected every unmatched crop:

| Measure | Count |
| --- | ---: |
| Frank regions | 290 |
| Koharu text or SFX regions | 367 |
| Matched one-to-one | 284 |
| Frank-only | 6 |
| Genuine Japanese text among Frank-only | 1 |
| Koharu-only | 83 |
| Genuine text or SFX among Koharu-only | 63 |
| False, duplicate, page, or interface regions among Koharu-only | 20 |

Koharu preserves nearly all useful regions found by Frank and materially increases SFX coverage. Its additional coverage has a cost: about one quarter of the unmatched additions were duplicate instances, page numbers, interface fragments, or artwork. Thresholding and deduplication remain necessary.

Frank and Koharu balloon masks were paired on 233 overlapping detections. Their mean mask agreement was 0.885 IoU and median agreement was 0.952. Visual review showed that both usually follow ordinary white balloon contours well. The main failures came from merged adjacent balloons, unusual containers, and mismatched semantic extents. Frank's connected-component heuristic performs better on ordinary monochrome balloons than the static review predicted, so replacing it solely for mask shape is not justified.

The detector improvement worth pursuing is the richer semantic layout and SFX coverage. Keep Frank's current detector as a fallback and retain its OCR validation, prose handling, and small-region recovery.

The detailed disagreement decisions are stored in [`evaluation/koharu_baseline/annotations/detection_disagreement_audit.json`](../evaluation/koharu_baseline/annotations/detection_disagreement_audit.json).

## OCR

The OCR set contains 37 verified Japanese crops: 19 ordinary and 18 difficult. Twenty-five crops had exact four-engine consensus and were visually spot-checked. The remaining crops were transcribed from the source image. One browser-toolbar false positive was excluded.

| Engine | Overall CER | Exact match | Difficult-crop CER | Difficult exact match |
| --- | ---: | ---: | ---: | ---: |
| Frank Manga OCR | 4.13% | 75.68% | 25.00% | 66.67% |
| Koharu Manga OCR | 3.95% | 75.68% | 23.68% | 66.67% |
| Koharu Baberu | 4.85% | 81.08% | 32.89% | 72.22% |
| Koharu Hayai | **1.80%** | **86.49%** | **5.26%** | **88.89%** |

Hayai reduced overall character error by 56% relative to Frank and difficult-crop error by 79%. It correctly recovered several short numeric, sign, and stylized crops that Manga OCR reordered or hallucinated. On ordinary dialogue, Frank and Koharu Manga OCR remained excellent and effectively equivalent. Baberu improved exact-match count but produced more severe errors on difficult crops.

Hayai should be added as a fallback selected for short, numeric, horizontally oriented, multilingual, or low-confidence Manga OCR results. Manga OCR should remain the default for ordinary vertical Japanese dialogue because Frank already keeps it warm and preserves specialized validation behavior.

## Inpainting

Eight controlled 384-pixel cases were created by overlaying Japanese glyphs on untouched manga crops and retaining the original crop as the clean reference. The mask contains only the synthetic glyphs plus a small dilation. This provides a known target for artwork reconstruction.

Across all eight cases:

| Backend | Mean masked MAE | Median masked MAE | Operational result |
| --- | ---: | ---: | --- |
| Frank LaMa | **29.14** | **24.29** | Best direct-model mean; about 0.04 seconds per warmed crop |
| Koharu LaMa | 29.55 | 27.36 | Equivalent quality, slower as a one-shot CLI |
| Koharu AOT | 30.55 | 28.37 | Won four individual cases but was less consistent |

The direct models remained weak on dense line art, halftones, and speed lines, so the four hardest cases were escalated:

| Backend | Mean masked MAE on hard cases | Cached one-shot time |
| --- | ---: | ---: |
| Frank LaMa | 36.84 | about 0.04 seconds after warmup |
| Koharu LaMa | 37.36 | about 1.6 seconds |
| Koharu AOT | 41.01 | about 1.3 seconds |
| Koharu RORem | 47.07 | about 16.4 seconds |
| Koharu FLUX.2 Klein | **25.75** | about 7.6 seconds |

FLUX.2 Klein lowered hard-case error by 30% relative to Frank LaMa and won all four difficult reference cases. Visual review agreed with the metric: it preserved hair, street details, speed lines, and dense artwork more faithfully. RORem was the worst tested option and does not justify its runtime cost.

The production cleanup policy should be:

1. Fill uniform balloon interiors directly.
2. Use warmed LaMa for ordinary textured regions.
3. Route only difficult artwork or failed LaMa results to FLUX.2 Klein.
4. Composite generated pixels strictly inside the removal mask.
5. Retain source, mask, backend, seed, and cleanup output in the page document.

## Translation and orchestration

No translation model contest was run. Koharu's advantage here comes from orchestration rather than a unique model: it translates a page as a structured batch with stable segment identifiers and optional page context. Frank's current per-bubble calls discard neighboring dialogue context.

Frank should implement page-batched translation with stable IDs, reading order, source text, prior translations, terminology, and optional source-image context. Existing llama-swap and Ollama models can serve that request; adopting Koharu's local LLM catalog is unnecessary.

## What Frank already does better

Frank should retain:

- Kindle and Naver integration inside the reader.
- Server-side queues, cache reuse, deduplication, and lightweight clients.
- Japanese prose-column handling and furigana.
- OCR validation and rejection of short hallucinations.
- Webtoon-specific loading, slicing, and scrolling behavior.
- Fast warmed LaMa cleanup for routine cases.

The benchmark contains no Korean webtoon ground truth, so it provides no basis for replacing Frank's webtoon detector or Korean OCR path.

## Evidence limits

- Detection used a complete visual disagreement audit rather than independently drawn boxes and polygon masks. It supports a product decision about coverage and semantics, but it is not a publication-grade precision and recall benchmark.
- The corpus consists of repository manga captures. Full-color webtoons require a separate labeled set.
- OCR truth combines four-engine consensus for unanimous cases with manual visual transcription for disagreements.
- Inpainting uses controlled synthetic overlays with clean references. Real text-removal quality should continue to receive spot review because the hidden artwork behind native text is unknowable.
- Timings mix warmed services and one-shot CLIs. They guide routing and capacity decisions, not production throughput forecasts.

These limits do not change the implementation priorities: Hayai has a measured OCR advantage on difficult crops, FLUX.2 Klein has a measured advantage on difficult artwork, and Koharu's semantic detector output finds useful SFX that Frank currently misses.
