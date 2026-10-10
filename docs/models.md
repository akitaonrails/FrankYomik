# Model inventory

`server/pipeline/stage_manifest.json` is the machine-readable authority for
model identity, revisions, configuration, prompts, fonts, and implementation
artifacts. Workers verify this manifest before joining the queue. This page
explains the shipped choices and operational requirements.

## Shipped pipeline models

| Stage | Model | Pinned identity | Runtime and cache |
|---|---|---|---|
| Manga and webtoon detection | RT-DETR-v2 comic text and balloon detector | `ogkalu/comic-text-and-bubble-detector` at `4269a0e53d40419f4746e8b3e30ae7faef3a26c6` | `transformers==5.10.4`; Hugging Face cache |
| Manga and book OCR | Manga OCR | `kha-white/manga-ocr-base` at `aa6573bd10b0d446cbf622e29c3e084914df9741` | `manga-ocr==0.1.16`; Manga OCR and Hugging Face caches |
| Webtoon OCR | EasyOCR Korean | `korean_g2.pth`, MD5 `befecf7b1ca2fffb5af814a51443682d` | `easyocr==1.7.2`; EasyOCR cache |
| Manga and webtoon translation | Qwen 3 14B through Ollama | `qwen3:14b`, Ollama digest `sha256:bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8` | External Ollama or llama-swap service |
| Webtoon cleanup | Big LaMa | release weight SHA-256 `7ba7aa7ac37a4d41fdbbeba3a2af7ead18058552997e3a3cd1a3b2210c9e6b4c` | `simple-lama-inpainting==0.1.0`; supervised persistent child |

Manga cleanup uses deterministic balloon and artwork fills by default. Its
LaMa identity remains in the manifest for compatibility, but model inpainting
is disabled for that stage. Webtoon LaMa is enabled and falls back to a
mask-bounded deterministic fill when it times out, fails, or returns an invalid
shape. Such recovered results are marked degraded and are not put in the
durable result cache.

## Evaluated models that do not ship

- Hayai OCR v2.1 is implemented behind a disabled diagnostic adapter. It beat
  Manga OCR on the frozen difficult-crop set, but its disclosed training data
  is not sufficient for distribution approval. Workers neither download it nor
  advertise it as a capability.
- Koharu's detector checkpoint remains a benchmark reference because its
  Manga109-derived weight terms require separate legal review. Frank keeps the
  production RT-DETR checkpoint and adopted the richer semantic data contract.
- FLUX.2 Klein improved the four hardest synthetic inpainting cases, but it is
  excluded from v2.0.0 because the routing policy, deterministic reproduction,
  resource limits, and production failure behavior are not yet established.
- AOT and RORem did not improve the tested aggregate cleanup quality and are
  not production dependencies.

## Startup verification

The worker preflight loads or locates every model needed by all four pipelines,
checks pinned identities and local weight bytes where available, checks the
Ollama model digest, and validates fonts and configuration artifacts. A worker
does not consume jobs or publish a heartbeat until preflight passes. The API
also requires the worker's derived pipeline revision to match its own.

The current revision is derived from the checked-in manifest rather than
maintained by hand. To print it from the production worker image:

```bash
python -c 'from pipeline.revision import derived_pipeline_revision; print(derived_pipeline_revision())'
```

## Hardware and storage

The generic Compose path uses the CPU worker. Production uses the ROCm image on
the Strix Halo host and shares the following persistent directories between
worker replacements:

- `/home/worker/.cache/huggingface`
- `/home/worker/.cache/manga_ocr`
- `/home/worker/.EasyOCR`

The translation model is served separately and is the largest required model
(about 9 GB). Detector, OCR, and cleanup models fit comfortably on the 64 GB
Strix Halo system. Two production workers are limited to 8 GB RAM with 12 GB
swap each. Pin updates require changing the stage manifest, verifying hashes,
rebuilding both worker variants, and rerunning the full corpus gate.

## Licenses

Package and model licenses remain those published by their upstream projects.
The release audit treats a declared repository license separately from training
data provenance. Comic Neue is distributed under the SIL Open Font License;
the previously bundled restricted display fonts were removed. See the benchmark
and phase evaluation documents for the Hayai and Koharu provenance decisions.
