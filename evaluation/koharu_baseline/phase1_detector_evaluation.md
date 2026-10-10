# Phase 1 detector candidate evaluation

## Decision

Keep Frank's pinned RT-DETR-v2 detector as the production default. Do not
promote ogkalu's compact v4 ONNX artifact as the semantic detector.

The compact model is useful packaging work, but it does not supply Koharu's
`text`, `sfx`, `balloon`, and `panel` contract, instance masks, or most of the
audited SFX coverage. Its balanced threshold also introduces browser chrome
and artwork false positives. The available model card names broad comic styles
but does not identify the training datasets or their terms, so the weights do
not satisfy Phase 1's training-data provenance gate.

## Candidate identity and terms

- Repository: `ogkalu/comic-text-and-bubble-detector`
- Repository revision: `16e8a622f91fabc6b5b65c96d32d1183f8843546`
- Artifact: `detector-v4-s_int8.onnx`, 11,120,765 bytes
- SHA-256: `5fe9e4f576e49d4e7e8b0e029d6d3cdc252abd4694113e1cae120e62c931ea79`
- Declared license: Apache-2.0
- Declared training statement: about 11,000 resized images described only as
  manga, webtoon, manhua, and Western comic styles; tall webtoons were split
  vertically.

The exact [model revision and file](https://huggingface.co/ogkalu/comic-text-and-bubble-detector/tree/16e8a622f91fabc6b5b65c96d32d1183f8843546)
provide the license declaration and artifact identity. The model card does not
name the datasets, acquisition method, annotation source, or dataset licenses.
Apache-2.0 is a clear grant from the uploader for the artifact, but it cannot by
itself establish rights in unidentified training material.

Frank's current safetensors checkpoint remains pinned at repository revision
`4269a0e53d40419f4746e8b3e30ae7faef3a26c6`, artifact SHA-256
`037930a861e67870eb345be01b28cc70d7e2b7956528e48ee0ebdb0c093df80d`.
Both artifacts have the same three output labels: `bubble`, `text_bubble`, and
`text_free`.

## Measured result

The candidate ran with single-threaded ONNX Runtime CPU on all 19 frozen pages.
Mean model inference time was 0.143 seconds per page and median time was 0.142
seconds. These timings exclude image loading and postprocessing.

Matching uses the same audited rule as the original comparison: a pair matches
when either box center lies inside the other box, followed by greedy one-to-one
assignment. Koharu's 367 text/SFX regions are an audited baseline rather than
independently drawn ground truth.

| Bubble/text threshold | Free-text threshold | Candidate regions | Matched current 290 | Missed current | Matched Koharu 367 | Missed Koharu |
|---:|---:|---:|---:|---:|---:|---:|
| 0.35 | 0.60 | 278 | 277 | 13 | 278 | 89 |
| 0.30 | 0.30 | 295 | 285 | 5 | 286 | 81 |
| 0.25 | 0.25 | 299 | 285 | 5 | 288 | 79 |
| 0.20 | 0.20 | 302 | 285 | 5 | 288 | 79 |
| 0.10 | 0.10 | 367 | 287 | 3 | 298 | 69 |

At the balanced 0.30 threshold, ten regions were new relative to Frank's
frozen output. Visual review found three useful additions: a numeric/sign crop,
a stylized SFX crop, and a dialogue crop. The other seven were two browser
toolbars, two browser URL strips, a face, a cat face, and a vending-machine
illustration. Of the five current regions it dropped, four were known false
positives and one was a genuine dialogue balloon. This yields a small net
coverage gain and violates the gate that no browser or interface region reaches
translation. The per-region decisions are retained as machine-readable
[`phase1_detector_v4_disagreements.json`](annotations/phase1_detector_v4_disagreements.json)
annotations and are validated by the evaluator.

Lowering the threshold did not recover the semantic gap. At 0.10 the candidate
still missed 69 Koharu text/SFX regions while adding 80 regions relative to the
current detector. It therefore cannot cover Frank's 284 baseline matches plus
Koharu's 63 audited useful additions.

Containment deduplication was stable from 0.40 through 0.80 at the balanced
score threshold. A 0.30 deduplication threshold suppressed two additional
matched regions. Frank's existing 0.50 rule remains appropriate for this
three-class detector family.

## Mask and webtoon policy

The ONNX graph returns labels, boxes, and scores, with no instance masks. Using
the candidate boxes with Frank's existing bright-component mask extractor
produced 236 matched balloon masks against the Koharu baseline: mean IoU 0.8753
and median IoU 0.9521. The median narrowly clears the existing 0.9519 result,
so the hybrid mask policy is sound; the candidate adds no mask improvement of
its own.

No labeled tall Korean webtoon source is present in the frozen evaluation set.
The local webtoon images under `output/` are already rendered/debug products
and cannot serve as detection truth. A webtoon run was unnecessary because the
candidate already fails the manga coverage, false-positive, semantic-output,
and provenance gates. Keep the current webtoon detector unchanged until a
verified source fixture exists.

## Reproduction

From the repository root:

```bash
mkdir -p evaluation/koharu_baseline/outputs/phase1-detector-candidate/models
curl -fL \
  -o evaluation/koharu_baseline/outputs/phase1-detector-candidate/models/detector-v4-s_int8.onnx \
  https://huggingface.co/ogkalu/comic-text-and-bubble-detector/resolve/16e8a622f91fabc6b5b65c96d32d1183f8843546/detector-v4-s_int8.onnx
python evaluation/koharu_baseline/scripts/evaluate_detector_candidate.py \
  --model evaluation/koharu_baseline/outputs/phase1-detector-candidate/models/detector-v4-s_int8.onnx
```

The script verifies the artifact checksum before inference and writes the
machine-readable report under the ignored `outputs/` directory. It requires
the frozen Frank and Koharu raw outputs from the completed baseline run.

## What a promotable detector must add

A future production candidate needs four classes, retained instance masks,
documented training-data provenance, and a permissive redistribution path. It
must reach the audited useful SFX additions without passing page chrome or page
numbers downstream. Until such a checkpoint exists, retain Frank's current
detector and implement the semantic contract and Koharu-style postprocessing so
that a later model can be substituted without another pipeline rewrite.
