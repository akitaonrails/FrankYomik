#!/bin/bash
set -euo pipefail

SERVER="192.168.0.145"
SSH_PORT=2022
SSH_USER="akitaonrails"
REGISTRY="192.168.0.145:3007/akitaonrails"
COMPOSE_FILE="yomik-docker-compose.yml"
ENV_FILE="~/frank_yomik/.env"
TAG="${1:-latest}"
VARIANT="${2:-cpu}"  # cpu, rocm, or cuda

COMPOSE="docker compose -f ${COMPOSE_FILE} --env-file ${ENV_FILE}"

ssh_cmd() {
  ssh -p "${SSH_PORT}" "${SSH_USER}@${SERVER}" "$@"
}

echo "==> Building and pushing images (tag: ${TAG}, variant: ${VARIANT})"
cd "$(dirname "$0")/.."
scripts/push-images.sh "${TAG}" "${VARIANT}"

echo ""
echo "==> Deploying on ${SERVER}"

# Copy compose file
echo "  Syncing compose file..."
scp -P "${SSH_PORT}" docker-compose.prod.yml \
  "${SSH_USER}@${SERVER}:~/docker/${COMPOSE_FILE}"

# Pull latest images and restart services
echo "  Pulling images..."
ssh_cmd "cd ~/docker && ${COMPOSE} pull api worker"

echo "  Restarting services..."
ssh_cmd "cd ~/docker && ${COMPOSE} up -d --force-recreate api worker"

echo "  Waiting for health check..."
sleep 3
ssh_cmd "cd ~/docker && ${COMPOSE} ps"

echo ""
echo "==> Done. Services deployed with tag: ${TAG} variant: ${VARIANT}"
