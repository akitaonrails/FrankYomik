# Pipeline architecture

## Current pipeline architecture

Frank Yomik exposes four image pipelines through one Go API and Python worker
queue:

| Pipeline | Input and output |
|---|---|
| `manga_translate` | Japanese manga page to translated rendered page. |
| `manga_furigana` | Japanese manga page to rendered furigana annotations. |
| `book_furigana` | Japanese prose page to gutter furigana. |
| `webtoon` | Korean webtoon image to translated rendered image. |

Phase 0 added typed production stage boundaries and the version 2
[PageDocument](page_document_v2.md). Phase 1 added the
[semantic detection contract](semantic_detection.md) while retaining the
selected RT-DETR weights and Frank's monochrome balloon mask extractor. The
active implementations still live
in `server/kindle/` and `server/webtoon/`. Lazy compatibility adapters under
`server/pipeline/adapters/` implement the protocols used by
`server/worker/job.py`, which adapts their results into a validated document.

## Request and cache flow

```text
Flutter or Chromium client
  -> Go API: authenticate, hash source, check revision-scoped cache
  -> Redis Stream job carrying pipeline_revision
  -> Python worker: reject mismatched revision, run selected pipeline
  -> PageDocument v2 plus rendered PNG
  -> cache manifest v4 and content-addressed objects
  -> Redis status and WebSocket completion
  -> client downloads and displays the rendered PNG
```

The Go API owns submission, deduplication, status, cache reads, and the public
HTTP/WebSocket surface. The Python worker owns model execution, document
construction, cache writes, and metadata rerendering. Redis contains transient
queue and delivery state. The filesystem cache is the persistent result store.

Before allocating decoded pixels, the worker parses the image header with
Pillow and rejects unsupported or malformed files, dimensions above 32,768,
and pages above 40,000,000 pixels. It loads an accepted image once and derives
the OpenCV BGR array from that RGB decode. This complements the API compressed
body limit and prevents a small authenticated upload from expanding without
bound.

## Processing flows

### Manga translation

1. Decode the page and classify it as artwork or prose.
2. Normalize current RT-DETR output into balloon, text, and SFX geometry,
   link contained text to balloons, and project renderable regions into the
   current OCR path.
3. OCR each region with Manga OCR.
4. Verify the configured Ollama model digest, then translate valid regions.
5. Run cleanup and persist its exact base pixels and effective masks as
   content-addressed PNG objects.
6. Build a version 2 document with geometry-based region IDs, render inputs,
   and stage provenance.
7. Render the document onto the persisted cleanup base.

### Manga furigana

Detection and OCR match the translation flow. The transform stage creates
furigana segments, and the manga renderer draws those segments instead of a
translation.

### Book furigana

The prose path keeps the page intact, analyzes vertical columns, reads them in
chunks, creates ruby readings, and renders those readings in the gutters. It
does not use manga balloon detection or manga inpainting.

### Webtoon

The webtoon path applies the same semantic contract with vertical reading order
and page-space stitching for tall strips. It then performs Korean OCR in a
separate stage, translates dialogue and SFX, cleans dialogue regions, and then
renders without model or network calls. Dialogue and SFX both require the pinned
Ollama digest. Enabled LaMa cleanup verifies its local weight bytes and runs in
a persistent supervised child. A deadline covers model load, inference-lock
wait, IPC, and inference. Timeout, OOM, invalid output dimensions, and expected
backend failures terminate and reset the child, roll back its output, and use a
mask-bounded deterministic fill. Recovered pages are returned as completed but
are noncacheable, and their region provenance names the fill rather than LaMa.
Its stage results are normalized into the same document contract as the other
pipelines.

### Document rendering and rerender

Manga and webtoon live processing now crosses the same `PageDocument` render
boundary as metadata rerender. The renderer starts from the exact RGB cleanup
base named by `cleanup_base`, processes regions by `reading_order`, and uses
the persisted render box, source OCR boxes, orientation, parent balloon, and
semantic mask. It retains Frank's manga translation, vertical furigana,
color-aware webtoon dialogue, and outlined SFX renderers. Book rendering keeps
its existing gutter flow.

Each completed text render writes its final font size, line count, and fit
status back into the document. Webtoon fallback ellipsis is explicitly marked;
overflow is logged and remains inspectable after a metadata edit. Artwork text
uses its dedicated renderer on the already-cleaned base, so rendering does not
perform a hidden second cleanup.

Cleanup mask references name the exact effective removal masks. Object
references are restricted to `objects/<sha256>`, are hash and dimension
checked before use, and must stay within their region. Rerender performs no
detection, OCR, translation, cleanup, or model call. Legacy schema 1 records
continue through the prior source-based compatibility path.

English dialogue and SFX use the bundled OFL Comic Neue regular and bold
fonts. Restricted Komika Text and BadaBoom files are no longer distributed.

## Typed stage boundaries

`server/pipeline/protocols.py` defines the compatibility page-stage contracts
used by production for:

- detection;
- OCR;
- translation;
- cleanup;
- rendering.

