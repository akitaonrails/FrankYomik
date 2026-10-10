# PageDocument schema version 2

`PageDocument` is the canonical JSON metadata produced for every newly
processed page. It records what each pipeline stage found and produced while
the rendered image remains the reader-facing result.

This document defines the metadata payload schema. It does not define the
cache manifest. The cache manifest is schema version 3 and is documented in
[Cache and page metadata](cache_metadata_v2.md).

The executable definition and validation rules live in
`server/pipeline/document.py`.

## Top-level object

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | Always `2`. |
| `page_id` | non-empty string | Page identity used when deriving region IDs. |
| `pipeline` | non-empty string | One of the four user-facing pipeline names. |
| `pipeline_revision` | non-empty string | Deployment-wide implementation and model revision. |
| `source_hash` | non-empty string | SHA-256 identity supplied for the source image. |
| `image` | object | Positive integer `width` and `height` in pixels. |
| `regions` | array | Ordered normalized page regions. |
| `stage_provenance` | object | Stage name to provenance object. |

The document uses strict JSON types: booleans are not integers, numeric values
must be finite, the source hash must be 64 lowercase hexadecimal characters,
and the pipeline and revision must be valid. Region IDs and reading-order
values must be unique. Boxes, polygons, and render boxes must fit within the
image; mask dimensions must match it; and every `parent_id` must name another
region without creating a cycle. Python and Go enforce the same cache-boundary
contract. Serialization sorts `stage_provenance` and JSON object keys so the
same document has a deterministic content hash.

Schema-owned objects reject unknown fields in both Python and Go. This applies
to the document, image, region, provenance, mask, OCR, translation, cleanup,
render, transform wrapper, and user-edit objects. `transform.value` remains an
arbitrary JSON value because its structure belongs to the named transform.
Rejecting additions prevents one runtime from hashing data that the other
silently discards. A new field therefore requires a schema version or a
coordinated validator update.

### Missing, null, and default values

Validation produces one normal form before serialization and hashing. Nullable
fields accept either an absent member or explicit `null`; the normal form omits
the member. This rule covers every optional region reference and scalar, plus
optional provenance, mask, OCR, translation, cleanup, and render members.

Fields with semantic defaults accept an absent member, explicit `null`, or the
explicit default. The normal form materializes these values:

| Field | Normal value |
|---|---|
| top-level `regions` | `[]` |
| top-level `stage_provenance` | `{}` |
| `ocr.alternatives` | `[]` |
| `transform.value` | `null` |
| `render.vertical` | `false` |
| region `user` | `{"manual_translation":""}` |
| `user.manual_translation` | `""` |

Required fields do not gain a default and reject `null`. An arbitrary
`transform.value` retains nulls and object members inside that value because it
is outside the schema-owned wrapper. The shared
`server/tests/fixtures/page_document_v2_normal_form.json` fixture enumerates
all nullable and defaulted schema fields and is executed against both runtimes.

## Canonical JSON and content hashes

Python and Go use the same canonical serializer for metadata objects:

- object keys are sorted and no insignificant whitespace is emitted;
- strings are UTF-8 JSON strings; `<`, `>`, `&`, U+2028, and U+2029 remain
  literal characters;
- integer tokens retain their exact integer value;
- tokens with a fraction or exponent are interpreted as finite IEEE 754
  binary64 values, then written with trailing zeros removed, `-0` normalized to
  `0`, lowercase `e`, and no plus sign or exponent-leading zeros;
- ordinary magnitudes use decimal notation; scientific notation is used when
  the decimal point would fall after position 21 or at/before position -6;
- arrays retain their order and `null`, booleans, strings, and numbers retain
  their JSON type.

`content_hash` is SHA-256 over these exact UTF-8 bytes. The shared fixtures
`server/tests/fixtures/page_document_v2_canonical.json` and
`server/tests/fixtures/canonical_json_normalization.json` cover both runtimes,
including exponent and trailing-zero forms plus U+2028/U+2029. A Go cache
test sends metadata through the real HTTP PATCH handler, loads and rerenders
the stored object through Python, and verifies that the handler response, cache
manifest, and Python document produce the same hash.

## Region object

Every region has these fields:

| Field | Type | Meaning |
|---|---|---|
| `id` | non-empty string | Opaque geometry identity derived from page, kind, box, and an optional discriminator. |
| `kind` | non-empty string | Current pipelines emit `bubble`, `artwork_text`, `sfx`, and `text_column`. |
| `bbox` | four integers | Half-open pixel bounds `[x1, y1, x2, y2]`. |
| `reading_order` | non-negative integer | Unique order within the page. |
| `user` | object | Reader-owned edits; currently `manual_translation`. |

