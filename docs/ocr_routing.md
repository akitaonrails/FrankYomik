# Manga OCR routing

Manga pages use a typed crop-level OCR boundary. The selected result carries
text, validity, optional calibrated confidence, quality signals, engine
provenance, alternatives, and a deterministic routing reason. `PageDocument`
persists the selected provenance, alternatives, and routing reason on each
region.

## Policy

Manga OCR remains the production engine for ordinary Japanese dialogue and for
every prose chunk. The experimental router may consult Hayai OCR v2.1 for manga
artwork/SFX, mixed-script or numeric output, punctuation-only output, invalid Japanese, failed two-crop
consistency, or explicitly horizontal crop geometry. A short Manga OCR string
alone does not route: the frozen evaluation includes a correct repeated SFX
that Hayai shortened.

When Hayai returns a usable candidate, it becomes selected and Manga OCR is
stored as an alternative. Numeric, punctuation, mixed-script, and artwork
routes use script-aware validation; ordinary dialogue keeps Frank's Japanese
validation. Hayai exposes no calibrated confidence, so its persisted confidence
is null. Crop overlap remains a quality signal and is not presented as OCR
confidence.

A Hayai timeout terminates its child process, including the accelerator work.
An unavailable backend or OOM retains a valid Manga OCR result, records the
degraded failure class in `routing_reason`, and makes the result non-cacheable.
The public job status remains `completed`. Unexpected backend errors fail the job. This prevents a
transient fallback from entering the healthy pipeline cache.

Korean webtoons continue to use EasyOCR. Book furigana keeps the existing Manga
OCR prose chunks. Neither path initializes Hayai.

## Model status

The implementation is pinned to
`JustANormalTinkerer/hayai-ocr-v2@4a4ce477c9a8841f208b94e1d9ed5c0938965e05`
and greedy decoding with 256 image patches, 256 output tokens, and repetition
penalty 1.0. Runtime accepts only the allowlisted safetensors, JSON, and
tokenizer files after SHA-256 verification. Reviewed model code and the exact
SigLIP2 configuration are local, so `trust_remote_code` is never used.

Hayai is disabled in `server/config.yaml`, and the adapter only reads a locally
pre-staged Hugging Face snapshot; it cannot download weights. Its declared
artifact license is
Apache-2.0, but the training image datasets lack sufficient source and license
statements for release distribution. See
[evaluation evidence](../evaluation/koharu_baseline/phase2_hayai_evaluation.md)
and [third-party notices](../server/THIRD_PARTY_NOTICES.md).

The frozen 1.44% selective-policy CER is an oracle upper bound based on known
difficult labels and Koharu detector confidence. That 0.95 confidence boundary
does not apply to Frank's retained RT-DETR revision, and seven difficult crops
currently lack a Frank processing parent. Production routing needs a new
end-to-end calibration before Hayai can be enabled.

The optional adapter itself passed the exact production ROCm runtime gate on
Strix Halo. It reproduced the frozen `102` crop three times in one 8 GiB
container, then 100/100 times in each of two simultaneous 8 GiB containers.
Neither container was OOM-killed; sampled container memory stayed below 1.25
GiB and incremental observed device memory peaked at 2.84 GB. These results
establish technical capacity only and do not change the disabled default.
