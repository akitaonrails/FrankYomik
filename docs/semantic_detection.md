# Semantic detection contract

Phase 1 introduces a backend-neutral detection result without changing detector
weights. The current pinned RT-DETR checkpoint remains the production model.
The evaluated compact v4 candidate is blocked: it failed the coverage and
interface false-positive gates, has the same three labels rather than the four
semantic classes, provides no masks, and lacks named training-data provenance.
Koharu weights are not packaged or selected by this change. The measurements
and exact artifact identity are in
[`phase1_detector_evaluation.md`](../evaluation/koharu_baseline/phase1_detector_evaluation.md).

## Region meanings

The contract in `server/pipeline/detection.py` has four kinds:

| Kind | Meaning |
|---|---|
| `text` | Text geometry inside or outside a container. |
| `sfx` | Free/decorative text processed by the artwork-text path. |
| `balloon` | A dialogue or narration container. |
| `panel` | A panel/frame boundary retained for layout reasoning. |

Every detection has a stable detector-local ID, confidence, half-open bounding
box, raw model label, and detector provenance. Polygon, in-memory mask,
parent, and reading order are also supported. A text detection whose center is
inside a balloon, or whose area overlaps it by at least 50%, links to the best
balloon. Full containment wins, followed by overlap and the smallest enclosing
balloon. Stable IDs break any remaining tie.

## Current RT-DETR adapter

The compatibility mapping is:

| Raw label | Semantic kind | Minimum confidence |
|---|---|---:|
| `bubble` | `balloon` | 0.35 |
| `text_bubble` | `text` | 0.35 |
| `text_free` | `sfx` | 0.60 |
| `panel` or `frame` | `panel` | 0.50 |

The pinned model currently emits the first three labels. The panel aliases let
another backend implement the same contract without changing PageDocument.
Unknown labels are ignored.

Duplicate suppression compares regions only within the same semantic class.
This preserves an overlapping balloon and contained text instead of treating
one as a duplicate of the other. Manga order is top to bottom with regions in
the same visual row ordered right to left. Vertically overlapping regions and
regions separated by at most one quarter of the smaller region height form a
row. When panels are available, panels are ordered in visual rows first and
each panel's contents are ordered within that panel. Webtoon order is top to
bottom, then left to right. Geometry and IDs provide deterministic tie breaks.

Detector output order does not participate in identity. The adapter sorts the
normalized observations before assigning duplicate suffixes, so permuting an
otherwise identical model result produces the same IDs, parent links, and
reading order. The collection boundary rejects duplicate IDs, duplicate
reading-order values, dangling parents, and relationships other than text
inside a balloon.

## Compatibility and masks

The current OCR and rendering code still consumes its previous dictionary
shape. A compatibility projection selects each balloon, uncontained text
region, and SFX region. Contained text is retained in PageDocument as a child
of its balloon and is not OCRed twice.

PageDocument retains each semantic detection's exact reading-order value. A
render-only record, such as an additional text subgroup within one webtoon
balloon, receives an order after the complete semantic range. It cannot take a
semantic order or cause a later detector region to be renumbered.

Frank's connected-component mask extractor remains active for ordinary manga
balloons. The contour is compacted into a polygon for PageDocument. The mask
reference uses `derived://source/<region-id>` with encoding
`connected-component-v1`, meaning the mask is deterministically regenerated
from the source image and region geometry during rerender. Pixel arrays are
not embedded in JSON. Webtoon balloon masks use `webtoon-boundary-v1` and keep
the existing boundary extraction and cleanup policy.

Tall webtoon strips run detection per overlapping strip. Coordinates and
parent IDs are moved into page space, same-class overlap duplicates are
removed, relationships are rebuilt, and reading order is assigned only after
stitching.

## Verification

The model-free contract suite covers class-specific thresholds, same-class
suppression, balloon/text retention, deterministic linking, and manga/webtoon
reading order:

```bash
cd server
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/unit/test_pipeline_detection.py \
  tests/unit/test_processor.py \
  tests/unit/test_webtoon_processor.py \
  tests/integration/test_worker_pipeline_contract.py
```

The four-pipeline consumer integration test also verifies that balloon masks,
polygons, raw labels, and text-to-balloon relationships survive cache storage
and metadata rerendering. A later detector replacement must pass the frozen
benchmark and license checks in the migration plan; the evaluated v4 artifact
does not.
