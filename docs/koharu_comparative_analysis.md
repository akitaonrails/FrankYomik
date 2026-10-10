# Koharu Comparative Analysis

> **Historical baseline:** This code inventory predates the Phase 0 pipeline
> contracts. The measured comparison and product recommendations remain the
> basis for the migration. For the current PageDocument, cache, and runtime
> design, see [Pipeline architecture](pipeline_architecture.md). Progress is
> tracked in the [pipeline migration plan](koharu_pipeline_migration_plan.md).

## Purpose and scope

This document compares Frank Yomik with [Koharu](https://github.com/koharu-rs/koharu), an open-source desktop manga translation and editing application. It records which capabilities are comparable, where each product is stronger, and which Koharu ideas are useful for Frank.

The comparison was performed on October 9, 2026 using:

- Koharu commit [`2c78c0cd`](https://github.com/koharu-rs/koharu/commit/2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3), checked out locally for static source inspection.
- Frank Yomik commit `e862dbc61568d6fa89cb9b88597caf2758bc8559`.
- Koharu's public documentation and detector model card.

The initial source review was followed by an executed comparison on the Strix Halo server. See [Koharu benchmark results](koharu_benchmark_results.md) for the measured detector, OCR, mask, and inpainting results and final adoption decisions.

The staged implementation and major-release gates are in the [Koharu-inspired pipeline migration and v2 release plan](koharu_pipeline_migration_plan.md).

## Executive assessment

Koharu and Frank overlap in detection, OCR, translation, cleanup, and rendering, but they serve different jobs.

- **Koharu is a manga localization studio.** Users import images, archives, or PDFs, process a project, correct the results, typeset pages, and export production assets.
- **Frank is a live reading companion.** It captures pages displayed by Kindle Japan or Naver Webtoon, processes them on a server, and reveals the result inside the original reader.

Frank is substantially behind Koharu as an editable manga localization system. Koharu has stronger region segmentation, cleanup, proofreading, typesetting, and export. Frank remains differentiated as a low-friction reading product and is stronger in live service integration, furigana, Japanese prose, mobile use, tall webtoons, prefetch, and centralized processing.

The best product direction is to preserve Frank's live reading experience while adopting the following ideas from Koharu:

1. Ground-truthed quality benchmarks.
2. Instance segmentation instead of boxes plus inferred masks.
3. A durable intermediate page document.
4. Page-batched, context-aware translation.
5. Mask-aware cleanup with cheap flat fills before inpainting.
6. An optional correction and export workflow.

## Capability summary

| Area | Assessment | Reason |
| --- | --- | --- |
| Manga balloon and text detection | Koharu ahead in semantics and SFX coverage | The executed audit found 63 genuine Koharu-only text or SFX regions while Koharu missed one genuine Frank-only Japanese sign. False additions and merged instances still require filtering. |
| Removal masks | Comparable on ordinary monochrome balloons | Across 233 paired detections, Frank and Koharu masks had 0.885 mean and 0.952 median IoU. Koharu's native masks remain architecturally cleaner. |
| OCR | Hayai ahead on difficult crops | Hayai achieved 1.80% overall CER and 5.26% difficult-crop CER, versus Frank Manga OCR at 4.13% and 25.00%. Manga OCR remains suitable for ordinary dialogue. |
| Translation | Koharu ahead | It batches page segments, preserves IDs, supports optional page images, custom instructions, local models, and many hosted providers. |
| Cleanup and inpainting | Mixed; Koharu FLUX wins difficult artwork | Frank LaMa slightly won the eight-case direct-model mean. FLUX.2 Klein reduced error by 30% on the four hardest cases. Koharu's cleanup orchestration is stronger. |
| Typesetting | Koharu ahead generally | It has retained typography, shaping, font fallback, vertical CJK, right-to-left text, and editable layout. |
| Immediate reading layout | Mixed | Frank has specialized balloon-aware rendering, page-wide font normalization, SFX handling, furigana, and webtoon composition. |
| Proofreading and correction | Koharu far ahead | Detection, OCR, translation, typography, masks, and artwork remain independently editable. |
| Layered PSD export | Koharu far ahead | It writes original, cleanup, removal-mask, image, and editable text layers. |
| Kindle and Naver integration | Frank ahead | Frank operates in the original reader and handles dynamic pages, gestures, capture, binding, and prefetch. |
| Furigana and Japanese prose | Frank ahead | Frank has manga furigana and a dedicated vertical prose column and gutter pipeline. |
| Tall webtoons | Frank ahead operationally | Frank understands Naver's loading and scrolling model and has a dedicated tall-image workflow. |
| Shared service architecture | Frank ahead | Redis queues, deduplication, worker recovery, content-addressed cache, and WebSocket completion support lightweight clients. |
| Local privacy and offline ownership | Koharu ahead | Projects and inference can stay on the user's workstation. |

## Koharu architecture and workflow

Koharu is a Rust desktop application with a retained project model and a WebGPU canvas. Its processing stages are:

```text
Import
  |
Detection --------> Inpainting
  |
OCR
  |
Translation
  |
Review / cleanup / typesetting
  |
PNG or layered PSD
```

Stages can run on the current page, selected pages, selected layers, or an entire project. Only one processing job runs at a time. Each stage commits its result, so completed work survives a later failure. Detection skips pages that already contain text regions; users must delete those regions before rerunning detection. See Koharu's [processing guide](https://github.com/koharu-rs/koharu/blob/2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3/packages/docs/en/guides/processing.mdx).

Koharu stores source images and structured scene state in `.khrproj` project directories. Its scene separates regions, text content, translations, typography, raster layers, assets, provenance, relations, and revisions. The canvas and exporters resolve the same scene instead of building separate representations for preview and output.

### Detection and layout analysis

Koharu uses a pinned RF-DETR Seg 2XL checkpoint through its own Rust inference implementation. The checkpoint reference is fixed in [the model loader](https://github.com/koharu-rs/koharu/blob/2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3/crates/koharu-ml/src/koharu_layout_rfdetr_seg_2xl/mod.rs#L28-L31).

The model predicts four classes:

- Text
- Onomatopoeia and sound effects
- Speech or text balloons
- Panels

Each detection contains a bounding box, confidence, and instance mask. The application then:

1. Applies class-specific confidence thresholds.
2. Applies nonmaximum suppression.
3. Builds panel, balloon, and text containment relationships.
4. Orders content top to bottom and right to left within the inferred layout.
5. Converts balloon masks into polygon geometry.
6. Links a text region to the smallest balloon containing at least 90% of its mask.
7. Infers text rotation, writing direction, fill color, stroke color, and stroke width from source pixels.
8. Produces separate regular-text and SFX removal masks.

The relationship and mask handling are implemented in [the detection stage](https://github.com/koharu-rs/koharu/blob/2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3/crates/koharu-pipeline/src/stages/detection.rs#L270-L288) and [text-to-balloon linking](https://github.com/koharu-rs/koharu/blob/2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3/crates/koharu-pipeline/src/stages/detection.rs#L435-L464).

The model itself does not provide OCR, reading order, or semantic text-to-balloon relationships. Koharu derives those in application code.

#### Published detector evidence

The [official model card](https://huggingface.co/mayocream/koharu-layout-rfdetr-seg-2xl-1152) reports evaluation on a held-out 1,001-page TextSeg-refined Manga109 validation split at 1152 pixels:

| Metric | Score |
| --- | ---: |
| Box mAP50-95 | 0.7969 |
| Box mAP50 | 0.8970 |
| Box mAP75 | 0.8425 |
| Mask mAP50-95 | 0.5713 |
| Mask mAP50 | 0.8256 |
| Detection precision | 0.8767 |
| Detection recall | 0.8177 |
| Detection F1 | 0.8392 |

Per-class box AP is 0.8779 for text, 0.4431 for SFX, 0.9092 for balloons, and 0.9574 for panels.

The evidence has important boundaries:

- Training data is predominantly Japanese manga.
- SFX is materially weaker than text, balloons, and panels.
- Covers, credits, decorative marks, logos, and dense collages remain difficult.
- Full-color Blue Archive and Marriage Toxin reviews were qualitative because those sets lacked ground truth.
- There is no direct evaluation on Kindle captures, Korean webtoons, or Frank's current corpus.

The architecture and published Manga109 results make a meaningful advantage over Frank likely for conventional monochrome manga. A controlled benchmark is still required before concluding that it improves Frank's downstream output or works better on webtoons.

### Frank's current detection path

Frank uses `ogkalu/comic-text-and-bubble-detector`, an RT-DETR-v2 object detector. It maps both `bubble` and `text_bubble` to `speech_bubble`, maps `text_free` to artwork text, and deduplicates overlapping boxes. See [`server/kindle/bubble_detector.py`](../server/kindle/bubble_detector.py).

The detector produces boxes rather than instance masks. For a speech balloon, Frank:

1. Expands the detected box.
2. Converts the crop to grayscale.
3. Thresholds pixels brighter than 200.
4. Chooses a large connected component, preferring one containing the box center.
5. Converts its largest contour to a page-sized mask.
6. Falls back to a filled bounding rectangle when no suitable component is found.

This is vulnerable to dark or colored balloons, borderless balloons, balloons touching page edges, decorative frames, multiple bright components, and detections containing artwork. A rectangular fallback can remove or overwrite pixels that were never part of the balloon.

Frank compensates with useful target-specific heuristics:

- EasyOCR-based text-region recovery.
- Vertical text stroke clustering.
- OCR validation.
- Small-balloon recovery.
- A second Manga OCR crop for rejecting short hallucinations on faces or artwork.
- Higher confidence requirements for SFX and narration because false positives damage artwork.

These safeguards remain useful even after adopting segmentation.

#### Reading-order defect

The detector comment says it sorts right to left, but the current key is:

```python
detections.sort(key=lambda d: (d["bbox"][1], d["bbox"][0]))
```

This sorts ascending by the left coordinate for detections with similar top coordinates, which is left to right. The code is in [`server/kindle/bubble_detector.py`](../server/kindle/bubble_detector.py#L115-L116).

The issue should be corrected independently of any detector replacement. A robust solution should group vertically overlapping items or use panel containment before applying right-to-left order. A simple negated `x` coordinate fixes the direction but not multi-panel layouts.

### OCR

Koharu offers four selectable OCR processors:

| Processor | Intended use |
| --- | --- |
| PaddleOCR VL 1.6 | General vision-language OCR |
| Manga OCR | Japanese manga text |
| Baberu OCR | Alternative manga-oriented recognition |
| Hayai OCR | Chinese, Korean, Japanese, and English manga text |

OCR output is stored separately from geometry and remains editable. Missing detections cannot be recovered by changing OCR because only detected regions are sent to recognition.

Frank uses Manga OCR for Japanese and EasyOCR in other paths. Frank has less recognizer choice but has useful specialization:

- Short Japanese results are compared with a center crop to detect hallucination.
- Prose pages are split into OCR-sized vertical column slices.
- Character positions are retained so furigana can be placed beside the correct source text.

Koharu is stronger in recognizer breadth and correction workflow. Frank's validation and prose logic should be retained.

### Translation

Koharu collects every eligible text layer on a page into one structured translation request. It preserves segment identity and order, allows custom instructions, and can include the source page for a vision-capable model. The implementation is in [the translation stage](https://github.com/koharu-rs/koharu/blob/2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3/crates/koharu-pipeline/src/stages/translation.rs#L37-L68).

Its supported backends include:

- Local GGUF models through llama.cpp.
- OpenAI, Gemini, Claude, Grok, MiniMax, DeepSeek, and OpenRouter.
- LM Studio and custom OpenAI-compatible endpoints.
- DeepL, Google Cloud Translation, and Caiyun.

Frank translates one bubble at a time through Ollama. It supports English and
Brazilian Portuguese. The worker verifies the pinned Ollama digest before use;
translation errors fail the job instead of being saved as Qwen output from a
different backend. The prompt correctly emphasizes natural dialogue, tone,
Japanese names and honorifics, brevity, and bubble space. See
[`server/kindle/translator.py`](../server/kindle/translator.py) and the
[pipeline stage manifest](pipeline_stage_manifest.md).

Koharu's page batch gives the model more context for names, pronouns, connected dialogue, and tone. Frank's independent calls cannot see neighboring bubbles or the page image. Koharu still lacks automatic prior-page context in the processing stage, and its translation instructions are global rather than project-specific.

### Inpainting and cleanup

Koharu treats cleanup as a normal editable pipeline stage. It supports:

- LaMa
- AOT Inpainting
- FLUX.2 Klein
- RORem Mixed

Before model inference, Koharu detects nearly uniform balloon interiors and fills them directly from surrounding color. Remaining removal-mask components are grouped into bounded 512-pixel tiles with 128 pixels of surrounding context. This reduces model work and gives each reconstruction useful neighboring pixels. See [the inpainting implementation](https://github.com/koharu-rs/koharu/blob/2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3/crates/koharu-pipeline/src/stages/inpainting.rs#L519-L672).

Users can paint additional removal masks, repair artwork, and rerun cleanup for selected areas.

Frank includes LaMa for manga and LaMa, Flux Fill, and SDXL backends for webtoons. Inpainting is optional and disabled by default. Speech balloons usually use stroke clearing or solid filling. The models exist, but cleanup is less integrated and users cannot correct masks interactively.

The most transferable Koharu improvement is the decision process around inpainting:

1. Use an accurate text mask.
2. Fill uniform balloon interiors cheaply.
3. Inpaint only textured regions and artwork.
4. Bound work to mask components with enough context.
5. Preserve masks for correction and reprocessing.

### Scene model and correction workflow

Koharu's most consequential advantage is its durable intermediate representation. Detection regions, source text, translations, typography, masks, cleanup layers, image layers, relations, provenance, and revisions remain separate project data.

This allows users to:

- Correct OCR without repeating detection.
- Correct translation without repeating OCR.
- Move, resize, add, delete, or reorder text.
- Change font, alignment, writing direction, fill, and stroke.
- Edit removal masks.
- Paint repairs or add images.
- Undo changes.
- Re-render and export the same retained state.

At the time of this analysis, Frank's flat cache metadata was only a partial
foundation. Phase 0 has since introduced the compact
[PageDocument v2](page_document_v2.md), which can represent reading order,
polygons, external masks, relationships, structured stage results, rendering
metadata, and provenance. Current backends populate the fields they can
produce; later migration phases will retain richer masks and relationships.

### Text layout and rendering

Koharu has a general retained text renderer with:

- Multilingual shaping.
- Font fallback.
- Automatic fitting.
- Vertical CJK.
- Right-to-left text.
- Fill and stroke.
- Editable positioning and orientation.

Canvas, PNG, and PSD start from the same prepared scene, which reduces output drift.

Frank's renderer has valuable specialized behavior:

- Balloon mask-aware fitting.
- Page-wide font-size normalization.
- Horizontal translated manga dialogue.
- Vertical SFX rendering.
- Source color sampling.
- Mask clipping.
- Manga furigana.
- Japanese prose furigana placed in gutters.
- Tall webtoon splitting and composition.

Koharu is stronger as a general authoring renderer. Frank's specialization is well aligned with immediate reading and should not be discarded.

### PSD export

Koharu has a native PSD writer and emits editable Photoshop text resources. Its PSD contains:

- The original image.
- A hidden combined text-removal mask.
- One layer for each cleanup or paint layer.
- Embedded image layers.
- One editable text layer for each translated text item.

Koharu does **not** export every detected balloon as a separate balloon layer. The useful layers are cleanup, masks, images, and translated text. The scene projection is implemented in [the PSD document builder](https://github.com/koharu-rs/koharu/blob/2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3/crates/koharu-psd/src/document.rs#L88-L177).

Known limitations documented by Koharu:

- Per-layer visibility and opacity are not fully written to PSD, although the merged preview honors them.
- Classic PSD dimensions cannot exceed 30,000 pixels on either side.
- OCR semantics, provenance, revision history, and complete renderer behavior remain in `.khrproj` rather than PSD.
- There is no separate inpainted-image-only export.

See the [export guide](https://github.com/koharu-rs/koharu/blob/2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3/packages/docs/en/guides/export.mdx).

## Where Frank is stronger

### Live Kindle and Naver reading

Frank watches dynamic pages in the actual Kindle and Naver readers, captures blob-backed or authenticated images, submits them to the server, and binds completed renders to the correct source page. It handles reader navigation, reused image elements, lazy loading, scrolling, page-turn gestures, cancellation, and prefetch.

The press-and-hold lens keeps the original page visible and reveals the processed page only under the pointer. It protects normal taps, page turns, swipes, and webtoon scrolling. It also checks a completed render against the displayed page before binding it, reducing the risk of showing a translation for the wrong page.

Koharu requires importing content into a project and running an authoring workflow. Frank provides a shorter path from encountering an unreadable page to reading it.

### Furigana and Japanese prose

Frank has first-class modes for:

- Adding furigana to manga dialogue without translating it.
- Detecting regular vertical prose columns.
- Splitting long columns into Manga OCR-sized slices.
- Estimating character positions.
- Avoiding existing publisher ruby.
- Placing generated readings in the correct gutters.

This supports Japanese learners and Kindle books, not only manga localization. Koharu's layout model is trained for manga page elements and does not provide an equivalent prose workflow.

### Webtoon operations

Frank understands Naver's page discovery and lazy-loading behavior, can prefetch episode images, and is designed around long scrolling content. Koharu can import raster images, but its detector is predominantly trained on Japanese manga and its PSD exporter has a 30,000-pixel side limit.

There is not enough evidence to claim Koharu's detector is better for Korean full-color webtoons. Frank's dedicated flow remains a product advantage even if its segmentation is later upgraded.

### Service architecture

Frank provides:

- Lightweight Flutter and Chromium clients.
- Token-protected API access.
- Redis Streams job delivery.
- Deduplication by image, pipeline, and target language.
- Recovery of jobs abandoned by crashed workers.
- Content-addressed source and result storage.
- WebSocket completion notifications with polling fallback.
- Centralized GPU inference.

This allows users to read from devices without installing multiple local models or maintaining a compatible workstation GPU. Koharu's local execution provides privacy and offline control, but it transfers hardware and model management to each user.

## Koharu limitations and adoption risks

### Detector license and training data

Koharu's application code is dual-licensed under MIT or Apache 2.0. Its detector weights are separate.

The detector model card uses a custom `other` license because it was trained using Manga109 images distributed under academic-use terms. It states that users are responsible for obtaining Manga109 and complying with its terms and applicable rights. The model card does not grant rights to Manga109 source images or third-party comics.

Consequences for Frank:

- Koharu's application code and architecture can be studied and reused subject to their software licenses.
- The detector weights should not become a shipping or commercial dependency without legal review.
- A production detector should use a model and training corpus with documented redistribution and product-use rights.
- The Koharu checkpoint can still be valuable as an internal research baseline if its terms permit the intended evaluation.

See the [model card's licensing section](https://huggingface.co/mayocream/koharu-layout-rfdetr-seg-2xl-1152#license-and-training-data-terms).

### Runtime and maintenance cost

Koharu supports local Torch detection and OCR, llama.cpp translation, diffusion models, WebGPU rendering, and several GPU backends in a cross-platform desktop application. This gives users broad control but creates a large build, packaging, driver, download, and support surface.

Frank should avoid copying this complete stack. Its centralized service and lightweight clients are simpler for readers and make model upgrades easier to operate.

### Workflow limitations

- Only one processing job runs at a time.
- Detection does not rerun on a page containing any existing text region unless those regions are deleted.
- Translation instructions are global, not project-specific.
- Project-level glossary and automatic previous-page context are not clearly integrated into the processing stage.
- OCR crops appear to use axis-aligned geometry even when rotation is inferred separately.
- SFX detection has much weaker published AP than balloon and regular-text detection.
- The detector's published out-of-domain evidence is qualitative.

## Recommended roadmap

The runnable evidence path for the uncertain comparisons lives in [`evaluation/koharu_baseline/`](../evaluation/koharu_baseline/README.md). It isolates private assets and GPU-heavy model runs from the ordinary test suite.

### Priority 0: Build a representative detection benchmark

Before replacing a model, label a representative evaluation corpus containing:

- Monochrome Kindle manga.
- Full-color manga.
- Korean webtoons.
- Borderless, open, colored, and dark balloons.
- Balloons touching page edges.
- Narration boxes.
- Small and decorative text.
- Large stylized SFX.
- Faces and artwork close to text.
- Dense and irregular panel layouts.
- Prose pages, evaluated as a separate task.

Evaluate the current Frank detector, the Koharu checkpoint as a research baseline, and permissively licensed candidates on the same images.

Measure:

- Per-class precision and recall.
- Balloon and text mask IoU.
- Text-to-balloon relationship accuracy.
- SFX recall and false-positive rate.
- Reading-order accuracy.
- OCR success after detector cropping.
- Source pixels incorrectly removed from artwork.
- Final render acceptability.
- Inference latency and peak memory on the production GPU.

Downstream measures matter more than detection confidence alone. A detector can score well while producing masks that damage artwork or crops that hurt OCR.

### Priority 0: Correct manga reading order

Replace the current `(top, left)` ordering with layout-aware manga order:

1. Use detected panels when available.
2. Group vertically overlapping regions within each panel.
3. Order groups from top to bottom.
4. Order regions within a group from right to left.
5. Retain a deterministic tie-breaker.

Add regression fixtures for aligned balloons, staggered balloons, and multi-panel pages.

### Priority 1: Introduce an instance-segmentation detector interface

Define a detector result that can hold:

```text
class
confidence
bounding box
instance mask or polygon
reading order
parent panel
parent balloon
```

Keep the current RT-DETR detector as a fallback while evaluating replacements. Prototype Koharu's checkpoint only as a benchmark until its licensing is resolved. Prefer training or adopting a model with suitable commercial and redistribution terms.

### Priority 1: Evolve cache metadata into a page document

**Phase 0 status: contract implemented.** The current
[PageDocument v2](page_document_v2.md) extends the existing cache metadata
rather than adding a separate project system. Its region contract contains:

```text
id
kind
confidence
bounding box
polygon or instance-mask asset
reading order
parent panel and balloon IDs
source OCR and language
translation and target language
typography
cleanup-mask asset
user overrides
generation provenance
```

Store large binary masks and cleanup images in the existing content-addressed object store and reference them from the manifest.

New pipeline results now persist the document while existing clients continue
consuming final PNGs. Later phases will make it the direct input to every stage
and will attach the retained mask assets produced by those stages.

### Priority 1: Batch translation by page

Send all valid page regions in reading order with stable IDs. Support:

- Optional source page image for vision-capable models.
- Per-book names and terminology.
- Honorific and untranslated-text rules.
- Tone and formatting instructions.
- Previous-page dialogue summary.
- A separate SFX policy.

Preserve Frank's instruction to keep dialogue concise enough for the available space. Validate the returned IDs and never rely only on array position.

### Priority 2: Improve cleanup before expanding generative models

For each removal region:

1. Use the exact text instance mask.
2. Determine whether the containing balloon has a nearly uniform interior.
3. Fill uniform interiors using a robust sampled color.
4. Inpaint only textured or artwork regions.
5. Group nearby mask components into bounded tiles with surrounding context.
6. Composite only pixels owned by the intended mask component.

This should reduce latency, GPU use, seams, and unwanted changes to artwork.

### Priority 2: Add an optional review mode

The first review interface can be narrow:

- Overlay regions and masks.
- Edit OCR and translation.
- Add, delete, move, and resize regions.
- Change reading order.
- Paint the removal mask.
- Rerender the current page.

It can operate on cached page documents through a desktop or browser interface. The default lens workflow should remain immediate and should not require project management.

### Priority 2: Add layered export

Once the page document is available, export:

- Original page.
- Cleanup result.
- Hidden removal mask.
- One editable translated text layer per region.
- A JSON manifest alongside the visual artifact.

Implement and verify PNG or SVG interchange first. Add PSD after the layer model and typography are stable. PSD should be treated as an interchange format; Frank's page document should remain the authoritative representation.

### Priority 3: Broaden translation configuration carefully

An OpenAI-compatible adapter plus the existing local Ollama path would cover many hosted and self-hosted models without maintaining a separate integration for every provider. Add dedicated integrations only when they offer a concrete feature or a large user benefit.

### Priority 3: Consider offline book import only if product strategy requires it

CBZ, ZIP, RAR, PDF, and folder import would move Frank toward Koharu's authoring market. This should follow improvements to live reading, page documents, review, and export. It should not delay the capabilities that differentiate Frank.

## Proposed product position

Frank's strongest direction is:

> Read immediately in the original service, then optionally correct or export the result.

This combines Frank's low-friction reader integration with the most valuable parts of Koharu's retained workflow. It avoids competing first on broad desktop authoring, where Koharu already has a substantial lead.

## Decisions to preserve

Future work should retain these existing Frank strengths:

- Original page remains available through the lens.
- Kindle and Naver page navigation continues to work normally.
- Manga furigana remains independent of translation.
- Japanese prose remains a first-class pipeline.
- Tall webtoons remain supported without requiring PSD-compatible dimensions.
- Clients remain lightweight and do not require local GPU setup.
- Server queueing, deduplication, recovery, and content-addressed caching remain the operational foundation.

## Reference links

- [Koharu repository](https://github.com/koharu-rs/koharu)
- [Koharu detector model card and metrics](https://huggingface.co/mayocream/koharu-layout-rfdetr-seg-2xl-1152)
- [Koharu processing workflow](https://github.com/koharu-rs/koharu/blob/2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3/packages/docs/en/guides/processing.mdx)
- [Koharu vision and inpainting models](https://github.com/koharu-rs/koharu/blob/2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3/packages/docs/en/models/vision.mdx)
- [Koharu translation providers](https://github.com/koharu-rs/koharu/blob/2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3/packages/docs/en/models/providers.mdx)
- [Koharu export behavior](https://github.com/koharu-rs/koharu/blob/2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3/packages/docs/en/guides/export.mdx)
- [Frank cache metadata](cache_metadata_v2.md)
- [Frank detector](../server/kindle/bubble_detector.py)
- [Frank manga processor](../server/kindle/processor.py)
- [Frank translator](../server/kindle/translator.py)
- [Frank prose layout](../server/kindle/book_layout.py)
- [Frank prose renderer](../server/kindle/book_renderer.py)
- [Frank webtoon processor](../server/webtoon/processor.py)
