# Phase 2 Hayai OCR evidence and routing decision

## Decision

Hayai OCR v2.1 passes Frank's Japanese recognizer-quality upper bound and Strix
Halo technical gate. The 1.44% selective result uses an oracle difficulty
category; no production router has been validated. Production distribution is
also blocked because the two image datasets responsible for OCR training do not
have auditable source and license statements.

Build the Phase 2 adapter and candidate routing contract behind a disabled
configuration. Keep it disabled until a current-pipeline, held-out routing test
passes. Do not include or download the Hayai weights in a released worker until
the training-data blocker is resolved or a substitute checkpoint passes the
same quality gate.

## Exact implementation under evaluation

The measured implementation is Koharu commit
`2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3`. It pins
[`JustANormalTinkerer/hayai-ocr-v2`](https://huggingface.co/JustANormalTinkerer/hayai-ocr-v2/tree/4a4ce477c9a8841f208b94e1d9ed5c0938965e05)
at revision `4a4ce477c9a8841f208b94e1d9ed5c0938965e05` and uses greedy decoding with
256 image patches, at most 256 generated tokens, and repetition penalty 1.0.
Despite the repository name, the pinned card identifies these weights as Hayai
OCR **v2.1**.

Required runtime artifacts:

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `model.safetensors` | 622,502,784 | `4c645b221db8428cda04991be234c18133bb8861142a3d87cba04c5099b02328` |
| `config.json` | 342 | `581b762f1dfd55d0108f3f84e3f157bc762524af37fb0c19a7172a18b75582e2` |
| `tokenizer.json` | 1,247,253 | `f8a0a909c628a684fe463094614e236a8b1d3609e7770f77e7beafaf1056bf13` |
| `tokenizer_config.json` | 244 | `6fb6c69afaedf1275872d3e62e276fd4467bd00da7a84cbbb5566a2cd28f58f6` |

The complete artifact, code, processor, training-data, and security inventory is
machine-readable in
[`model_manifests/hayai_ocr_v2.1.json`](model_manifests/hayai_ocr_v2.1.json).

Hayai accepts image crops and supports Japanese, Chinese, Korean, and English.
It is not a page detector or full-page OCR engine. The architecture contains a
SigLIP2 NaFlex vision encoder, a projection layer, and a 12-layer causal text
decoder with a 16,004-token byte-level BPE vocabulary. Production preprocessing
must pin the
[`google/siglip2-base-patch16-naflex`](https://huggingface.co/google/siglip2-base-patch16-naflex/tree/b53b807d3a2d5e2b3911292f2d69e5341cdc064c)
configuration and processor rather than resolving `main`.

## Security and dependency audit

The upstream Transformers usage requires `trust_remote_code=True`. The exact
reviewed
[`configuration_hayai.py`](https://huggingface.co/JustANormalTinkerer/hayai-ocr-v2/blob/4a4ce477c9a8841f208b94e1d9ed5c0938965e05/configuration_hayai.py)
and
[`modeling_hayai.py`](https://huggingface.co/JustANormalTinkerer/hayai-ocr-v2/blob/4a4ce477c9a8841f208b94e1d9ed5c0938965e05/modeling_hayai.py)
contain no shell or process execution, socket use, arbitrary file writes, or
dynamic evaluation. They do perform two avoidable remote resolutions:

1. `Siglip2VisionConfig.from_pretrained` resolves Google's processor model
   without a revision.
2. The custom `from_pretrained` implementation resolves its own weights again.

It prefers safetensors, but contains a `torch.load` pickle fallback when only a
`pytorch_model.bin` is found. Frank must not enable remote code or that fallback.
Only the four named runtime files may be downloaded, and every file must be
verified before parsing. A reviewed local implementation may use safetensors,
JSON, and the pinned tokenizer; it must make no network call after preflight.

The upstream path requires PyTorch, Transformers 5, safetensors, tokenizers,
Pillow, and the SigLIP2 image processor. Koharu instead ports the architecture
to Rust/libtorch and does not execute the remote Python. The benchmark proves
that port on ROCm 10/Torch 2.13; it does not yet prove a Python port on Frank's
PyTorch 2.9/ROCm 7.2 image. That exact worker-image smoke remains a promotion
gate for any implementation.

## License, provenance, and redistribution

The pinned model repository declares Apache-2.0 for its code and weights. Its
SigLIP2 base also declares Apache-2.0 and documents WebLI pretraining. The model
card identifies four Hayai training inputs:

- [`JustANormalTinkerer/hayai-dataset-merged`](https://huggingface.co/datasets/JustANormalTinkerer/hayai-dataset-merged),
  about one million OCR images;
- [`JustANormalTinkerer/hayai-finetuning-dataset-with-korean`](https://huggingface.co/datasets/JustANormalTinkerer/hayai-finetuning-dataset-with-korean/tree/764ca43fa4c57fd2db9ee5d3827f43dcdf9f69f6);
- multilingual
  [`wikimedia/wikipedia`](https://huggingface.co/datasets/wikimedia/wikipedia)
  text;
- [`globis-university/aozorabunko-clean`](https://huggingface.co/datasets/globis-university/aozorabunko-clean)
  text.

The language-prior corpora publish terms: Wikipedia declares CC BY-SA 3.0 and
GFDL, and Aozora Bunko clean declares CC BY 4.0 while describing a filter for
public-domain works.

The image data is the blocker. The one-million-image merged corpus returns 401
to an unauthenticated audit and has no available license or source methodology.
The latest public fine-tuning dataset revision predating the pinned model
commit, `764ca43fa4c57fd2db9ee5d3827f43dcdf9f69f6`, contains 38,907 crops but its
card names no license, source collections, or acquisition rights. The model
card does not pin the dataset revision, so this is an audit proxy rather than a
confirmed training snapshot. The model's Apache-2.0 declaration is clear, but
it does not supply the missing provenance for those training images. Frank
should not redistribute or automatically download these weights in a release
until that gap is resolved.

## Frozen accuracy evidence

No OCR inference was repeated because the implementation, weights, crops, and
verified transcriptions are unchanged. The frozen set contains 37 verified
Japanese crops: 19 ordinary and 18 difficult.

| Engine or policy | Overall CER | Exact match | Difficult CER | Difficult exact match |
|---|---:|---:|---:|---:|
| Frank Manga OCR | 4.13% | 75.68% | 25.00% | 66.67% |
| Hayai on every crop | 1.80% | 86.49% | 5.26% | 88.89% |
| Oracle: Manga OCR ordinary + Hayai difficult | **1.44%** | **86.49%** | **5.26%** | **88.89%** |

The oracle selection improved six difficult cases, tied eleven, and regressed
one. It shows the potential value of keeping Manga OCR on ordinary crops while
using Hayai on a well-selected subset. It does not establish how production can
identify that subset.

The reproducible evidence scorer is
[`scripts/evaluate_hayai_routing.py`](scripts/evaluate_hayai_routing.py). It
verifies the frozen score-file checksum and traces every crop's geometry and
score to Koharu's semantic detector `text` output:

```bash
python evaluation/koharu_baseline/scripts/evaluate_hayai_routing.py
```

The `hard` category was constructed from Koharu text detections below 0.95; the
`high` category contains Koharu text detections above that value. These are not
scores from Frank's retained RT-DETR detector. Using 0.95 in Frank would mix two
different models' confidence scales. The 1.44% result is therefore an
oracle/category quality bound rather than a deployable confidence policy.

### Current production signals

The scorer retrospectively associates each Koharu text crop with the smallest
current Frank region containing its center. This is enough to audit signal
coverage, with two material limits: production OCR receives Frank's larger
parent crop rather than the evaluated Koharu text crop, and seven of the 18
difficult text crops have no current Frank parent region, so they never reach
the OCR router.

| Observable signal on this corpus | Difficult selected | Ordinary selected |
|---|---:|---:|
| Manga OCR fails current Japanese validator | 5/18 | 0/19 |
| Manga OCR output has fewer than 15 meaningful characters | 18/18 | 5/19 |
| Manga OCR output contains a digit | 3/18 | 2/19 |
| Manga OCR output is punctuation only | 2/18 | 0/19 |
| Evaluated Koharu text crop is horizontal | 6/18 | 4/19 |
| No current Frank parent region exists | 7/18 | 0/19 |

A post hoc Frank parent-confidence cutoff below `0.9425190687179565`, counting
missing regions as selected, covers 16/18 difficult and 0/19 ordinary cases.
Only nine of those difficult cases are routeable; seven are detector misses. A
cutoff that includes every matched difficult case (`<= 0.9558324217796326`)
also selects four ordinary cases. Both cutoffs were inspected on this same
selected corpus and are descriptive, not calibrated settings.

Short output is the only recorded OCR signal with full difficult-set coverage,
but it also routes five ordinary cases and Hayai regresses one correct short
reading. The existing two-crop consistency result was not recorded in the
benchmark. No crop matched a current artwork-text parent. There is consequently
no observed production signal or combination in this evidence that validates
the oracle selection without a held-out end-to-end test.

## Candidate product routing

Manga OCR remains primary for Japanese dialogue and prose. The next held-out
evaluation should measure Hayai as a candidate when at least one recorded
reason applies:

1. the current detector confidence is below a threshold calibrated on separate
   training pages for that exact detector revision;
2. the semantic kind is SFX or artwork text;
3. Manga OCR fails Frank's existing validity or two-crop consistency check;
4. the crop is explicitly classified as short numeric, multilingual, stylized,
   or horizontal text by deterministic geometry/script rules.

No trigger above is accepted for production by this frozen corpus. In
particular, detector confidence and geometry must be evaluated using current
Frank regions and the exact crops sent to OCR.

When evaluated, use Hayai's result only if it passes the same Unicode, length, and
hallucination validation as Manga OCR. Store primary text, alternative text,
routing reasons, engine revision, and selected result in the page document.
Do not route prose chunks or Korean webtoons until their own verified sets show
no regression. Hayai emits no calibrated confidence score, so a fabricated
confidence value must not be stored.

Short output alone is not a sufficient reason to replace Manga OCR. Hayai
shortened the correct repeated SFX `わーわー` to `わー` in the frozen set.
Routing failure, timeout, OOM, invalid output, or an unavailable model must keep
the valid Manga OCR result and record degraded provenance.

## Accuracy and resource acceptance checks

Promotion requires all of the following on a held-out current-pipeline corpus
and production worker image:

- overall CER at most 1.80% and exact match at least 86.49%;
- difficult-crop CER at most 5.26% and exact match at least 88.89%;
- no ordinary-dialogue or prose regression;
- end-to-end routing metrics from current Frank detector regions and crops,
  without treating undetected text as routed;
- threshold calibration pages kept separate from final reporting pages;
- deterministic tests for every routing reason, invalid output, timeout, OOM,
  and unavailable-model fallback;
- exact artifact checksum verification before load and no remote code;
- cold and warm latency, peak host/device memory, and two-replica coexistence
  measured in Frank's actual ROCm image.

The scorer compares the underlying integer error and exact-match counts. The
percentages above are rounded for display and are not used as truncated numeric
thresholds.

The existing isolated Strix run already proves the Koharu implementation fits:
one cached crop took 1.144 seconds and increased observed device memory by about
399 MiB. Cold initialization took 32.973 seconds. The full 38-crop execution,
including one excluded browser-toolbar crop, averaged 1.125 seconds per crop.
The exact cached artifacts on the server were rehashed during this audit and
matched the manifest. The 29.2 MB evaluation binary hash is
`838e11b48651f7a119a0071d823eb57117ea44700a5eae683a101475e7843caf`.
The exact Frank Python adapter was then exercised in the production ROCm worker
image on Strix Halo with a frozen numeric crop whose reference output is `102`.
One 8 GiB container matched the reference three times: the first call, including
process and model startup from a local verified snapshot, took 4.239 seconds;
the two warm calls averaged 0.032 seconds. Two 8 GiB containers then ran 100
calls each concurrently. Both matched 100/100, exited normally, and were not
OOM-killed. Warm calls averaged 0.041 seconds in each container. Sampled
container memory peaked at 1.244 GiB and 943.9 MiB (0.922 GiB), while observed device memory
rose by 2,839,404,544 bytes above the pre-run baseline. This establishes runtime
and two-replica capacity for the optional adapter; it does not clear the
training-data or production-routing blockers. The reproducible entry point is
[`scripts/smoke_hayai_adapter.py`](scripts/smoke_hayai_adapter.py).
