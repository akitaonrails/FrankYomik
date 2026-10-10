# Phase 6 Strix Halo operations and rehearsal evidence

Inventory date: 2026-10-10

Host: `akitaonrails@192.168.0.90` (`marvin-server`)

This document records the pre-release read-only inventory and the rehearsal
procedure designed from it. The procedure has now completed; normalized results
are in [Phase 6 Strix Halo release rehearsal](phase6_strix_rehearsal.md). The
inventory itself did not pull images, build images, alter tags, restart
containers, write Redis, or change the cache.

## Promotion decision

The host has enough CPU, unified GPU memory, storage, model storage, and two
working ROCm worker slots. The matching v1 rollback pair is preserved in the
local registry as `v1.0.1-rollback-20261010`. Backups, exact candidate registry
digests, four-pipeline execution, recovery, deduplication, and isolated rollback
have passed. The repository deploy script records digests, verifies OCI source
and pipeline revision labels, and recreates only API and worker services.

## Read-only production inventory

### Deployment and health

The Compose project lives at `/var/opt/docker/frank/yomik` and is named
`frank_yomik`. Docker is 29.7.2 and Compose is 5.1.1. The current services are
Redis, API, two worker replicas, cloudflared, and their two one-shot init
services.

The public health endpoint returned:

```json
{"status":"healthy","redis":"connected","queue_high":501,"queue_low":51,"active_workers":2}
```

The two worker heartbeats were current. `queue_high` and `queue_low` are Redis
stream lengths, not unfinished work: both consumer groups had `pending=0` and
`lag=0`. The streams retain 501 and 51 acknowledged entries respectively.
There were 13 historical consumer names in each group. This distinction must
be retained in the Phase 6 health documentation and monitoring.

All five long-running project containers were up after the host's latest
restart. The current API had restarted once; neither worker reported an OOM.
Recent worker logs contained only Hugging Face authentication and EasyOCR CPU
fallback warnings. They contained no traceback, OOM, or XAUTOCLAIM error.

The current deployment predates pipeline revisioning. Neither worker image has
`pipeline/revision.py`, the API startup log does not print a pipeline revision,
and none of the containers has `PIPELINE_REVISION` configured. This makes the
existing deployment a useful v1 rollback baseline, but it cannot prove the v2
revision health contract.

### Exact running images and rollback state

| Component | Running image ID / registry digest | Created | State |
| --- | --- | --- | --- |
| API | `sha256:e329d82103b7d34beaa1641c74e08348673308fde3e445704705a34439b9ba6c` | 2026-09-05 | registry tag `v1.0.1-rollback-20261010` |
| ROCm worker | `sha256:effcffc418cb5ad06924ff802f58704f3384f62016d9a9d8d5ac1b2cda1f9a19` | 2026-09-05 | registry tag `v1.0.1-rollback-20261010` |
| older worker | `sha256:a5108dc0b14b140b6828acc4171fc0c0abdc5f4d3f744b2382b6c8a73df6611a` | 2026-05-30 | tagged `rollback-20260815`; no matching API tag was found |

The local registry at `192.168.0.90:3007` requires authentication. The Docker
daemon can resolve the running `latest` images, but unauthenticated Registry
HTTP tag enumeration returns 401. On 2026-10-10 the running API and both
identical worker replicas were pushed as the matching rollback tag above. No
service, cache, queue, or running container was changed while preserving them.
The deployable Linux/amd64 registry manifest digests are
`sha256:b6a9c755aca637ea6cbd45f744058ac93d1acc3bc6138e57366de001769eb25f`
for the API and
`sha256:0812dd7271818bcf827c927a4e724dab9d6c07f64e08f3d4942fbf0deb06abb1`
for the worker. The `e329...` and `effc...` values in the table are local image
config IDs and cannot be used after `@` in a registry reference.

At inventory time, the deployed checkout still used mutable `latest` images
and whole-project recreation. The current repository has since replaced that
path: `bin/deploy` refuses a conflicting release tag, resolves registry
digests, verifies OCI labels, records a release manifest, and recreates only
the API and two workers. This paragraph describes the historical host state;
the checked-in deployment procedure is the authoritative candidate procedure.

