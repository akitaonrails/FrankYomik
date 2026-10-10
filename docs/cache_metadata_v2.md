# Cache and page metadata

## Scope

The cache stores output for these reader pipelines:

- `manga_furigana`
- `manga_translate`
- `book_furigana`
- `webtoon`

Public image and metadata URLs remain unchanged. Cache manifest version 4 and
page metadata version 2 are separate schemas even though they currently share
the same storage tree.

## Cache identity

A processed render is identified by:

```text
cache_namespace + source_hash + pipeline_revision
```

`source_hash` is the SHA-256 digest of the original image. `pipeline_revision`
identifies the complete set of stage configurations and pinned model revisions
that affect output. Changing a detector, OCR model, translation prompt or model,
cleanup policy, or renderer must produce a new pipeline revision.

The cache namespace is the base pipeline name for English and
`<pipeline>_<target-language>` for other languages. For example, a Portuguese
manga translation is stored under `manga_translate_pt-br`, but the
`PageDocument.pipeline` inside it remains `manga_translate`. API cache paths and
manifest `pipeline` fields use the namespace. The cache splits it back into the
base pipeline and target language when validating metadata or queueing a
rerender.

The cache revision is derived from the pinned
[pipeline stage manifest](pipeline_stage_manifest.md). The API and worker
verify its named artifacts and independently compute the same SHA-256
fingerprint. `PIPELINE_REVISION` may assert the expected value; a mismatch
stops startup. `legacy-v1` is available only with the explicit
`ALLOW_LEGACY_PIPELINE_REVISION=1` migration setting. A manifest created before
revisioning has no `pipeline_revision` field and is treated as `legacy-v1`.
Old queued messages that omit the field also map to `legacy-v1`; missing
revision data never inherits the current derived revision.

A manifest whose revision differs from the running pipeline is a cache miss.
The filesystem paths do not include the revision, so the next successful write
replaces the page manifest while retaining deduplicated content-addressed
objects. Rolling back to another revision causes that revision to reprocess the
page.

The API and every worker must run the same revision. The API includes it in
Redis dedup identity and job messages. A worker rejects a job from another
revision instead of mixing stage implementations, and API cache lookup rejects
a manifest from another revision.

## Filesystem layout

```text
<cache>/
  v2/
    objects/
      aa/bb/<sha256>
    pages/
      by-hash/
        <pipeline>/<source_hash>/
          manifest.json
      by-ref/
        <pipeline>/<title_slug>/<chapter>/<page>.json
```

Objects contain the source image, rendered image, or canonical metadata JSON.
The manifest is authoritative. A by-reference record is an optional pointer
from reader metadata to the source hash.

## Manifest schema version 4

`manifest.json` contains:

- `schema_version`: `4`
- `pipeline`: durable cache namespace, including a non-English language suffix
- `pipeline_revision`; omitted only by legacy manifests
- `source_hash`
- `source_object_sha256`, `source_bytes`
- `image_object_sha256`, `image_bytes`
- `metadata_object_sha256`, `metadata_bytes`
- `artifact_object_sha256s`: cleanup base and mask object digests referenced
  by the document
- `content_hash`: SHA-256 of canonical metadata JSON
- `render_hash`: SHA-256 of `v2|content_hash|image_object_sha256`
- `image_stale`: true after a metadata edit until rerender completes
- `created_at_unix_ms`, `updated_at_unix_ms`
- optional reader reference fields: `title_slug`, `chapter`, `page`

Manifest version 2 has no `pipeline_revision`; it is read as the `legacy-v1`
revision. Version 3 predates document raster artifacts. Both remain readable.
The next page store or metadata update promotes them to version 4. Unknown
manifest versions are cache misses.

Unknown JSON fields are safe for older Go readers to ignore. Writers must
preserve the revision when rewriting a current manifest.

## Page metadata objects

New pipeline results store a `PageDocument` schema version 2 metadata object.
Its fields, nested objects, validation, and canonical serialization are defined
in [PageDocument schema version 2](page_document_v2.md). Cache writes validate
the complete document before storing it. The document contains the executable
base pipeline even when its manifest uses a language-qualified namespace.
Python and Go use the shared canonical
fixture in `server/tests/fixtures/page_document_v2_canonical.json`; canonical
JSON keeps UTF-8, `<`, `>`, `&`, U+2028, and U+2029 unescaped, sorts object
keys, normalizes exponent and trailing-zero number forms, and rejects
non-finite or out-of-range numbers. Schema-owned PageDocument objects reject
unknown fields in both runtimes. The full normalization standard is in
[PageDocument schema version 2](page_document_v2.md#canonical-json-and-content-hashes).
Before hashing, both runtimes also apply the same missing/null normal form:
nullable members are omitted and semantic defaults are materialized. This
prevents an HTTP editor from changing `content_hash` only by spelling an
absent value as `null`. The exhaustive shared fixture is
`server/tests/fixtures/page_document_v2_normal_form.json`.

### Reading schema version 1

Metadata version 1 is the legacy editable region payload. It uses flat fields
such as `ocr_text`, `transformed`, `source_font_size`, and
`user.manual_translation`. Cache reads preserve it as version 1 so callers can
use the legacy rerender path without mistaking it for a PageDocument.

The object on disk is immutable and is not rewritten during a read. New full
pipeline results write PageDocument version 2. A later explicit adapter may
convert a legacy payload only when it can construct every required PageDocument
field correctly. Unknown future schema versions are rejected so an older worker
cannot silently discard new data.

## API

- `GET /api/v1/cache/by-hash/{pipeline}/{source_hash}/image`
- `GET /api/v1/cache/by-hash/{pipeline}/{source_hash}/meta`
- `PATCH /api/v1/cache/by-hash/{pipeline}/{source_hash}/meta`

The patch body contains `base_content_hash`, the full `metadata` object, and an
optional `priority`. The update flow is:

For English, `{pipeline}` is the base pipeline name. For other target
languages, it is the language-qualified cache namespace such as
`manga_translate_pt-br`.

1. verify `base_content_hash` for optimistic concurrency;
2. require every content-addressed reference to belong to the existing
   manifest and verify its object hash;
3. store canonical metadata and mark the image stale;
4. enqueue a rerender with `rerender_from_metadata=1`;
5. rerender from the exact cleanup base without detection, OCR, translation,
   or cleanup;
6. write the fresh image and clear the stale flag.

The cross-runtime boundary test exercises the HTTP PATCH handler, canonical
cache write, Python cache load and metadata-only rerender, then compares the
Python hash with the hash returned by the handler.

## Robustness rules

- Python cache writes flush and `fsync` the temporary file before atomic rename.
  Go cache writes use a temporary file and atomic rename but do not currently
  issue `fsync`; neither implementation synchronizes the containing directory.
- Object reads verify their SHA-256 digest.
- Raster objects are bounded before PNG decode and checked for declared mode
  and dimensions. Metadata edits cannot introduce an object outside the
  page's existing artifact allowlist.
- Pipeline, source hash, and pipeline revision claims in metadata must agree
  with the cache lookup.
- Python and Go both perform strict PageDocument type, graph, hash, pipeline,
  revision, and geometry validation before an object or manifest is changed.
  Invalid API patches return HTTP 400.
- Stale images are not served while rerendering is pending.
- Cache misses trigger normal processing; they do not delete immutable objects.
