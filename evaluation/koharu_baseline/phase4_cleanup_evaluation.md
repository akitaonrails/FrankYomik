# Phase 4 cleanup and inpainting evaluation

Audit date: October 10, 2026

## Decision

Keep Frank's pinned LaMa backend as the routine textured-region default. Add
uniform fill ahead of model selection. FLUX.2 Klein 4B is a viable opt-in
candidate for difficult artwork, but it is **not ready to become an automatic
production fallback** until a deterministic router and the operational gates
below pass.

The frozen quality result supports the candidate. It does not support routing
the four known difficult cases in production: those cases were selected by
manual review with clean references and therefore form an oracle subset.

## Current-tree audit

Frank currently has two cleanup paths:

- Manga artwork cleanup in `server/kindle/inpainter.py` uses the shared mask,
  bounded-tile, memory, deadline, and supervised LaMa path. Expected failure
  returns the unchanged image with degraded metadata. Successful output is
  composited only through the effective removal mask.
- Webtoon cleanup in `server/webtoon/inpainter.py` selects a model-free median
  fill for uniform interiors before backend lookup. Textured regions use a
  persistent supervised LaMa child. Expected timeout, OOM, size, availability,
  and backend failures roll back and apply a degraded mask-bounded fill.
- The former Flux.1 and SDXL backend classes were removed. Production's backend
  registry exposes only pinned LaMa, and no FLUX.2 configuration or adapter is
  exported.

The selected cleanup route and degraded state flow into region provenance.
Degraded results are non-cacheable. The effective removal mask remains
transient and is recomputed from OCR and semantic geometry.

Existing tests cover mask construction, failure propagation, and compositing.
In particular, `test_inpaint_modifies_only_masked_pixels` exercises the
production webtoon compositor with a fake backend. The frozen evaluator below
adds exact page-independent evidence for every saved benchmark output.

## Historical backend baseline

The original eight-case benchmark uses a clean manga crop as the reference, a synthetic Japanese
glyph overlay as input, and an identical binary removal mask for each backend.
The historical portion of the evaluator reopens those pixels; it does not
trust the saved aggregate JSON. These results compare backend PNGs through the
frozen benchmark mask. They do not, by themselves, validate the current
production router, effective mask, tiling, rollback, or compositor.

```bash
cd evaluation/koharu_baseline
python scripts/evaluate_phase4_cleanup.py \
  --output outputs/phase4-production-policy.json
```

The output path is optional. Omitting it prints JSON. No model or GPU is loaded.

| Result | Masked MAE | Outside-mask changed pixels |
| --- | ---: | ---: |
| Frank LaMa, all 8 | **29.1424** | 0 |
| Koharu LaMa, all 8 | 29.5463 | 0 |
| Koharu AOT, all 8 | 30.5511 | 0 |
| FLUX.2 Klein, 4 oracle hard cases | **25.7514** | 0 |
| Oracle selective policy, 8 cases | **23.5991** | 0 |

Frank LaMa meets the Phase 4 overall target of 29.14 within the precision of
the pre-existing target. FLUX lowers hard-subset error by 30.1% relative to
Frank LaMa's 36.8380 on the same four cases and wins each case. Combining Frank
LaMa for the other four cases with FLUX for the oracle subset lowers aggregate
error by 19.0%.

All historical saved outputs are exact copies of the input outside the supplied mask. A
production FLUX adapter must apply the same compositor after checking output
dimensions. Generated pixels outside the mask must never reach the page.

The frozen evidence identities are:

| File | SHA-256 |
| --- | --- |
| `score.json` | `f1f0e950ad41cbc4d27529c581748da8babb9faaa25b2732ac0bd49c60f39c67` |
| `escalated-score.json` | `8042794e549ecc54612fffb0194b0ac1007fa0c006a85cb3fec6ef6247809229` |
| `frank_lama/results.json` | `6685c6060df73854c14c9ef8d89510f9ac2f0070060a65c881dfa3a36a2c2b8e` |

## Current production-policy evaluation

The same command also imports and executes the exact checked-in
`pipeline.cleanup` planner and `webtoon.inpainter.cleanup_bubble`. For each of
the eight frozen artwork cases it:

1. derives the OCR rectangle from the annotated synthetic-glyph mask;
2. estimates strokes and constructs the effective mask with production padding
   and dilation;
3. runs production connected-component tiling and the supervised child-process
   boundary;
4. supplies each tile with the corresponding frozen Frank LaMa pixels through
   the public injected-backend hook;
5. repeats the case with a deterministic child-process `MemoryError` to exercise
   OOM classification, rollback, degraded fill, and provenance; and