### Configuration and persistent data

The production file identities were:

| File | SHA-256 |
| --- | --- |
| `/var/opt/docker/frank/yomik/docker-compose.yml` | `51dc545b3061687e24dce68f2d283500eb01fcf9df5f489074ca603674b67d02` |
| `/var/opt/docker/frank/yomik/config.prod.yaml` | `a7d8d830ae966a244f571c1ae554505c9b1fe421efb4ceb97a266d9f76564407` |

The `.env` file is mode 0600 and contains only the names `APP_GID`, `APP_UID`,
`AUTH_TOKEN`, and `OLLAMA_BASE_URL`; values were not captured. Cloudflare
credentials exist under `.cloudflared/` and were not read. Workers mount the
configuration read-only and share writable cache, Manga OCR, Hugging Face, and
EasyOCR directories. Each worker has an 8 GiB memory limit and 12 GiB
memory-plus-swap limit. Redis and the API have no container memory cap.

The cache occupies 2.0 GiB. It contains 3,253 content-addressed object files
and 1,323 page manifests. Every page manifest is schema 2 and has no pipeline
revision, so the current tree treats it as `legacy-v1`. The aggregate namespaces
are 825 `manga_furigana`, 32 `book_furigana`, 376 `webtoon`, 56
`webtoon_pt-br`, 30 `manga_translate_pt-br`, and 4
`manga_furigana_pt-br`. Five schema-1 JSON files are debug records. No legacy
image-only PNG was found outside `cache/v2`.

The v2 cache uses one authoritative manifest path per pipeline and source hash.
A candidate revision treats the schema-2 manifest as a miss and may replace
that manifest with schema 4 while retaining immutable objects. Rolling back
therefore preserves object bytes but may require v1 to reprocess a page whose
manifest was promoted. This expected miss must not be reported as object
corruption.

Redis uses `noeviction`, has no configured maximum memory, and held about
969 MiB at inspection. Its bind mount is
`/var/opt/docker/frank/yomik/data/redis`. Cache data is at
`/var/opt/docker/frank/yomik/data/cache`.

### Models and host capacity

| Resource | Observed |
| --- | --- |
| CPU | Ryzen AI Max+ 395, 16 cores / 32 threads |
| Memory | 61 GiB total, 46 GiB available |
| Swap | 62 GiB total, effectively unused |
| GPU | Radeon 8060S, `gfx1151`, 64 GiB unified VRAM |
| GPU at inspection | 98% busy, 23.25 GiB reported used |
| `/var` | 2.5 TiB free |
| root filesystem | 11 GiB free |

The high GPU utilization came from other host activity and means latency data
must include a quiet-host precondition. The two current workers consumed about
1.06 GiB and 1.00 GiB of container memory while idle.

The production model caches contain the pinned ogkalu detector snapshot,
`kha-white/manga-ocr-base`, and EasyOCR Japanese/Korean/CRAFT weights. Their
sizes are approximately 1,012 MiB for Hugging Face and 112 MiB for EasyOCR.
The Manga OCR cache path is empty because its data is in the shared Hugging
Face cache. The isolated Koharu evaluation cache remains at
`/var/opt/docker/frank/yomik-eval/cache` and occupies 17 GiB; it is not a
production cache.

Ollama is reachable on the LAN address at port 11434. Installed models include
the required `qwen3:14b`, whose digest exactly matches the stage manifest:
`bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8`.
`qwen3:32b` and `nomic-embed-text` are also installed. Only
`nomic-embed-text` was resident. llama-swap is reachable at port 11435 and
advertises its configured catalog; no llama-swap model was resident through
Ollama. The production translation route should continue to use the pinned
Ollama model.

## Preconditions for the write rehearsal

Do not proceed until all of these are true:

