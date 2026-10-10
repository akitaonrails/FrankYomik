# Production deployment

Frank Yomik production runs on the Strix Halo homelab host with Docker Compose,
ROCm workers, Redis, llama-swap/Ollama, and Cloudflare Tunnel. The public API
contract remains `/api/v1`; deployment and worker capability details are
internal.

## Images and revision identity

Worker builds are reproducible from these inputs:

- `Dockerfile.api` pins both its Go builder and distroless runtime by digest.
- `Dockerfile.worker-rocm` uses the pinned ROCm/PyTorch base image digest.
- `Dockerfile.worker-cpu` uses pinned Python base image digests and exact CPU
  PyTorch packages.
- `server/requirements.txt` pins direct packages and
  `server/requirements.worker.lock` freezes transitive resolution.
- `server/pipeline/stage_manifest.json` pins implementation and model identity.

Production Compose requires `API_IMAGE` and `WORKER_IMAGE` as exact
`repository@sha256:...` references. A release tag is a human-readable index;
the deploy script refuses to replace it with different content and deploys
only the registry digests it resolved after pushing. Both images carry the Git
source revision and derived pipeline revision as OCI labels. The API and worker
independently derive the same `PIPELINE_REVISION`. Supplying
`PIPELINE_REVISION` is optional and acts as an assertion: a mismatch stops
startup.

## Strix Halo ROCm deployment

The checked-in production topology is `docker-compose.prod.yml`. It expects the
persistent directory layout already used on `192.168.0.90`:

```text
/var/opt/docker/frank/yomik/
├── .cloudflared/
└── data/
    ├── cache/
    ├── redis/
    ├── mangaocr-cache/
    ├── huggingface-cache/
    └── easyocr-cache/
```

The worker uses `/dev/kfd` and `/dev/dri`, `gfx1151`, and two replicas. Translation
uses the host llama-swap/Ollama endpoint configured by `OLLAMA_BASE_URL`. Before
starting a consumer, each replica verifies and loads the pinned detector, OCR,
furigana dictionary, translation model, fonts, and enabled LaMa backend. A
missing model, digest mismatch, incompatible accelerator, or unavailable
translation model stops that replica before it reads Redis streams.

Build, push, record, and restart from exact registry digests:

```bash
AUTH_TOKEN=... bin/deploy v2.0.0 rocm
```

The command requires a clean worktree. It refuses an existing release tag if
the new image digest differs, verifies both OCI revision labels locally and on
the host, and writes the resulting JSON record under `release-manifests/`
locally and beside the remote Compose project. Because the homelab registry is
HTTP, the command opens a temporary local port for pushes. The host prohibits
native OpenSSH TCP forwarding, so a bounded local proxy relays each registry
connection through an authenticated SSH command to the registry address. It
writes a mode-0600 Docker configuration containing only the forwarded alias and
the existing `192.168.0.90:3007` credential. The proxy, its SSH children, and
credential copy are removed by a trap on success, failure, or interruption; a
port collision fails before building.

Registry lookup uses `docker manifest inspect --insecure` and selects exactly
one Linux/amd64 manifest; ambiguous platform results fail. Only the registry's
exact `manifest unknown` response is treated as an unused release tag.
Authentication, network, server, and malformed response failures stop before
the release tag is pushed. Set `REGISTRY` explicitly only for a separately
configured registry that the local Docker daemon can already reach.

For a manual restart, copy the `deployed` references from that release manifest:

```bash
cd /var/opt/docker/frank/yomik
export API_IMAGE='localhost:3007/akitaonrails/frank-yomik-api@sha256:<digest>'
export WORKER_IMAGE='localhost:3007/akitaonrails/frank-yomik-worker-rocm@sha256:<digest>'
docker compose -f docker-compose.prod.yml pull api worker
docker compose -f docker-compose.prod.yml up -d --no-deps \
  --force-recreate --scale worker=2 api worker
```

Check startup and capability identity:

```bash
curl -s http://192.168.0.90:8080/api/v1/health | jq .
docker compose -f docker-compose.prod.yml logs worker
```

Each active worker entry reports `pipelines`, `pipeline_revision`, verified
`models`, `preflight`, and `ready`. `ready` is true only when the worker passed
preflight, advertises all four shared-queue pipelines, and its revision equals
the API revision. Health returns HTTP 503 while no ready worker exists. Legacy
timestamp-only heartbeats remain visible but are not marked ready.