`PipelineRuntime` defines every callable used by worker orchestration, including
distinct manga and webtoon detection, OCR, cleanup, and render calls. The
immutable `PipelineDependencies` implementation lives with the lazy current
backend adapters. `process_job` accepts that runtime, and the real `Consumer`
forwards its configured runtime. This keeps concrete ML packages out of
CPU-only test collection.

Current adapters carry the existing mutable manga and webtoon page objects
between stages to preserve behavior during migration. Detection additionally
carries backend-neutral `SemanticDetection` values. `PageDocument` is the
typed, validated persistence boundary produced after those stages.

`server/pipeline/stage_manifest.json` is the authoritative identity manifest.
`server/pipeline/current_backend.py` reads it to supply stage provenance,
source language, and cleanup policy instead of making worker orchestration
infer them from function or class names. Later phases can replace one injected
boundary while the rest of the runtime and its document contract stay fixed.

## Pipeline revision

`PIPELINE_REVISION` identifies every choice that can affect output, including
stage configuration, prompt behavior, and pinned models. It must match on the
API and every worker. The allowed format is 1 to 128 characters from letters,
digits, `.`, `_`, `:`, and `-`.

The API includes the revision in job messages and Redis deduplication. Workers
reject jobs from a different revision. A missing revision in an old job message
maps to `legacy-v1`; it does not inherit the current default. Cache reads treat
a manifest from a different revision as a miss. A miss reprocesses the page and
replaces its manifest while content-addressed objects remain deduplicated.

The API and worker independently derive the revision by hashing the checked-in
stage manifest. Both verify the source, configuration, and font artifact hashes
named by that manifest. `PIPELINE_REVISION` is optional; when set, it is an
expected value and startup fails unless it matches the derived fingerprint.
A pre-revision cache manifest belongs to `legacy-v1`; reading it requires both
the legacy value and the explicit compatibility flag:

```dotenv
PIPELINE_REVISION=legacy-v1
ALLOW_LEGACY_PIPELINE_REVISION=1
```

Deploy the API and workers from the same manifest. During a rolling deployment,
an old worker rejects a new-revision job rather than processing it with mixed
stages. Per-stage provenance contains the actual implementation revision,
model weights, and prompt/configuration hash; it does not repeat the global
deployment fingerprint. See [Pipeline stage manifest](pipeline_stage_manifest.md)
for the pinning, verification, and update procedure.

Before connecting to Redis, a production worker preflights every model and data
dependency needed by all four pipelines. Current priority streams are shared,
so a partial-capability process is rejected at startup rather than claiming a
job it cannot execute. After preflight, each heartbeat has a companion JSON
record containing the pipeline list, revision, verified model names, and
preflight status. `/api/v1/health` exposes that record and marks a worker ready
only when its revision matches the API.

## Pipeline name and cache namespace

A `PageDocument.pipeline` is always one of the four executable base pipeline
names. The durable cache namespace also includes the output language:

- English uses the base name, such as `manga_translate`.
- Another language uses `<pipeline>_<target-language>`, such as
  `manga_translate_pt-br`.

The manifest and cache URL use the language-qualified namespace, while the
document stored inside that manifest keeps `pipeline: "manga_translate"`.
Cache validation splits the namespace before validating the document. This
keeps English and Portuguese renders of the same source from replacing each
other without teaching processing code about storage names.

## Persistence versions

These version numbers describe different contracts:

| Name | Current version | Purpose |
|---|---:|---|
| Cache directory layout | `v2` | Content-addressed filesystem paths. |
| Cache manifest schema | `3` | Pointers, hashes, revision, timestamps, and stale state. |
| Page metadata (`PageDocument`) | `2` | Structured stage results for one page. |
| Legacy page metadata | `1` | Flat editable regions retained for rerender compatibility. |

The authoritative cache manifest specification is
[Cache and page metadata](cache_metadata_v2.md). The authoritative payload
specification is [PageDocument schema version 2](page_document_v2.md).

## Verification

Fast tests run without model downloads or external services:

```bash
cd server
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/unit tests/integration \
  -m "not slow and not model and not network" -v
```

`server/tests/integration/test_worker_pipeline_contract.py` sends Redis-style
messages through the real `Consumer._process_message` path for all four
pipelines. It exercises job decoding, injected runtime orchestration, version 2
document construction, the `pt-br` cache namespace, manifest/object writes,
and cache reloads with deterministic model-free stage implementations. The fake
records every applicable layout/detection, OCR, translation or transform,
cleanup, and render call. It also
edits cached metadata and rerenders manga and webtoon results, proving that
detection, OCR, transformation, translation, and model cleanup are not called
again. `book_furigana` is
covered through processing and caching; metadata rerender is intentionally not
supported for its gutter layout.

Real model tests are explicit:

```bash
cd server
PYTHONPATH=. .venv/bin/python -m pytest tests/integration -m model -v
```

Some model tests are also marked `slow`. Tests that require an external service
use `network`. CI runs the fast command and therefore exercises unit tests and
the fake-backed four-pipeline contract on CPU.