1. Phase 6 code and the stage manifest are frozen at a reviewed Git SHA.
2. API and worker images pass their local build and test gates and carry that
   Git SHA and pipeline revision as OCI labels.
3. Candidate API and worker release tags resolve to the exact registry digests
   in the generated release manifest. Compose uses the digest references.
4. Pull the preserved `v1.0.1-rollback-20261010` API and worker pair and verify
   the two digests above before the candidate is started.
5. The production Compose file, production config, `.env`, Redis RDB, and a
   cache file inventory are backed up under a timestamped directory on `/var`.
6. Redis stream lag and pending counts are zero. Reader clients are placed in a
   short maintenance window so the recovery tests cannot consume real jobs.
7. Ollama reports the exact `qwen3:14b` digest. At least 32 GiB of system memory
   is available, the GPU is quiet enough for measurement, and `/var` has at
   least 30 GiB free.
8. The four rehearsal inputs are fixed and locally available: one manga
   translation page, one manga furigana page, one prose furigana page, and one
   representative tall Korean webtoon. Record each source SHA-256. The current
   repository corpus supplies manga pages but no committed full-page prose or
   webtoon fixture, so select those two from the copied production cache or a
   private release corpus before starting.

### Preserve rollback artifacts

The rollback images are already preserved. The maintenance window still needs
the configuration, Redis, and cache backups below.

```bash
cd /var/opt/docker/frank/yomik
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/var/opt/docker/frank/yomik-backups/${stamp}"
mkdir -p "$backup"
cp -a docker-compose.yml config.prod.yaml .env "$backup/"

docker pull 192.168.0.90:3007/akitaonrails/frank-yomik-api:v1.0.1-rollback-20261010
docker pull 192.168.0.90:3007/akitaonrails/frank-yomik-worker-rocm:v1.0.1-rollback-20261010
test "$(docker image inspect \
  192.168.0.90:3007/akitaonrails/frank-yomik-api:v1.0.1-rollback-20261010 \
  --format '{{.Id}}')" = \
  'sha256:e329d82103b7d34beaa1641c74e08348673308fde3e445704705a34439b9ba6c'
test "$(docker image inspect \
  192.168.0.90:3007/akitaonrails/frank-yomik-worker-rocm:v1.0.1-rollback-20261010 \
  --format '{{.Id}}')" = \
  'sha256:effcffc418cb5ad06924ff802f58704f3384f62016d9a9d8d5ac1b2cda1f9a19'

before_save="$(docker exec yomik-redis redis-cli --raw LASTSAVE)"
docker exec yomik-redis redis-cli BGSAVE
while :; do
  after_save="$(docker exec yomik-redis redis-cli --raw LASTSAVE)"
  [ "$after_save" -gt "$before_save" ] && break
  sleep 1
done
cp -a data/redis/dump.rdb "$backup/redis.rdb"
find data/cache -type f -print0 | sort -z | xargs -0 sha256sum \
  >"$backup/cache-before.sha256"
```

Verify the preserved rollback images with the authenticated registry client and
record both digests. A file copy taken while Redis is writing is not a valid
Redis backup; wait for `BGSAVE` to finish before copying `dump.rdb`.

## Rehearsal topology

First run an isolated Compose project at
`/var/opt/docker/frank/yomik-phase6` with a separate Redis volume, a copy or
reflink clone of the production cache, a separate API port, and the same two
worker replica shape. Share model caches only; do not mount the production
result cache or Redis data read-write. This makes restart, XAUTOCLAIM, schema
promotion, and rollback tests safe.

After the isolated gates pass, use the same immutable image references for the
short production-shaped canary. Recreate API and worker services without
bringing Redis down. Require the candidate health response to expose the exact
pipeline revision and both workers' capability/model preflight status before
submitting a job.

## Ordered rehearsal

### 1. Clean startup and preflight

Start the isolated Redis, then API, then one worker. Capture image digests,
container image IDs, configuration hashes, startup logs, pipeline revision,
model revisions, and health JSON. Startup must fail before claiming jobs when
a required weight is missing, has the wrong checksum, or is incompatible with
`gfx1151`. Restore the valid cache and require healthy status before continuing.