6. measures both frozen-mask quality and effective-mask integrity from pixels.

The saved-output adapter avoids repeating unchanged LaMa inference while still
exercising production routing, process supervision, tile order, masks, and
compositing. Runtime therefore measures policy and child-process overhead; it
does not include real LaMa inference.

The corpus does not retain the detector's sampled background color. The
evaluator uses an observable proxy: the median RGB value in the eight-pixel
ring outside the annotated mask. The fixtures are artwork crops and have no
semantic balloon mask, matching manga artwork cleanup. A separate flat control
uses the same 384 by 384 dimensions and annotated mask geometry to exercise the
uniform branch.

| Production-policy result | Route | Mean MAE on effective mask | Mean MAE on frozen reference mask | Outside-effective-mask delta |
| --- | --- | ---: | ---: | ---: |
| Saved Frank output adapter, 8 artwork cases | `lama_tiles` | 9.5941 | **29.1424** | 0 |
| Forced child OOM, 8 artwork cases | `uniform_fill`, degraded | 84.8264 | 87.8057 | 0 |
| Flat uniform control | `uniform_fill` | 0.0000 | 0.0000 | 0 |

Every artwork case produced one bounded tile and selected `lama_tiles`. Every
forced OOM was classified as `oom`, rolled back, and returned a degraded
mask-bounded fill. The uniform control made zero backend calls. The effective
masks contain 28,121 to 29,267 pixels, compared with 9,565 pixels in each
frozen annotation, so effective-mask MAE is reported separately and must not be
compared directly with the historical 29.14 target. Scoring the production
compositor through the original frozen masks reproduces 29.1424 exactly.

The tracked machine-readable output is
[`outputs/phase4-production-policy.json`](outputs/phase4-production-policy.json).
Per-case records include the actual route, degradation reason, effective mask
size, both MAEs, tile count, and exact outside-mask delta.
At pipeline revision
`sha256:3d5e7eb24fe67d0882f9b30935890d759d1c2291b38b330dad3113a6f47e9551`,
two consecutive executions produced byte-identical result files with SHA-256
`0552709cdf7ee7bb279a42072bcc21a008943c97c4943a44b4f2007c598aa91d`.

Wall-clock observations are deliberately excluded from that tracked artifact.
They vary with host scheduling and would make an otherwise deterministic
quality gate change on every run. To record local timings separately, use an
ignored or temporary path:

```bash
python scripts/evaluate_phase4_cleanup.py \
  --output outputs/phase4-production-policy.json \
  --runtime-output /tmp/phase4-runtime.json
```

Across the two reproducibility runs, saved-output policy calls averaged 459.13
to 464.91 ms, forced-OOM calls averaged 460.89 to 468.84 ms, and the uniform
control took 446.83 to 455.01 ms. These local observations include production
planning and process supervision and exclude learned-model inference.

The degraded fill is an integrity and availability fallback. Its high artwork
error confirms that it must be marked degraded and excluded from healthy cache
reuse; it is not a quality substitute for LaMa.

## FLUX.2 Klein identity and provenance

The benchmark used Koharu commit
`2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3` plus its evaluation-only mask
serialization patch. Koharu's local code pins three Hugging Face revisions and
uses native inference; it does not execute model-repository Python or request
`trust_remote_code`.

The machine-readable revisions, sizes, and SHA-256 values are in
[`model_manifests/flux2_klein_4b.json`](model_manifests/flux2_klein_4b.json).
The three cached files on Strix were rehashed and match Hugging Face's blob
digests exactly. Total model weight storage is 5,351,111,708 bytes (4.98 GiB).

| Component | Pinned revision | License |
| --- | --- | --- |
| Unsloth FLUX.2 Klein 4B Q4_K_M | `0084d1df98e2e2137fe776d55170bc4792ec1d66` | Apache-2.0 |
| BFL FLUX.2 small decoder | `a3efc24f613ef42d9428af62fdbd6f5fd8856c4a` | Apache-2.0 |
| Unsloth Qwen3 4B Q4_K_M | `22c9fc8a8c7700b76a1789366280a6a5a1ad1120` | Apache-2.0 |

Primary references:

