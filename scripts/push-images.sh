#!/usr/bin/env bash
set -euo pipefail

# Publish a release tag without ever replacing it with different content.
# Production deployment additionally records and consumes the resolved digests.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY="${REGISTRY:-192.168.0.90:3007/akitaonrails}"
TAG="${1:?Usage: scripts/push-images.sh <release-tag> [cpu|rocm]}"
VARIANT="${2:-rocm}"

if [[ "${TAG}" == *[!A-Za-z0-9._-]* || -z "${TAG}" ]]; then
  echo "Release tag contains unsupported characters: ${TAG}" >&2
  exit 1
fi
if [[ "${TAG}" == "latest" ]]; then
  echo "Refusing the moving tag 'latest'; provide a release version." >&2
  exit 1
fi
if [[ -n "$(git -C "${ROOT}" status --porcelain --untracked-files=normal)" ]]; then
  echo "Refusing release build from a dirty worktree." >&2
  exit 1
fi

SOURCE_REVISION="$(git -C "${ROOT}" rev-parse --verify HEAD)"
REVISION_PYTHON="${REVISION_PYTHON:-${ROOT}/server/.venv/bin/python}"
if [[ ! -x "${REVISION_PYTHON}" ]]; then REVISION_PYTHON=python3; fi
PIPELINE_REVISION="$(PYTHONPATH="${ROOT}/server" "${REVISION_PYTHON}" -c \
  'from pipeline.revision import get_pipeline_revision; print(get_pipeline_revision())')"

resolve_digest() {
  local manifest
  manifest="$(docker manifest inspect --insecure --verbose "$1" 2>/dev/null)" || return 1
  printf '%s' "${manifest}" | python3 -c '
import json, sys
data = json.load(sys.stdin)
entries = data if isinstance(data, list) else [data]
matches = [item["Descriptor"]["digest"] for item in entries
           if item.get("Descriptor", {}).get("platform", {}).get("os") == "linux"
           and item["Descriptor"]["platform"].get("architecture") == "amd64"]
if len(matches) != 1:
    raise SystemExit("registry reference must resolve to exactly one linux/amd64 manifest")
print(matches[0])
'
}

publish() {
  local dockerfile="$1" image_name="$2"
  local image="${REGISTRY}/${image_name}"
  local candidate="${image}:candidate-${SOURCE_REVISION:0:12}-${BASHPID}-$RANDOM"
  local release="${image}:${TAG}"
  local digest existing source_label pipeline_label
  docker build -f "${dockerfile}" \
    --build-arg "SOURCE_REVISION=${SOURCE_REVISION}" \
    --build-arg "PIPELINE_REVISION=${PIPELINE_REVISION}" \
    --build-arg "RELEASE_VERSION=${TAG}" -t "${candidate}" "${ROOT}" >&2
  source_label="$(docker image inspect "${candidate}" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
  pipeline_label="$(docker image inspect "${candidate}" --format '{{ index .Config.Labels "io.frank-yomik.pipeline-revision" }}')"
  [[ "${source_label}" == "${SOURCE_REVISION}" && "${pipeline_label}" == "${PIPELINE_REVISION}" ]] || {
    echo "OCI revision label verification failed for ${candidate}" >&2
    return 1
  }
  docker push "${candidate}" >&2
  digest="$(resolve_digest "${candidate}")"
  [[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "Invalid digest for ${candidate}" >&2; return 1; }
  existing="$(resolve_digest "${release}" || true)"
  if [[ -n "${existing}" && "${existing}" != "${digest}" ]]; then
    echo "Refusing to overwrite ${release}: existing ${existing}, candidate ${digest}" >&2
    return 1
  fi
  if [[ -z "${existing}" ]]; then
    docker tag "${candidate}" "${release}"
    docker push "${release}" >&2
  fi
  [[ "$(resolve_digest "${release}")" == "${digest}" ]] || {
    echo "Release tag verification failed for ${release}" >&2
    return 1
  }
  printf '%s@%s\n' "${image}" "${digest}"
}

case "${VARIANT}" in
  cpu) WORKER_DOCKERFILE=Dockerfile.worker-cpu; WORKER_NAME=frank-yomik-worker ;;
  rocm) WORKER_DOCKERFILE=Dockerfile.worker-rocm; WORKER_NAME=frank-yomik-worker-rocm ;;
  *) echo "Unknown variant: ${VARIANT} (use cpu or rocm)" >&2; exit 1 ;;
esac

cd "${ROOT}"
echo "Published API: $(publish Dockerfile.api frank-yomik-api)"
echo "Published worker: $(publish "${WORKER_DOCKERFILE}" "${WORKER_NAME}")"
