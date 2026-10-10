# Pipeline stage manifest

`server/pipeline/stage_manifest.json` is the source of truth for pipeline
identity. It pins every stage's implementation, model weights, prompt, and
output-affecting settings. It also records SHA-256 hashes for every checked-in
Python runtime source in `pipeline/`, `worker/`, `kindle/`, and `webtoon/`, plus
the requirements policy, configuration, and every configured output font used
by those stages, including fonts in nested directories.

Both runtimes canonicalize the manifest as sorted, compact JSON and identify
it as `sha256:<digest>`. The Go API embeds the manifest and its named artifacts
and verifies their hashes before advertising the revision. The Python worker
verifies the files in its runtime before accepting work. A source or asset
change without the corresponding manifest update stops startup instead of
silently reusing cache entries under a stale identity.

`PIPELINE_REVISION` is an optional deployment assertion. When it is absent,
the API and worker use the derived fingerprint. When it is set, startup only
succeeds if it exactly equals the derived fingerprint. This catches images or
hosts built from different manifests. Compatibility with pre-revision cache
entries is deliberately opt-in:

```dotenv
PIPELINE_REVISION=legacy-v1
ALLOW_LEGACY_PIPELINE_REVISION=1
```

## Updating an identity

When output can change:

1. Pin the exact implementation, model weights, prompt, or setting in the
   manifest. Do not use a moving model tag or a dependency range as a weights
   identity.
2. Recompute the SHA-256 of every changed artifact and update both its
   `artifacts` entry and the affected stage implementation revision. A stage
   with `component_artifacts` uses the canonical composite hash of every named
   artifact; changing any component requires a new composite revision.
3. Run the Python revision tests and Go tests. They verify artifact drift,
   cross-language fingerprint parity, and fingerprint changes for
   implementation, model, weights, prompt, and configuration changes.
4. If deployment sets `PIPELINE_REVISION`, update it to the new derived value
   before bringing up the API and workers.

The model executor packages are exact requirements and are recorded in their
stage identities. Other support dependencies remain bounded ranges, so the
manifest identifies the checked-in requirements policy rather than claiming a
complete transitive environment lock. The current Hugging Face revisions are
immutable repository commits passed to `from_pretrained` or
`snapshot_download`. The LaMa identity records the release asset URL, package
version, and the downloaded `big-lama.pt` SHA-256. The Qwen identity is the
Ollama registry manifest digest. Verify it from the registry response before
updating the manifest:

```bash
curl -fsSI \
  -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
  https://registry.ollama.ai/v2/library/qwen3/manifests/14b \
  | grep -i '^ollama-content-digest:'
```

For the pinned manifest this header is
`bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8`.
An Ollama host can be checked through `/api/tags`; its `qwen3:14b` entry must
report the same digest before that service is used for this revision. The
translation adapters perform this `/api/tags` check before their first request
and fail closed if the configured tag is absent or resolves to other bytes.
This check applies to Ollama's native API; a llama-swap OpenAI endpoint does not
provide the required digest contract and is not accepted as verification.

Before constructing LaMa, the worker downloads or locates `big-lama.pt` and
checks its bytes against the manifest SHA-256. It does not load mismatched
weights. When configured inpainting is enabled, model load, mask construction,
and inference failures fail the page rather than silently applying solid
cleanup while recording LaMa provenance.

Translation is also fail closed. Ollama errors and empty dialogue or SFX
responses fail the job. The pipeline does not substitute Google Translate or
source text under Ollama provenance, so saved region provenance always names
the backend and model that actually executed.

The manifest accepts only its restricted normal form: numeric literals cannot
use exponent notation, redundant trailing fractional zeros, or negative zero,
and strings cannot contain U+2028 or U+2029. Go fingerprints it with the shared
canonical JSON serializer; Python validates the same restrictions before its
sorted compact serialization. Cross runtime parity tests pin the resulting
fingerprint.

## Page provenance

The global fingerprint belongs to the job, cache manifest, and
`PageDocument.pipeline_revision`. Each stage's provenance records its own
implementation revision, exact model and weights identity, and a configuration
hash derived from that stage's prompt and output settings. This lets a saved
document show which component produced each result without mislabeling every
stage with the deployment-wide fingerprint.

Detection is composite because its output is jointly determined by the model
adapter, shared semantic postprocessing, page processor, mask/polygon logic,
and tall-strip utilities where applicable. Python validates each checked-in
composite against the current artifact digests before exposing provenance.

The normalized worker currently asks for all six provenance slots for every
pipeline. A pipeline may share an implementation in a slot it does not expose
as a separate legacy operation, such as the manga transform policy used by the
webtoon normalization path. Those mappings are explicit so document creation
never invents a provenance identity.
