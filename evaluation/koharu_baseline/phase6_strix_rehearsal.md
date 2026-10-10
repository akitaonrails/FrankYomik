# Phase 6 Strix Halo release rehearsal

The production-shaped rehearsal completed on 2026-10-10 against two ROCm
workers on the Strix Halo host. Private page inputs, titles, tokens, source
hashes, and response payloads remain only in the protected homelab evidence
directory.

## Accepted candidate identity

- Candidate: `v2.0.0-rc.12`, commit `6f799a1b8b48`
- Pipeline revision: `sha256:ccca09e65f58280fb20db34634af92b43610196242787f8c903347dd787a6e89`
- API image digest: `sha256:02535d60bd04af7b952333b0e3385708e0ce35a3c8ec25c2bc50a34ffc7cd3a4`
- ROCm worker image digest: `sha256:6cba4a0a35f14b2dfb737072cbeccf910ad1b2f0b3ae969cb4dae5ba41e2cb3d`
- Evidence inventory: 40 files, 11,170,926 bytes; name-and-size inventory SHA-256 `fbc8432ef44924bf11256c2c1f43bda3e9430911d0e66a79eaeb6a5b090e27be`

The inventory checksum proves which private evidence set was reviewed without
publishing page-derived hashes or content.

RC.11 supplied the detailed timings and operational fault injection below. Its
release scan then found fixed high-severity findings in the Go, Ubuntu, Pillow,
and Transformers layers. RC.12 contains those updates. The complete local suite
and all four live pipelines were repeated on RC.12, and both GPU workers became
ready at its exact revised pipeline fingerprint.

## Detailed RC.11 pipeline results

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