Scale to two workers and confirm distinct consumer names and matching pipeline
revisions. Record idle memory and GPU memory before loading each stage.

### 2. Four complete pipelines

Submit the frozen inputs through the HTTP API with cache bypass enabled and
wait for terminal results in this order:

1. `manga_furigana`, avoiding translation model load;
2. `book_furigana`, reusing Manga OCR;
3. `manga_translate`, loading the pinned `qwen3:14b`;
4. `webtoon`, exercising tall-image splitting and composition.

For every run record source hash, job ID, worker name, pipeline revision,
output and metadata URLs, output dimensions/mode, output SHA-256, metadata
content/render hashes, stage provenance, cold/warm wall time, peak container
memory, peak GPU memory, fallback reasons/counts, and whether the result was
cacheable. Run every input a second time without bypass and require a cache hit
with identical output bytes and no stage/model calls.

### 3. Two replicas and deduplication

Submit two different cold inputs close together and prove that both worker
consumer names complete work. Submit the same source, pipeline, target language,
and revision twice concurrently. Require one stream entry and one result; both
requests must resolve to the same job or durable cached result. Change only the
pipeline revision in the isolated stack and require a distinct dedup identity
and a cache miss.

### 4. Worker restart and XAUTOCLAIM

Use an isolated long-running fixture. Wait until Redis reports one pending
entry for the first worker, stop that worker, and retain Redis and the API.
Start the replacement worker, wait more than the configured 60-second minimum
idle time, and require its log to record one claim. The job must complete once,
the consumer group must return to zero pending entries, and the output/cache
hash must match a clean run. Repeat once with the other priority stream if both
are used by production clients.

### 5. Legacy read and v2 write

Use the cloned production cache. Its schema-2 manifests are the frozen v1
evidence. Run the new binaries once with `PIPELINE_REVISION=legacy-v1` and
`ALLOW_LEGACY_PIPELINE_REVISION=1`; require an existing schema-2 page to be
served without worker stage calls. This explicitly tests the migration reader.

Then run the candidate with its derived revision. The same source must be a
revision cache miss. After successful processing, require a schema-4 manifest,
PageDocument schema 2 metadata, the exact candidate revision, verified object
hashes, and a second-request cache hit. Check that preexisting content-addressed
object files still match `cache-before.sha256`. A same-source manifest is
expected to change because the on-disk path is not revision-qualified.

### 6. Rollback

Stop only the isolated candidate API/workers and start the preserved v1 pair
against the same cloned Redis/cache. Do not roll Redis back for the normal
test. Require health and both worker heartbeats, then verify:

- an untouched schema-2 page remains a v1 cache hit;
- a source whose manifest was promoted to schema 4 is a safe miss or a valid
  read according to v1 behavior, never a malformed response;
- reprocessing produces a decodable same-dimension PNG;
- every preexisting content-addressed object's SHA-256 is unchanged;
- the v2 candidate objects remain hash-valid and can be read again after
  returning to the candidate images;
- Redis has no pending entry from an incompatible revision and no mixed-revision
  result is served.

Only after this isolated rollback passes should the production-shaped canary
be attempted. Retain the Redis backup for disaster recovery; restoring it is
not part of a normal image rollback.

## Acceptance checks

| Gate | Required result |
| --- | --- |
| Provenance | API and both workers run the reviewed Git SHA, immutable image digests, and exact derived pipeline revision. |
| Startup | Required model checks pass before a worker claims jobs; deliberate missing/bad weight fails closed. |
| Four pipelines | All four complete, preserve source dimensions and PNG contract, and produce recorded output/metadata hashes. |
| Cache | Warm request is byte-identical and invokes no stage; legacy read is explicit; new write is schema 4 with PageDocument schema 2. |
| Dedup | Same source/pipeline/language/revision yields one execution; changed revision does not deduplicate. |
| Recovery | An abandoned pending entry is claimed after 60 seconds and completes once. |
| Replicas | Two distinct consumers process jobs without OOM or revision mismatch. |
| Resources | Neither 8 GiB worker is OOM-killed; host retains at least 8 GiB available memory; no sustained swap growth. |
| Translation | Ollama model digest remains `bdbd181c...debe8`; provider/model mismatch fails before cached output is written. |
| Fallback | Every fallback is counted and named; transient cleanup/OCR degradation is marked non-cacheable. |
| Rollback | Preserved v1 images start, old content objects retain their hashes, v1 pages remain usable or reprocess safely, and candidate objects remain valid. |

