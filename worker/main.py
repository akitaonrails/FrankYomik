"""Worker entry point: python -m worker [--pipeline manga|webtoon|both]."""

import argparse
import logging
import sys

from pipeline.config import _load_yaml_config
from .consumer import Consumer

log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Frank Manga worker — Redis stream consumer")
    parser.add_argument(
        "--pipeline", choices=["manga", "webtoon", "both"],
        default="both",
        help="Which pipeline(s) this worker handles (default: both)")
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

    log.info("Starting worker (pipeline=%s, redis=%s)", args.pipeline, redis_url)

    # Pre-load heavy models so first job doesn't pay startup cost
    _preload_models(args.pipeline)

    consumer = Consumer(
        redis_url=redis_url,
        consumer_group=consumer_group,
        heartbeat_interval=heartbeat_interval,
        job_timeout=job_timeout,
    )
    consumer.connect()
    consumer.run()


def _preload_models(pipeline: str) -> None:
    """Preload OCR and other heavy models to avoid first-job latency."""
    if pipeline in ("manga", "both"):
        log.info("Preloading manga-ocr model...")
        try:
            from pipeline.ocr import _get_ocr  # noqa: F401
            _get_ocr()
            log.info("manga-ocr model loaded")
        except Exception:
            log.warning("Failed to preload manga-ocr (will load on first job)")

    if pipeline in ("webtoon", "both"):
        log.info("Preloading EasyOCR model...")
        try:
            from webtoon.ocr import _get_reader  # noqa: F401
            _get_reader()
            log.info("EasyOCR model loaded")
        except Exception:
            log.warning("Failed to preload EasyOCR (will load on first job)")


if __name__ == "__main__":
    main()
