# Phase 6 Strix Halo release rehearsal

The production-shaped rehearsal completed on 2026-10-10 against two ROCm
workers on the Strix Halo host. Private page inputs, titles, tokens, source
hashes, and response payloads remain only in the protected homelab evidence
directory.

## Candidate identity

- Candidate: `v2.0.0-rc.11`
- Pipeline revision: `sha256:2f4fee363797d26338e69fdba4b1cf2facac7201dbe69e9dd3183b5049d3849a`
- API image digest: `sha256:c8a57a394c8f98943a2081605247ab6c14b8315d8823f14558a96a75dd4b7161`
- ROCm worker image digest: `sha256:39ab3339ed1438165cd1dc7415306cd455040e7e4665c57b3777d606d9cd8e0b`
- Evidence inventory: 38 files, 11,175,001 bytes; name-and-size inventory SHA-256 `432c78c91f3e81f35a55d55aa5269c2774c4d35aed746372adbee49d91c1b82a`

The inventory checksum proves which private evidence set was reviewed without
publishing page-derived hashes or content.

## Pipeline results

| Pipeline | Cold result | Regions | Cache check |
|---|---:|---:|---|
| Manga furigana | 13 s | 10 | Warm response byte-identical |
| Book furigana | 8 s | 15 | Warm response byte-identical |
| Manga translation to Portuguese | 23 s | 18 | Warm response byte-identical in the language-scoped namespace |
| Webtoon translation | 71 s | 26 | Completed as degraded and intentionally noncacheable |

Every completed result used cache manifest schema 4. Manga and webtoon
metadata contained verified PageDocument object references and hashes. The
webtoon LaMa backend returned an invalid output size; the supervised cleanup
path rejected it, restored the last safe image, applied the bounded
deterministic fill, marked the affected provenance, and retained transient
metadata for inspection.

## Operational scenarios

- Two simultaneous identical submissions produced one stream entry and one
  shared job; the second response reported a deduplication hit.
- The worker owning a pending entry was stopped. The survivor reclaimed it
  after the configured idle threshold, completed it, and left the pending
  count at zero. The stopped worker then rejoined with all capabilities.
- Both workers passed detector, OCR, translation, font, cleanup, GPU device,
  and exact pipeline-revision preflight before becoming ready.
- The preserved v1 API and worker images were started against an isolated clone
  of the cache. A legacy result was read successfully, v2 was restored, and
  the live v2 cache inventory was byte-for-byte unchanged.

## Release decision

Phase 6 passes. The shipped decisions are RT-DETR with the semantic detection
contract, Manga OCR with the verified routing contract, page-batched
translation, bounded LaMa or deterministic cleanup, PageDocument rendering,
and schema 4 persistence. Hayai and FLUX remain evaluated candidates rather
than production dependencies for the reasons recorded in `docs/models.md`.