There is no evidence yet for a production latency threshold. Record cold and
warm values for each stage and compare them with the Phase 0 baseline rather
than inventing a limit. Any OOM, corrupt object, mixed-revision result, missing
pipeline, or unexplained output-hash drift blocks promotion.

## Evidence record template

Copy this table into the final run record and fill every cell from commands or
captured artifacts.

| Field | Evidence |
| --- | --- |
| UTC start/end | |
| Host/kernel/Docker/Compose | |
| Reviewed Git SHA | |
| Derived pipeline revision | |
| API image tag, registry digest, local ID | |
| Worker image tag, registry digest, local ID | |
| v1 API rollback tag/digest/ID | |
| v1 worker rollback tag/digest/ID | |
| Compose/config SHA-256 | |
| Redis backup path and `LASTSAVE` | |
| Cache-before inventory SHA-256 | |
| Ollama `qwen3:14b` digest | |
| Detector/Manga OCR/EasyOCR/LaMa preflight | |
| Initial health/capabilities JSON | |
| Initial stream lag/pending | |
| Initial host/container/GPU memory | |
| Four source SHA-256 values | |
| Per-pipeline cold/warm stage timings | |
| Per-pipeline output/metadata/content/render hashes | |
| Per-pipeline dimensions/mode | |
| Per-pipeline fallback/cacheable result | |
| Two-replica consumer evidence | |
| Dedup and revision-separation evidence | |
| XAUTOCLAIM old/new consumer, message, pending count | |
| Legacy schema-2 cache-hit evidence | |
| Candidate schema-4 write and warm-hit evidence | |
| Cache-after inventory and preexisting-file diff | |
| Rollback health and output evidence | |
| Candidate-after-rollback object verification | |
| Final stream lag/pending | |
| Final host/container/GPU memory and OOM state | |
| Decision and blockers | |

Store JSON, logs, timing observations, and hash inventories outside the tracked
result file when they contain timestamps, private source identifiers, or host
paths. The tracked summary should contain normalized values and hashes of those
private evidence files so a rerun can be compared without committing reader
content or secrets.

## Read-only commands used for this inventory

```bash
ssh -F /dev/null -o BatchMode=yes akitaonrails@192.168.0.90
cd /var/opt/docker/frank/yomik
docker compose ps --format json
curl -fsS http://192.168.0.90:8080/api/v1/health
docker inspect --format \
  '{{.Name}} image={{.Image}} started={{.State.StartedAt}} restart_count={{.RestartCount}} oom={{.State.OOMKilled}}' \
  yomik-api frank_yomik-worker-1 frank_yomik-worker-2
docker image ls --no-trunc --digests
docker stats --no-stream
docker exec yomik-redis redis-cli XINFO GROUPS frank:jobs:high
docker exec yomik-redis redis-cli XINFO GROUPS frank:jobs:low
docker exec yomik-redis redis-cli XPENDING frank:jobs:high workers
docker exec yomik-redis redis-cli XPENDING frank:jobs:low workers
curl -fsS http://192.168.0.90:11434/api/ps
curl -fsS http://192.168.0.90:11434/api/tags
curl -fsS http://192.168.0.90:11435/v1/models
free -h
df -h / /var
rocm-smi --showproductname --showmeminfo vram --showuse
```

Environment values, Cloudflare files, source titles, source hashes, cached
images, Redis payloads, and authentication material were deliberately excluded
from the inventory.
