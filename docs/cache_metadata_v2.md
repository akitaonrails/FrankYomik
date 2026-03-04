# Cache + Metadata V2

## Scope
Applies to:
- Kindle Japanese pipelines (`manga_furigana`, `manga_translate`)
- Naver Webtoon Korean pipeline (`webtoon`)

## Filesystem layout

```
<cache>/
  v2/
    objects/
      aa/bb/<sha256>                    # content-addressed blobs
    pages/
      by-hash/
        <pipeline>/<source_hash>/
          manifest.json                 # authoritative record
      by-ref/
        <pipeline>/<title_slug>/<chapter>/<page>.json  # optional pointer
```

## Manifest
`manifest.json` fields (authoritative):
- `schema_version`
- `pipeline`
- `source_hash`
- `source_object_sha256`, `source_bytes`
- `image_object_sha256`, `image_bytes`
- `metadata_object_sha256`, `metadata_bytes`
- `content_hash` (sha256 of canonical metadata JSON)
- `render_hash` (sha256 of `v2|content_hash|image_object_sha256`)
- `image_stale` (true after metadata edit until rerender completes)
- timestamps

## Metadata payload (object blob)
Top-level:
- `schema_version`
- `pipeline`
- `source_hash`
- `image`: `{ width, height }`
- `regions`: array

Region entry:
- `id`
- `kind`: `bubble | artwork_text | sfx`
- `bbox`: pixel `[x1,y1,x2,y2]`
- `bbox_norm`: normalized `[0..1]`
- `ocr_text`
- `is_valid`
- `transformed`: `{ kind, value }`
- `user`:
  - `manual_translation`

## API
- `GET /api/v1/cache/by-hash/{pipeline}/{source_hash}/image`
- `GET /api/v1/cache/by-hash/{pipeline}/{source_hash}/meta`
- `PATCH /api/v1/cache/by-hash/{pipeline}/{source_hash}/meta`
  - body:
    - `base_content_hash` (optimistic concurrency)
    - `metadata` (full payload)
    - `priority`

Patch flow:
1. Server updates metadata object + manifest (`image_stale=true`).
2. Server enqueues worker rerender with `rerender_from_metadata=1`.
3. Worker rerenders from metadata (no redetect/retranslate).
4. Worker writes fresh rendered image + manifest (`image_stale=false`).

## Robustness rules
- All file writes are atomic (temp file + rename).
- All object reads are SHA-verified.
- Cache lookup is hash-first (`pipeline + source_hash`), with optional by-ref pointer.
- Metadata edits change `content_hash`; stale image is never served while rerender is pending.
