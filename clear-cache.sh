#!/usr/bin/env bash
# Clear all Frank server caches (Redis + v2 filesystem).
# Run from the project root where docker-compose.yml lives.
#
# Preserves Redis streams (frank:jobs:*) and consumer groups so the
# worker doesn't crash. Only flushes data/result/dedup keys.
set -euo pipefail

echo "==> Flushing Redis cache keys (preserving streams)..."
for pattern in "frank:results:*" "frank:images:*" "frank:meta:*" "frank:notify:*" "frank:progress:*"; do
  docker compose exec -T redis redis-cli --no-auth-warning --scan --pattern "$pattern" \
    | xargs -r docker compose exec -T redis redis-cli --no-auth-warning DEL
done
# Dedup is a single hash key
docker compose exec -T redis redis-cli --no-auth-warning DEL frank:dedup
echo "    Done."

echo "==> Clearing v2 filesystem cache..."
docker compose exec api rm -rf /data/cache/v2
docker compose exec worker rm -rf /app/cache/v2
echo "    Done."

echo ""
echo "All server caches cleared. No restart needed."
