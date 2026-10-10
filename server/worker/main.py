"""Worker entry point: python -m worker [--pipeline manga|webtoon|both]."""

import argparse
import logging
import os
import socket

from kindle.config import _load_yaml_config
from .consumer import Consumer
from .preflight import ALL_PIPELINES, PreflightError, preflight_worker

log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Frank Manga worker — Redis stream consumer")
    parser.add_argument(
        "--pipeline", choices=["both"], default="both",
        help="Current workers must handle all pipelines (default: both)")
    parser.add_argument(
        "--redis-url", default=None,
        help="Redis URL (default: from config.yaml or redis://localhost:6379)")
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Load config
    config = _load_yaml_config()
    worker_cfg = config.get("worker", {})
    redis_url = args.redis_url or worker_cfg.get("redis_url", "redis://localhost:6379")
    consumer_group = worker_cfg.get("consumer_group", "workers")
    heartbeat_interval = worker_cfg.get("heartbeat_interval", 30)
    job_timeout = worker_cfg.get("job_timeout", 300)
    result_ttl = worker_cfg.get("result_ttl", 3600)
    heartbeat_ttl = worker_cfg.get("heartbeat_ttl", 60)
    progress_ttl = worker_cfg.get("progress_ttl", 60)
    high_burst_before_low = worker_cfg.get("high_burst_before_low", 3)

    log.info("Starting worker (pipeline=%s, redis=%s)", args.pipeline, redis_url)

    try:
        report = preflight_worker(ALL_PIPELINES)
    except PreflightError as error:
        parser.error(f"worker preflight failed: {error}")

    cache_dir = worker_cfg.get("cache_dir", "./cache")

    # Stream consumer names must be unique within the group; with multiple
    # replicas the container's hostname (set by docker compose) gives us that.
    # CONSUMER_NAME can override for tests.
    consumer_name = (
        os.environ.get("CONSUMER_NAME")
        or socket.gethostname()
        or None
    )

    consumer = Consumer(
        redis_url=redis_url,
        consumer_group=consumer_group,
        consumer_name=consumer_name,
        heartbeat_interval=heartbeat_interval,
        job_timeout=job_timeout,
        cache_dir=cache_dir,
        result_ttl=result_ttl,
        heartbeat_ttl=heartbeat_ttl,
        progress_ttl=progress_ttl,
        high_burst_before_low=high_burst_before_low,
        capabilities=report,
    )
    consumer.connect()
    consumer.run()


if __name__ == "__main__":
    main()
