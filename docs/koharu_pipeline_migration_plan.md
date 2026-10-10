# Koharu-inspired pipeline migration and v2 release plan

## Objective

Upgrade Frank Yomik one processing stage at a time using the Koharu ideas and models that improved measured quality, while preserving Frank's reader-focused product, API behavior, Kindle and Naver integrations, furigana modes, prose support, queueing, and cache guarantees.

The target release is **v2.0.0**. The current release is v1.0.1, and this work changes model dependencies, intermediate data, cache identity, worker deployment, and output quality enough to warrant a major version.

This is a sequential migration. A stage may start only after the previous stage is implemented, documented, tested in isolation, exercised through the complete pipeline, reviewed, and committed as a working delivery boundary.

## Status

Phases 0 through 3 are complete. The foundation provides typed stage protocols,
a validated [PageDocument v2](page_document_v2.md), cache manifest v3 revision
identity, compatibility reads for legacy metadata and manifests, verified model
and font identity, fail-closed translation and configured inpainting, and an
injected, consumer-level
[four-pipeline integration gate](pipeline_architecture.md#verification) in CI.
Phase 1 retained the current detector after the candidate failed its provenance
and quality gates. Phase 2 confirmed Hayai's recognition quality and added a
disabled, pinned adapter, while retaining Manga OCR as the production default
because a safe router from Frank's current signals was not established. Every
completed phase passed its independent review. Phase 3 replaces per-region
translation calls with validated stable-ID page batches while preserving target
languages and furigana behavior. Each phase is committed as a working delivery
boundary.

## Product boundary

The migration includes:

- semantic page detection and retained instance masks;
- better OCR routing for difficult manga text;
- page-level translation with stable region identities and context;
- cheap cleanup before model inpainting and a high-quality fallback for difficult artwork;
- richer but compact page metadata to carry results between stages;
- rendering changes required to consume those results safely;
- reproducible CPU and ROCm worker packaging, model manifests, and operational checks.

Frank remains a live reading product. Project editing, PSD export, archive import, general studio tooling, and Koharu's desktop canvas are outside this migration.

## Evidence behind the plan

The decisions below use the completed [comparative analysis](koharu_comparative_analysis.md), [benchmark results](koharu_benchmark_results.md), and the reproducible assets under [`evaluation/koharu_baseline/`](../evaluation/koharu_baseline/README.md).

Measured conclusions:

- Koharu's semantic detector found 63 genuine text or SFX regions that Frank missed, while adding 20 false, duplicate, page-number, interface, or artwork regions. Its checkpoint is an internal baseline only because its Manga109-derived terms need legal review.
- Frank and Koharu ordinary balloon masks were already close: 0.885 mean and 0.952 median IoU across 233 paired regions. Mask shape alone does not justify replacing Frank's normal monochrome path.
- Hayai OCR produced 1.80% character error and 86.49% exact matches on 37 verified crops. Frank's Manga OCR produced 4.13% and 75.68%. The largest gain was on difficult, short, numeric, and stylized text.
- Page-batched translation, stable IDs, terminology, and context are direct orchestration improvements; they do not require a translation model contest.
- Frank LaMa slightly beat Koharu LaMa and AOT over all eight inpainting cases. FLUX.2 Klein reduced error by about 30% on the four hard artwork cases, at much higher latency. It should be a selective fallback.

## Architecture and test audit before Phase 0

The active path was split between `server/kindle/processor.py`, `server/webtoon/processor.py`, and orchestration in `server/worker/job.py`. The cache persisted legacy schema version 1 metadata through `server/worker/page_cache.py`.

Existing coverage is useful but does not cover this migration end to end:

| Area | Existing evidence | Gap to close before stage replacement |
|---|---|---|
| Manga stage functions | Unit tests mock detector, OCR, translation, and rendering boundaries | No contract tests for interchangeable backends or retained masks |
| Manga full pipeline | Two slow tests run furigana and mocked translation on one page each | Translation is mocked; no worker, cache, model provenance, fallback, or quality assertions |
| Detector | Real-model smoke tests assert shape and minimum counts on manga fixtures | No precision/recall labels, class semantics, mask quality gate, webtoon set, or license gate |
| Webtoon | Extensive unit coverage for clustering, OCR helpers, cleanup, and rendering | No complete webtoon image pipeline integration test |
| Worker/cache | Strong mocked worker routing and cache unit tests | No upload-to-worker-to-cache-to-image integration test and no cache invalidation by pipeline/model revision |
| OCR | Validation helper tests and OCR mocked at pipeline boundaries | No golden crop accuracy suite in normal CI; no fallback routing tests |
| Translation | Prompt/response unit tests and parallel per-bubble call tests | No structured page batch, ordering, partial failure, duplicate ID, or malformed response coverage |
| Inpainting | Mask and backend unit tests, plus the external eight-case benchmark | No policy test that chooses fill, LaMa, or FLUX; no outside-mask damage invariant through a complete render |
| Clients | Flutter, extension, and real-browser lens tests | No compatibility test against a v2 server response/cache payload |

This audit drove Phase 0. CI now creates a fresh environment and runs unit plus
fake-backed integration tests while excluding tests marked `model`, `slow`, or
`network`. Real model quality tests remain explicit commands for a configured
machine.

## Target pipeline

```text
captured page
  -> semantic layout detector
  -> normalized page document with revision-scoped region IDs, relationships, masks, order, provenance
  -> primary OCR plus selective Hayai fallback
  -> validated page translation batch (translation mode) or furigana transform
  -> cleanup policy: uniform fill -> Frank LaMa -> FLUX fallback for hard artwork
  -> mask-aware Frank renderer
  -> versioned cache object and unchanged image response to the reader
```

The prose path keeps its column detector and gutter renderer. It can use the OCR adapter where the adapter is beneficial, but semantic manga layout and inpainting do not apply to `book_furigana`.

## Cross-stage rules

Every stage follows the same delivery gate:

1. Freeze the current behavior with contract or golden tests.
2. Add the new implementation behind a typed adapter and explicit configuration.
3. Keep the last working backend available for rollback during the migration.
4. Run focused unit and quality tests.
5. Run all four pipeline integration scenarios through worker orchestration and cache persistence.
6. Run the stage's model smoke and quality suite on the Strix Halo where applicable.
7. Update architecture, configuration, model, deployment, and operator documentation in the same change.
8. Review the phase for correctness, architecture, dependency/license risk, and regression risk.
9. Commit the green phase before starting the next one.

Quality promotion requires the new stage to meet its stated threshold without regressing the other stages. A failed candidate remains an experimental backend and does not become the default.

## Phase 0: establish contracts and a trustworthy baseline

**Status: complete; Gate 0 passed after the authorized exceptional review.** See
[Pipeline architecture](pipeline_architecture.md),
[PageDocument schema version 2](page_document_v2.md), and
[Cache and page metadata](cache_metadata_v2.md) for the implemented contracts.

### Implementation

- Introduce explicit stage protocols and dependency injection for layout detection, OCR, translation, cleanup, and rendering. Remove import-time model coupling from package initializers so CPU tests can collect without Torch.
- Define a compact `PageDocument` schema that carries page dimensions, geometry-based region IDs that remain stable while detector output is unchanged, semantic kind, box, optional polygon/mask reference, confidence, reading order, parent balloon relationship, OCR alternatives and confidence, transform, typography hints, cleanup choice, and per-stage provenance.
- Version the document and cache identity independently. Require a production `pipeline_revision` deployment fingerprint that covers stage configuration and pinned model revisions so old results cannot be silently served after a model change.
- Read cache metadata v1/v2 for compatibility and write the new schema. Add an explicit migration path and fixtures for old manifests.
- Add deterministic fake adapters that exercise every stage without model downloads.
- Add four worker-level integration scenarios: `manga_translate`, `manga_furigana`, `book_furigana`, and `webtoon`. Each must decode an image, call real orchestration with fakes, persist metadata/image objects, reload them, rerender editable metadata where supported, and preserve dimensions and pipeline identity.
- Add test markers (`integration`, `model`, `network`, `slow`) and documented commands. Run fast unit and contract integration tests in CI. Keep quality benchmarks outside ordinary CI but make them one-command and machine-readable.

### Gate

- Fresh environment can collect and run the CPU suite without downloading models.
- All four fake-backed pipelines pass through the real consumer message path,
  injected runtime, and cache round trip; editable pipelines rerender without
  repeating model stages.
- Current real manga integration fixtures still render successfully.
- Old cache fixtures remain readable; a changed `pipeline_revision` produces a cache miss.
- CI runs the new contract integration suite.

### Documentation

Create the v2 page document specification and v3 cache-manifest specification, then update `README.md`, `CLAUDE.md`, cache documentation, test commands, and the architecture flow. Mark or remove superseded planning documents rather than leaving competing descriptions.

## Phase 1: semantic detection and retained masks

**Status: complete; Gate 1 passed.** The backend-neutral contract, current RT-DETR adapter, class-aware
postprocessing, relationships, reading order, mask geometry persistence, and
compatibility projection are implemented. The current checkpoint remains in
production. The compact v4 candidate was evaluated and blocked because it
failed the coverage, interface false-positive, semantic-output, mask, and
training-data provenance gates. See the
[Phase 1 detector evaluation](../evaluation/koharu_baseline/phase1_detector_evaluation.md).
The final gate verified visual-row and panel-scoped manga order, a 4,100-pixel
three-strip webtoon path, complete detection implementation identity, and the
machine-readable review of every v4 disagreement. The derived pipeline revision
is `sha256:ddc3e2917c6242968f783cc5fa25ad7f42bc06e354392d084929686d4667d2ac`.

### Implementation

- Add a detector result contract for `text`, `sfx`, `balloon`, and `panel`, with confidence, bounding box, optional polygon/RLE mask, model class, and provenance.
- Port Koharu's useful postprocessing behavior: class-aware thresholds, mask restoration, duplicate suppression, text-to-balloon linking, and deterministic reading order.
- Preserve Frank's current mask extraction as the fallback for ordinary monochrome balloons because it already performed well.
- Use the Koharu checkpoint only for internal comparison until its distribution and product-use rights are cleared. Select or train a permissively licensed production checkpoint with the same semantic contract. Record source, revision, checksum, license, training-data statement, and redistribution status in a model manifest.
- Keep manga and webtoon policy separate at first. The Japanese benchmark supports promoting semantic manga detection. Webtoon promotion requires its own labeled full-color/tall-page result.

### Gate

- On the frozen 19-page manga set, production candidate recall covers at least Frank's 284 matched regions plus the audited useful additions, while false additions do not exceed the audited Koharu baseline without an explained precision/recall tradeoff.
- Mask comparison does not regress ordinary balloon median IoU below the current 0.952 paired baseline, or the hybrid policy routes those cases through Frank's extractor.
- No page-number/interface false positive reaches translation/rendering in the audited set.
- At least one representative tall Korean webtoon fixture proves coordinate stitching, masks, and memory bounds. The default webtoon detector changes only if its labeled downstream OCR/render result improves.
- All four Phase 0 integration scenarios remain green.

### Documentation

Document class meanings, thresholds, relationships, mask encoding, model provenance/license, benchmark command, measured results, configuration, fallback, and rollback.

## Phase 2: OCR routing with Hayai for difficult regions

**Status: complete; Gate 2 passed.** Manga OCR remains the production default because
the frozen corpus confirms Hayai's recognizer quality but does not validate a safe
router from Frank's current detector signals. Seven of 18 difficult regions in the
audited set have no current Frank parent and therefore never reach OCR. The optional
Hayai adapter is pinned, locally verified, disabled by default, and fails back without
caching transient results. Automatic weight distribution remains blocked until the
training-image provenance is complete. See [OCR routing](ocr_routing.md) and the
[Phase 2 evaluation](../evaluation/koharu_baseline/phase2_hayai_evaluation.md).
The final gate verified the exact legacy Manga OCR trigger, typed routing provenance,
four pinned local artifacts, single and dual-worker Strix Halo resource evidence, and
derived pipeline revision
`sha256:83a9975ece9d4143312a1866764b0293ef97db98fcd782fe57858285f1b47ef5`.

### Implementation

- Add a shared OCR adapter that returns text, confidence/quality signals, engine, revision, and optional alternatives.
- Keep Manga OCR as the fast primary for ordinary Japanese dialogue and prose chunks.
- Add Hayai OCR as a pinned backend and route short numeric, multilingual, stylized/SFX, horizontal, or primary/consistency-failing regions to it. Plain short output is not sufficient after the frozen regression check. Make the decision deterministic and store it in the page document.
- Preserve Frank's Japanese validation, two-crop consistency check, furigana handling, and prose chunking. Preserve the EasyOCR Korean path until a Korean benchmark establishes that Hayai improves it.
- Audit the Hayai code and weight licenses before production packaging, then pin model files by revision and checksum.

### Gate

- The 37-crop suite meets or beats Hayai's measured 1.80% CER and 86.49% exact match within reproducible tolerance.
- Difficult-crop CER remains at or below 5.26%; ordinary dialogue and prose do not regress against Manga OCR.
- Routing unit tests cover every reason, unavailable-model fallback, timeout/OOM fallback, and provenance.
- Manga translation, manga furigana, and book furigana complete through worker/cache integration; webtoon remains green.
- Strix Halo smoke records cold/warm latency and peak memory for one and two worker replicas.

### Documentation

Document supported scripts/languages, routing rules, confidence limitations, cache paths, model sizes/checksums/licenses, resource use, configuration, and failure behavior.

## Phase 3: page-batched translation

**Status: complete; Gate 3 passed.** Manga and webtoon translation now use a
stable-ID structured page request with strict response validation, atomic
application, and bounded recovery. Normal pages use one provider call; pages
over 64 eligible regions split deterministically while retaining one model
verification and an all-or-fail result. Furigana paths are unchanged. The exact
pinned `qwen3:14b` endpoint on the Strix Halo accepted the production schema for
Japanese dialogue and a mixed Korean dialogue/SFX request. See
[Page-batched translation](page_batched_translation.md) and the
[Phase 3 evaluation](../evaluation/koharu_baseline/phase3_translation_evaluation.md).
The final derived pipeline revision is
`sha256:8ff06138e3e318985a8fa5dab38c40af496b4d4c13d6bc0ef686cd2eba45ad8f`.

### Implementation

- Replace independent per-region requests with one structured page request containing stable IDs, reading order, semantic kind, source text, target language, optional preceding-page context, terminology, and style rules.
- Require schema-valid output keyed by the submitted IDs. Reject duplicates and unknown IDs, preserve source order, and retry missing items with a bounded smaller batch or the existing single-region call.
- Use the existing Ollama/llama-swap endpoint and configured model. Do not import Koharu's provider catalog.
- Persist prompt/schema revision and model identity. Do not store secrets or full provider responses in page metadata.
- Preserve current target-language API behavior and furigana pipelines.

### Gate

- Deterministic fake-provider tests cover reordering, missing/duplicate/unknown IDs, malformed JSON, partial completion, timeout, context limits, and fallback.
- A curated page set is reviewed for names, pronouns, dialogue continuity, untranslated-text rules, and SFX handling against the old per-bubble output.
- Batch mode reduces provider calls to one per normal page and never loses a valid region when fallback succeeds.
- All four complete pipeline scenarios remain green; cache keys change when prompt/model revision changes.

### Documentation

Document the request/response schema, prompt policy, context and terminology behavior, privacy boundary, retries, provider configuration, and troubleshooting.

## Phase 4: cleanup and inpainting policy

Implementation status: the shared deterministic mask, uniform-fill decision,
bounded LaMa tiles, resource guards, deterministic seeds, persisted route, and
safe degradation are implemented. Frank LaMa remains the production default.
FLUX.2 Klein remains disabled pending an observably deterministic router, the
two-replica/OOM operational gate, and resolution of incomplete training-data
disclosure, in addition to the controlled quality gate below.

### Implementation

- Build the cleanup mask from retained semantic masks and OCR strokes, with explicit dilation and clipping.
- Port Koharu's decision flow: fill nearly uniform interiors directly; group remaining connected components into bounded tiles with surrounding context; use deterministic model seeds where supported.
- Keep Frank LaMa as the default learned backend.
- Add FLUX.2 Klein as a bounded fallback for artwork regions classified as difficult. Avoid running it for normal white balloons or pages where its latency has no measured benefit.
- Define resource guards for tile count, dimensions, wall time, host memory, and GPU memory. Backend failure must degrade to the last safe cleanup result rather than fail the whole page.

### Gate

- Re-run the eight controlled cases and keep overall masked MAE at or below the Frank LaMa baseline of 29.14.
- On the four difficult cases, fallback quality approaches or beats the measured FLUX value of 25.75 within the documented reproducibility tolerance.
- Pixels outside the expanded cleanup mask remain unchanged within the encoder tolerance.
- Uniform-fill cases do not load an inpainting model.
- Strix Halo tests establish latency, memory, parallel-worker behavior, OOM recovery, and model cache persistence.
- All four complete pipeline scenarios remain green.

### Documentation

Document the policy decision tree, thresholds, tile/context geometry, seeds, model provenance/licenses/checksums, resource budget, cache volumes, configuration, and degraded behavior.

## Phase 5: consume the richer document in rendering

### Implementation

- Make manga and webtoon rendering consume the normalized page document rather than detector-specific objects.
- Use semantic kind, parent balloon, instance mask, reading direction, and source typography estimates to select placement and style.
- Retain Frank's specialized English manga, vertical furigana, prose-gutter, and color-aware webtoon renderers. Add only Koharu mechanics that improve the reading output: stable layout inputs, mask-aware bounds, correct horizontal/vertical selection, Unicode shaping/font fallback where required, and deterministic typography provenance.
- Preserve source image dimensions and the client-facing PNG behavior so lens signature matching and full-page replacement remain compatible.

### Gate

- Golden structural assertions cover dimensions, alpha/color mode, region containment, no outside-mask cleanup, no text overflow, reading order, and deterministic rerendering.
- Representative manga, furigana, prose, and webtoon pages receive visual review at actual lens zoom levels.
- Existing Flutter and extension lens/signature browser tests pass using new server renders.
- Metadata edits rerender without detection, OCR, translation, or cleanup model calls.
- All four complete pipeline scenarios remain green.

### Documentation

Document layout decisions and invariants, font requirements/licenses, rerender behavior, and client compatibility. Update screenshots only after the rendered result is accepted.

## Phase 6: production consolidation and migration rehearsal

### Implementation

- Select new defaults and remove temporary experimental paths that have no rollback or diagnostic value. Keep the v1 backend configuration for the v2.0 rollback window.
- Build CPU and ROCm worker images from pinned dependencies. Preflight every required model and expose worker capabilities/revisions in health output.
- Add startup failure messages for missing/incompatible weights and prevent an incapable worker from claiming a job it cannot run.
- Rehearse v1 cache reading, v2 cache writing, dedup revisioning, worker restart, abandoned-job recovery, two-replica scheduling, and rollback on the Strix Halo deployment.
- Update deployment documentation from the stale RunPod-only description to the actual homelab Compose/ROCm setup, while retaining a clearly marked generic deployment section if still supported.

### Gate

- Fresh image builds pass Go, Python, Flutter, extension, browser, contract integration, and model smoke suites.
- A clean Strix Halo deployment downloads or locates pinned models, becomes healthy, processes each pipeline, survives worker restart, and serves correct cached images to a real client.
- Performance report records per-stage warm/cold latency, peak memory, page success rate, fallback counts, and output hashes for the release corpus.
- Rollback restores the v1 worker against preserved data without corrupting v2 cache objects.

### Documentation

Update every setup, architecture, configuration, troubleshooting, model, benchmark, and cache page. Run a repository-wide stale-term check for old model names, old schema versions, removed settings, obsolete paths, and conflicting diagrams.

## Phase 7: v2.0.0 release

The release phase follows the repository release workflow and the release audit process after all implementation phases have landed.

### Version and compatibility work

- Set Flutter `version` to `2.0.0` with an appropriate monotonically increasing Android build number.
- Set extension manifest and package versions to `2.0.0`.
- Add `releases/v2.0.0.md` with user-visible quality changes, model/storage requirements, cache behavior, server/client compatibility, upgrade steps, rollback steps, and known limitations.
- State that public pipeline names and image endpoints remain stable. Document the server/client compatibility matrix and page-document/cache schema changes.
- Update the release workflow so release-candidate tags are published as prereleases rather than normal releases, or use draft releases for candidates.

### Release sequence

1. Freeze features after Phase 6 and cut a `v2.0.0-rc.1` candidate from the reviewed SHA.
2. Run the full release matrix and install its actual artifacts on supported clients.
3. Run live Kindle manga, Kindle prose, and Naver webtoon smoke tests against the Strix Halo deployment.
4. Compare release-corpus quality and resource metrics to the Phase 0 baseline and investigate every regression.
5. Run the combined post-change audit, including security, dependencies, model licenses, cache migration, documentation, and exact commit provenance.
6. Fix material findings and cut another candidate when code or packaged dependencies change.
7. Tag `v2.0.0` only from the exact green release-candidate commit and let `.github/workflows/release.yml` publish the signed/packaged artifacts.
8. Deploy the exact tagged API and worker images, perform the production smoke, and monitor error rate, latency, fallback rate, and GPU memory through the rollback window.

### Final release gate

- Hosted CI is green at the tag SHA.
- All four processing pipelines pass on production-shaped infrastructure.
- Quality thresholds from Phases 1 through 5 remain satisfied.
- No unresolved high-severity audit or licensing issue remains.
- Release notes, README, CLAUDE.md, cache spec, deployment guide, model manifest, and benchmark report describe the shipped defaults.
- Rollback images/configuration and cache backup have been verified before deployment.

## Documentation ownership and freshness

Documentation is part of each phase's definition of done. The final structure should have one authoritative page for each concern:

- `README.md`: supported product behavior and quick start;
- `CLAUDE.md`: current code map and development/test commands;
- `docs/pipeline_architecture.md`: stage contracts and runtime flow;
- `docs/page_document_v2.md`: normalized pipeline data and compatibility schema;
- `docs/models.md`: pinned revisions, checksums, licenses, cache locations, hardware needs;
- `docs/deployment.md`: current production and generic deployment procedures;
- `docs/koharu_pipeline_migration_plan.md`: historical execution plan and decisions;
- `docs/koharu_benchmark_results.md`: measured baseline and later comparison results;
- `releases/v2.0.0.md`: user migration and release notes.

At each phase gate, search all tracked prose, examples, Compose files, Dockerfiles, and workflows for the changed names and settings. Update or explicitly archive stale plans. A documentation check should validate local Markdown links and fail CI for references to removed configuration keys or files.

## Stop conditions

A stage does not advance when:

- its new default misses the stated quality threshold;
- a required model lacks acceptable use or redistribution terms;
- the full pipeline or v1 cache compatibility is red;
- the worker cannot operate within the Strix Halo resource budget;
- output breaks lens verification, page dimensions, or live reader gestures;
- documentation and rollback instructions do not match the tested build.

In that case, keep the previous stage implementation as the default, record the evidence, and revise the candidate or scope before continuing.