Optional region fields are:

- `confidence`, between zero and one;
- `polygon`, with at least three non-negative pixel points;
- `mask`, an external binary-mask reference;
- `parent_id`, for relationships such as text inside a balloon;
- `detector_label` and `detection_provenance`;
- `ocr`, `translation`, or `transform` stage results;
- `cleanup` and `render` metadata;
- `source_font_size`, when the source estimate is available.

Region kinds are intentionally open strings so later detectors can add a kind
without changing the wire format. Producers still need a documented semantic
meaning and consumers must ignore kinds they do not render.

Region IDs are stable when the same source, base pipeline, kind, geometry, and
duplicate encounter order are reproduced. Reading-order changes alone do not
change an ID. The deployment revision is not part of the hash, but a detector
upgrade can change geometry or duplicate ordering and therefore change IDs.
Clients must not treat them as permanent identities across detector or pipeline
upgrades.

## Nested objects

### Provenance

Every stage result can identify its producer:

```json
{
  "backend": "manga-ocr",
  "revision": "sha256:ac1dd7c1b8e85148087327fcc7a67b397b4d6676b3f02a9be511005906146946",
  "model": "kha-white/manga-ocr-base@aa6573bd10b0d446cbf622e29c3e084914df9741",
  "config_hash": "sha256:886c2bda586ff7c08b103bfc46b6a6aa3a8e2f56ca30cf55be1a40c4a7f80403"
}
```

`backend` and `revision` are required. `model` and `config_hash` are optional.
The revision is the revision of that stage result; the top-level
`pipeline_revision` identifies the deployed pipeline as a whole.

### Mask reference

Large masks stay outside the JSON document:

```json
{
  "reference": "mask-object-id",
  "encoding": "png",
  "width": 120,
  "height": 80,
  "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

The reference, encoding, width, and height are required. A supplied digest
must be a 64-character hexadecimal SHA-256 value.

### OCR

`ocr` contains `text`, `language`, `valid`, `provenance`, and an `alternatives`
array. Confidence and `routing_reason` are optional. Each alternative contains
text, provenance, and optional confidence.

### Translation and transform

`translation` contains `text`, `target_language`, `provenance`, and optional
`source_language`.

`transform` represents a non-translation operation such as furigana. It
contains a non-empty `kind`, a JSON-serializable `value`, and `provenance`.
Current kinds include `furigana_segments`, `furigana_ruby`, and `text`.

### Cleanup and render

`cleanup` contains a non-empty `method`, provenance, and an optional mask
reference.

`render` contains the rendered bounds and provenance. It can also record font
family, positive font size, vertical layout, and text color.

## Minimal example

```json
{
  "schema_version": 2,
  "page_id": "page-a1b2",
  "pipeline": "manga_translate",
  "pipeline_revision": "sha256:46ef6e1c7de8e1d65830f35cdfbddb6f33616d626d4415b1361381d02babf2d0",
  "source_hash": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "image": {"width": 1200, "height": 1800},
  "regions": [
    {
      "id": "r_8d30c83d668ef24a",
      "kind": "bubble",
      "bbox": [700, 100, 1050, 430],
      "reading_order": 0,
      "ocr": {
        "text": "こんにちは",
        "language": "ja",
        "valid": true,
        "provenance": {
          "backend": "manga-ocr",
          "revision": "sha256:ac1dd7c1b8e85148087327fcc7a67b397b4d6676b3f02a9be511005906146946",
          "model": "kha-white/manga-ocr-base@aa6573bd10b0d446cbf622e29c3e084914df9741",
          "config_hash": "sha256:886c2bda586ff7c08b103bfc46b6a6aa3a8e2f56ca30cf55be1a40c4a7f80403"
        },
        "alternatives": []
      },
      "user": {"manual_translation": ""}
    }
  ],
  "stage_provenance": {}
}
```

## Compatibility

Metadata schema version 1 remains readable and rerenderable through the legacy
flat-region path. Reads preserve it as version 1; they do not synthesize fields
or rewrite its immutable object. New complete pipeline results write
`PageDocument` version 2. Unknown metadata schema versions are rejected.

A version 2 document must carry `pipeline_revision`, and its pipeline, source
hash, and revision must agree with the cache operation. For a translated
non-English result, the cache manifest and URL use a language-qualified
namespace such as `manga_translate_pt-br`, while this document keeps the base
pipeline `manga_translate`. See
[Pipeline architecture](pipeline_architecture.md) for revision behavior and
[Cache and page metadata](cache_metadata_v2.md) for storage compatibility.