- [BFL's official inference repository](https://github.com/black-forest-labs/flux2)
  identifies both FLUX.2 Klein 4B variants as Apache-2.0 and production-capable.
- [The exact Unsloth quant repository](https://huggingface.co/unsloth/FLUX.2-klein-4B-GGUF)
  identifies BFL's 4B model as its base and the artifact as Apache-2.0.
- [The exact small-decoder repository](https://huggingface.co/black-forest-labs/FLUX.2-small-decoder)
  identifies the distilled decoder as Apache-2.0.
- [The Qwen3 source repository](https://huggingface.co/Qwen/Qwen3-4B)
  carries Apache-2.0 terms; the pinned Unsloth quant links it as the base.

Apache-2.0 permits commercial use and redistribution subject to its notice and
license conditions. Koharu itself is MIT or Apache-2.0. This removes the model
license blocker that applies to Koharu's Manga109-trained detector.

Training-data provenance remains incomplete. BFL states that it filtered
pretraining data for NSFW and known CSAM and performed targeted safety tuning,
but neither the official card nor the quant card enumerates the underlying
image datasets. This is a disclosed provenance limitation, not a license
conflict found in the reviewed artifacts. The Unsloth card also does not pin
the exact quantization tool revision. Production should consume only the
listed byte digests and should retain the license and model-card notices.

## Strix Halo availability and resource evidence

The candidate is already present in the isolated evaluation workspace:

```text
/var/opt/docker/frank/yomik-eval/bin/flux2_klein
/var/opt/docker/frank/yomik-eval/cache/koharu/packages/hugging-face/models/
```

The binary SHA-256 is
`61c8ce97dcfd010f2c784b0999f14c73bc5fc168de8468f079a8572285238154`.
It previously completed all four hard cases on the Radeon 8060S (`gfx1151`).
The cold process took 69.219 seconds and the following one-shot processes took
7.470, 7.606, and 7.662 seconds. Each invocation exits after one result, so
these are cached-weight process timings rather than a persistent-service warm
latency measurement.

The cached weights and results were still present and passed SHA-256 checking
after the current service restart, establishing cache persistence in the
evaluation volume. FLUX is not exposed through Ollama or llama-swap; the
available integration is the standalone Koharu binary. A production adapter
would therefore need a supervised local process or service with bounded input,
timeout, output-size checks, and no shell interpolation.

The audit did not launch another FLUX process because two active llama-server
processes already held 25.02 GB of unified GPU memory, including one 24.25 GB
allocation. The machine had about 43 GiB system memory available. Starting two
unmeasured FLUX replicas in that state would risk disrupting the shared host.
The earlier run proves single-process compatibility and output quality; it
does not prove a two-replica production budget.

## Production promotion gates

The implementation can promote uniform fill and retain LaMa immediately once
its tests pass. FLUX should remain disabled by default until all of these are
green:

1. **Observable router:** define a deterministic rule from source pixels,
   removal mask, and LaMa output only. Evaluate it on all eight cases and on a
   held-out set. Manual case IDs, reference images, and output MAE are forbidden
   router inputs. False escalation of uniform and ordinary regions must be
   reported alongside hard-case recall.
2. **Uniform fast path:** a nearly uniform interior chooses direct fill before
   backend creation. The production-policy flat control proves zero load and
   inference calls; the production unit suite must retain this invariant.
3. **Quality:** routine policy mean remains at or below Frank LaMa's 29.1424;
   selected hard cases approach the 25.7514 FLUX reference within an explicitly
   documented tolerance.
4. **Integrity:** the production-policy corpus proves byte-identical RGB pixels
   outside the effective removal mask for fill, LaMa, and OOM fallback.
   `test_production_cleanup_stage_preserves_pixels_outside_effective_mask`
   also passes through worker orchestration and rendering. Retain both checks;
   FLUX must pass the same invariant before enablement.
5. **Failures:** timeout, malformed output, size mismatch, process error, and
   CUDA/ROCm OOM retain the last valid LaMa or source result, record degraded
   provenance, and never cache it as a healthy FLUX result.
6. **Capacity:** on an otherwise idle Strix, record cold and repeated latency,
   peak host and device memory, two worker replicas, deliberate bounded OOM,
   worker survival, and cache reuse after restart. Two replicas must coexist
   within the deployment memory limits without swap thrashing or production
   worker eviction.
7. **Supply chain:** pin all three revisions and SHA-256 values in the stage
   manifest, verify before load, package license notices, and pin the runtime
   implementation rather than downloading executable code at startup.

## Recommended implementation boundary

Implement the cleanup policy behind the shared pipeline stage interface:

```text
empty mask -> unchanged
nearly uniform removal neighborhood -> deterministic fill
ordinary textured region -> warmed pinned LaMa
deterministically difficult artwork -> optional FLUX.2 Klein
FLUX unavailable or failed -> retain valid LaMa result and mark degraded
```

Keep FLUX disabled by default for the first production merge. This allows the
uniform-fill improvement, shared provenance, exact compositing, and stable
LaMa behavior to ship while the router and capacity evidence are completed.
