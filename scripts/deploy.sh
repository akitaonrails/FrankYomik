#!/bin/bash
set -euo pipefail

SERVER="192.168.0.90"
SSH_USER="akitaonrails"
REGISTRY="localhost:3007/akitaonrails"
REMOTE_DIR="/var/opt/docker/frank_yomik"
TAG="${1:-latest}"
VARIANT="${2:-rocm}"  # cpu, rocm, or cuda

ssh_cmd() {
  ssh -F /dev/null "${SSH_USER}@${SERVER}" "$@"
}

echo "==> Building and pushing images (tag: ${TAG}, variant: ${VARIANT})"
cd "$(dirname "$0")/.."
scripts/push-images.sh "${TAG}" "${VARIANT}"

echo ""
echo "==> Deploying on ${SERVER}"

# Copy compose and config files
echo "  Syncing compose file..."
scp -F /dev/null docker-compose.prod.yml \
  "${SSH_USER}@${SERVER}:${REMOTE_DIR}/docker-compose.yml"

echo "  Syncing config..."
scp -F /dev/null config.prod.yaml \
  "${SSH_USER}@${SERVER}:${REMOTE_DIR}/config.prod.yaml"

# Pull latest images and restart services
echo "  Pulling images..."
ssh_cmd "cd ${REMOTE_DIR} && docker compose pull api worker"

echo "  Restarting services..."
ssh_cmd "cd ${REMOTE_DIR} && docker compose up -d --force-recreate api worker"

echo "  Waiting for health check..."
sleep 3
ssh_cmd "cd ${REMOTE_DIR} && docker compose ps"

echo ""
echo "==> Done. Services deployed with tag: ${TAG} variant: ${VARIANT}"
