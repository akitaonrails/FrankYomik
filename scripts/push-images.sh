#!/bin/bash
set -euo pipefail

# Push to Gitea registry at 192.168.0.90:3007 from dev machine.
# On the server, Docker pulls from localhost:3007 (same host).
REGISTRY="${REGISTRY:-192.168.0.90:3007/akitaonrails}"
TAG="${1:-latest}"
VARIANT="${2:-rocm}"  # cpu, rocm, or cuda

echo "Building and pushing to ${REGISTRY} with tag: ${TAG} variant: ${VARIANT}"

# API (small, static Go binary)
docker build -f Dockerfile.api -t "${REGISTRY}/frank-yomik-api:${TAG}" .
docker push "${REGISTRY}/frank-yomik-api:${TAG}"

# Worker
case "${VARIANT}" in
  cpu)
    DOCKERFILE="Dockerfile.worker-cpu"
    IMAGE_SUFFIX=""
    ;;
  rocm)
    DOCKERFILE="Dockerfile.worker-rocm"
    IMAGE_SUFFIX="-rocm"
    ;;
  cuda)
    DOCKERFILE="Dockerfile.worker"
    IMAGE_SUFFIX="-cuda"
    ;;
  *)
    echo "Unknown variant: ${VARIANT} (use cpu, rocm, or cuda)"
    exit 1
    ;;
esac

docker build -f "${DOCKERFILE}" -t "${REGISTRY}/frank-yomik-worker${IMAGE_SUFFIX}:${TAG}" .
docker push "${REGISTRY}/frank-yomik-worker${IMAGE_SUFFIX}:${TAG}"

echo "Done. Images pushed:"
echo "  ${REGISTRY}/frank-yomik-api:${TAG}"
echo "  ${REGISTRY}/frank-yomik-worker${IMAGE_SUFFIX}:${TAG}"
