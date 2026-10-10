# v2.0.0 security audit

Audit date: 2026-10-10  
Scope: commits after `v1.0.1`, the final v2 tree, production deployment files,
container build inputs, GitHub workflows, model acquisition, HTTP/WebSocket
entry points, Redis queueing, cache persistence, and release operations.

## Result

No unresolved high-severity security or licensing issue blocks v2.0.0. GitHub
had no open issues labeled as security, no private repository advisories, no
Dependabot alerts, and no CodeQL alerts at the audit date. Repository secret
scanning was not enabled, so the release gate used a full-history Gitleaks scan
instead.

Five historical Gitleaks matches were reviewed as deterministic fixture or
artifact hashes rather than credentials. Their exact fingerprints are recorded
in `.gitleaksignore`; a repeat full-history scan passes. No broad pattern or
path exclusion was added.

## Threat review and changes

| Area | Release state |
|---|---|
| Authentication | Bearer comparison uses constant-time equality. Ordinary URL query authentication is rejected. |
| WebSocket | Authentication uses the protocol handshake; frame size, subscription count, and writer lifetime are bounded. Origin checks remain enforced. |
| Uploads | Compressed request limits are complemented by image-header, dimension, and 40-million-pixel decode limits. Malformed and unsupported images fail before full allocation. |
| Queue | Submission reservation is atomic. Concurrent identical requests share one job, and failed reservations are removed with compare-and-delete semantics. Worker recovery uses bounded Redis Stream claiming. |
| Cache | Object references are content-addressed, hash checked, dimension checked, path restricted, and region contained. Degraded results do not enter durable cache. Metadata writes validate the shared schema. |
| Model execution | Models and prompts are pinned in the stage manifest. Worker preflight verifies required artifacts and the Ollama digest before consuming jobs. Cleanup inference runs in a supervised child with a wall deadline and deterministic bounded fallback. |
| Containers | API is nonroot and workers drop to the configured UID while retaining only required supplemental GPU groups. Production app images use immutable registry digests. Redis, BusyBox, Cloudflared, and generic Ollama references are pinned to tested OCI digests. The final scan also removed a stray ignored development binary from the worker build context, upgraded fixed Ubuntu packages in the ROCm base, and moved Pillow and Transformers to their fixed releases. |
| CI and release | Third-party actions use full commit SHAs. AppImageTool is checksum verified. Python locks and Docker base image digests are pinned. The API is compiled with Go 1.26.9, which clears the three high-severity standard-library findings caught in the RC image scan. A tag publishes only after the exact commit passes hosted CI. |
| Secrets | Tokens remain outside the repository and release evidence. Private source fixtures and job payloads stay on the homelab with mode 0600 where applicable. |

## Residual risks

- Cloudflare tunnel credentials and the production bearer token remain
  operator-managed secrets. Compromise of the host or its deployment directory
  can expose them.
- The API is designed for a trusted single-tenant installation. It does not
  implement per-user authorization or tenant isolation.
- Model formats and ML runtimes are a large supply-chain surface. Pins prevent
  unreviewed drift; updating them still requires a fresh audit and image scan.
- Native GitHub secret scanning should be enabled when the repository plan and
  settings allow it. Gitleaks remains the enforced compensating check.

## Verification

The security fixes are covered by Go and Python tests for authentication,
WebSocket limits, image bounds, queue deduplication, cache/object validation,
worker preflight, pipeline revision matching, degraded persistence, and
recovery. The release gate also validates both Compose configurations, scans
Git history with Gitleaks, builds the CPU worker from scratch, deploys the ROCm
pair by digest, and exercises concurrent submission, worker failover, and the
isolated rollback path.

Trivy 0.67.2 found three high-severity Go standard-library issues and 22 high
findings across the initial ROCm OS and Python layers. After the fixed Go,
Ubuntu, Pillow, and Transformers updates and removal of the stray development
binary, rescans report zero high or critical findings for the API, CPU worker,
and ROCm worker images. The complete Python suite also passes inside the rebuilt
ROCm image with the updated runtime.
