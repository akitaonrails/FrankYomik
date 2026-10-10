# Version 2 compatibility

## Public API and clients

The four public pipeline names and the v1 HTTP image/job endpoints remain
stable in v2.0.0. Current v1.0.1 clients can submit jobs to a v2 server and
display completed images. Version 2 clients can also use richer progress and
metadata fields; clients must continue ignoring unknown response fields.

| Client | v1 server | v2 server |
|---|---|---|
| v1.0.1 Flutter or extension | Supported during rollback | Supported for submit, status, image, and WebSocket completion |
| v2.0.0 Flutter or extension | Basic image flow only; richer metadata may be absent | Supported and tested |

The bearer token remains in the `Authorization` header for HTTP. WebSocket
authentication accepts the negotiated WebSocket protocol; the old generic
query-token form is intentionally rejected to keep credentials out of URLs.

## Worker and API pairing

API and worker images are a release pair. They derive a pipeline revision from
the stage manifest, include it in jobs and heartbeats, and refuse mismatched
work. Do not mix a v1 worker with a v2 API on the live queue. Health is ready
only when at least one preflighted worker reports the API's exact revision.

## Persistent cache

Version 2 writes cache manifest schema 4 and PageDocument schema 2 metadata.
Cache keys include the pipeline revision and target language. Existing v1
results are treated as legacy and do not silently satisfy a v2 request. Content
objects remain addressable and deduplicated, so migration does not require
deleting the old cache.

Schema 2 and 3 cache manifests remain readable by the v2 worker where their
required invariants are present. Schema 1 metadata uses the legacy rerender
path. New writes always use schema 4. A metadata rerender validates object
hashes, image dimensions, region containment, and the PageDocument contract.

Rollback uses the preserved v1 images with a cloned cache directory and the
explicit legacy pipeline settings. It must never mount the live v2 cache for
writes. The production rehearsal verified v1 reads from an isolated clone and
then restored v2 without changing the live cache inventory.

## Output behavior

Completed manga and webtoon jobs now expose PageDocument metadata with stable
geometry-based region IDs, reading order, stage provenance, translation data,
cleanup masks, render geometry, and fit results. Target-language translations
use a language-scoped cache namespace. A recovered cleanup result remains
available for the job lifetime but is marked degraded and is not reused as a
durable cache hit.