## Generic CPU deployment

The root `docker-compose.yml` is the supported generic path. It builds
`Dockerfile.worker-cpu`; no GPU device reservation is required. CPU inference is
slower and still needs enough memory for the detector, OCR, and LaMa. The
generic Compose service sets `MANGA_OCR_DEVICE=cpu`,
`TEXT_DETECTION_GPU=false`, and `EASYOCR_GPU=false`. The image retains the
manifest-verified `server/config.yaml`; neither deployment replaces that file
with an untracked host copy.

```bash
export AUTH_TOKEN='replace-me'
docker compose build api worker
docker compose up -d
curl -s http://localhost:8080/api/v1/health | jq .
```

Model caches are mounted under the worker home directory. Startup may download
pinned Hugging Face or EasyOCR artifacts into empty caches. The configured
Ollama model must already exist and match the stage manifest digest; preflight
does not silently accept another tag or digest.

For local ROCm development, layer the checked-in override:

```bash
docker compose -f docker-compose.yml -f docker-compose.rocm.yml build worker
docker compose -f docker-compose.yml -f docker-compose.rocm.yml up
```

## Rollback window

`docker-compose.v1-rollback.yml` preserves the explicit v1 escape hatch without
making it a production default. Never attach a v1 writer to the live v2 cache:
an older cache implementation could replace a schema 4 manifest. Stop metadata
edits briefly, clone the live cache to a separate rollback directory, and record
the source tree hash before rehearsal:

```bash
export V1_CACHE_DIR=/var/opt/docker/frank/yomik/data/cache-v1-rehearsal
mkdir -p "$V1_CACHE_DIR"
rsync -a --delete /var/opt/docker/frank/yomik/data/cache/ "$V1_CACHE_DIR/"
find /var/opt/docker/frank/yomik/data/cache -type f -print0 \
  | sort -z | xargs -0 sha256sum > /tmp/yomik-live-cache.before.sha256
```

Set immutable v1 image references for both API and worker so queue revisions
agree, then layer the rollback override:

```bash
export API_IMAGE='localhost:3007/akitaonrails/frank-yomik-api@sha256:<v2-digest>'
export WORKER_IMAGE='localhost:3007/akitaonrails/frank-yomik-worker-rocm@sha256:<v2-digest>'
export V1_API_IMAGE='localhost:3007/akitaonrails/frank-yomik-api@sha256:b6a9c755aca637ea6cbd45f744058ac93d1acc3bc6138e57366de001769eb25f'
export V1_WORKER_IMAGE='localhost:3007/akitaonrails/frank-yomik-worker-rocm@sha256:0812dd7271818bcf827c927a4e724dab9d6c07f64e08f3d4942fbf0deb06abb1'
export V1_CACHE_DIR=/var/opt/docker/frank/yomik/data/cache-v1-rehearsal
docker compose -f docker-compose.prod.yml \
  -f docker-compose.v1-rollback.yml up -d api worker
```

The override sets `PIPELINE_REVISION=legacy-v1`, enables the compatibility flag,
and replaces both cache mounts with `V1_CACHE_DIR`. After rehearsal, verify the
live tree against `/tmp/yomik-live-cache.before.sha256`; discard the isolated
clone. Return to v2 by removing the override and starting the recorded v2
digest references.

## Troubleshooting

### Worker is absent from health output

Read worker logs. Preflight names the failed capability and underlying error.
Check cache volume permissions, configured device availability, model digests,
and the Ollama endpoint. The worker intentionally has no heartbeat until all
checks pass.

### Worker is active but `ready` is false

Compare the worker and top-level `pipeline_revision`. Restart API and workers
from the digest references in the same release manifest. A timestamp-only
heartbeat belongs to an older worker that does not publish the capability
contract.

### Ollama model verification fails

List models at the configured endpoint and compare the tag and digest with
`server/pipeline/stage_manifest.json`. Pull the pinned model through the
homelab model service, then restart the worker.

### ROCm initialization fails

Confirm `/dev/kfd` and `/dev/dri` are present, the configured group IDs match the
host, and the host supports the pinned ROCm image. Use the generic CPU Compose
service as a complete unit when testing without ROCm.
