"""Worker health check utilities."""

import json
import logging
import time

import redis

from .consumer import HEARTBEAT_PREFIX, STREAM_HIGH, STREAM_LOW

log = logging.getLogger(__name__)

ACTIVE_WORKER_WINDOW_SECONDS = 90


def check_health(redis_url: str, consumer_group: str = "workers") -> dict:
    """Check worker health: Redis connectivity, queue lengths, active workers."""
    try:
        rdb = redis.from_url(redis_url, decode_responses=True)
        rdb.ping()
    except (redis.ConnectionError, redis.TimeoutError) as e:
        return {"status": "unhealthy", "error": f"Redis unreachable: {e}"}

    # Queue lengths
    try:
        high_len = rdb.xlen(STREAM_HIGH)
    except redis.ResponseError:
        high_len = 0
    try:
        low_len = rdb.xlen(STREAM_LOW)
    except redis.ResponseError:
        low_len = 0

    # Active workers (heartbeat keys)
    now = int(time.time())
    worker_keys = rdb.keys(f"{HEARTBEAT_PREFIX}*:heartbeat")
    active_workers = []
    for key in worker_keys:
        ts = rdb.get(key)
        if ts and now - int(ts) < ACTIVE_WORKER_WINDOW_SECONDS:
            # Extract worker name from key pattern
            name = key.replace(HEARTBEAT_PREFIX, "").replace(":heartbeat", "")
            worker = {"name": name, "last_heartbeat": int(ts)}
            capability_value = rdb.get(
                f"{HEARTBEAT_PREFIX}{name}:capabilities"
            )
            if capability_value:
                try:
                    capabilities = json.loads(capability_value)
                except (TypeError, json.JSONDecodeError):
                    capabilities = None
                if (isinstance(capabilities, dict)
                        and capabilities.get("schema_version") == 1):
                    worker.update(capabilities)
            active_workers.append(worker)

    # Pending messages per consumer group
    pending = {}
    for stream_name, stream_key in [("high", STREAM_HIGH), ("low", STREAM_LOW)]:
        try:
            info = rdb.xpending(stream_key, consumer_group)
            pending[stream_name] = info.get("pending", 0) if info else 0
        except redis.ResponseError:
            pending[stream_name] = 0

    return {
        "status": "healthy",
        "queue_high": high_len,
        "queue_low": low_len,
        "pending": pending,
        "active_workers": active_workers,
    }
