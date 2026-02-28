"""Redis stream consumer for processing jobs with priority queues."""

import json
import logging
import os
import re
import signal
import time
import hashlib

import redis

from .job import ProcessingJob, ProcessingResult, process_job
from .page_cache import PageCache

log = logging.getLogger(__name__)

# Stream and key names
STREAM_HIGH = "frank:jobs:high"
STREAM_LOW = "frank:jobs:low"
IMAGE_KEY_PREFIX = "frank:images:"
RESULT_KEY_PREFIX = "frank:results:"
RESULT_IMG_PREFIX = "frank:results:img:"
NOTIFY_PREFIX = "frank:notify:"
HEARTBEAT_PREFIX = "frank:worker:"
PROGRESS_PREFIX = "frank:progress:"

# TTLs
RESULT_TTL = 3600  # 1 hour
HEARTBEAT_TTL = 60  # seconds
PROGRESS_TTL = 60  # seconds


_SLUG_RE = re.compile(r"[^a-z0-9\-]")


def _slugify(s: str) -> str:
    """Convert a string to a lowercase, hyphen-separated slug."""
    s = s.lower().strip().replace(" ", "-")
    s = _SLUG_RE.sub("", s)
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")


class Consumer:
    """Redis stream consumer with two-stream priority."""

    # Process at most this many high-priority jobs consecutively before giving
    # low-priority prefetch jobs a chance. Keeps "current page first" behavior
    # while preventing low queue starvation during continuous reading.
    HIGH_BURST_BEFORE_LOW = 3

    def __init__(self, redis_url: str, consumer_group: str = "workers",
                 consumer_name: str | None = None,
                 heartbeat_interval: int = 30,
                 job_timeout: int = 300,
                 cache_dir: str = "./cache"):
        self.redis_url = redis_url
        self.consumer_group = consumer_group
        self.consumer_name = consumer_name or f"worker-{os.getpid()}"
        self.heartbeat_interval = heartbeat_interval
        self.job_timeout = job_timeout
        self.cache_dir = cache_dir
        self._page_cache = PageCache(cache_dir)
        self._running = False
        self._rdb: redis.Redis | None = None
        self._last_heartbeat = 0.0
        self._high_streak = 0

    def connect(self) -> None:
        """Connect to Redis and ensure consumer groups exist."""
        self._rdb = redis.from_url(self.redis_url, decode_responses=False)
        self._rdb.ping()
        log.info("Connected to Redis: %s", self.redis_url)

        # Create consumer groups (MKSTREAM creates the stream if needed)
        for stream in (STREAM_HIGH, STREAM_LOW):
            try:
                self._rdb.xgroup_create(stream, self.consumer_group,
                                        id="0", mkstream=True)
                log.info("Created consumer group '%s' on %s",
                         self.consumer_group, stream)
            except redis.ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise
                # Group already exists — fine

    def run(self) -> None:
        """Main consumer loop. Blocks until shutdown signal."""
        self._running = True
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        log.info("Worker %s starting consumer loop", self.consumer_name)
        self._heartbeat()

        while self._running:
            try:
                self._tick()
            except redis.ConnectionError:
                log.warning("Redis connection lost, reconnecting in 5s...")
                time.sleep(5)
                try:
                    self.connect()
                except Exception:
                    log.exception("Reconnection failed")
            except Exception:
                log.exception("Unexpected error in consumer loop")
                time.sleep(1)

        log.info("Worker %s shutting down", self.consumer_name)

    def _tick(self) -> None:
        """One iteration with weighted high/low scheduling + heartbeat."""
        msg = None

        # After a burst of high jobs, probe low first to avoid starvation.
        if self._high_streak >= self.HIGH_BURST_BEFORE_LOW:
            msg = self._read_one(STREAM_LOW, block_ms=100)
            if msg is None:
                msg = self._read_one(STREAM_HIGH, block_ms=100)
        else:
            # Normal path: prefer current-page responsiveness.
            msg = self._read_one(STREAM_HIGH, block_ms=100)
            if msg is None:
                msg = self._read_one(STREAM_LOW, block_ms=1000)

        if msg is not None:
            stream, msg_id, fields = msg
            self._process_message(stream, msg_id, fields)
            if stream == STREAM_HIGH:
                self._high_streak += 1
            else:
                self._high_streak = 0

        # Periodic heartbeat
        now = time.monotonic()
        if now - self._last_heartbeat >= self.heartbeat_interval:
            self._heartbeat()

    def _read_one(self, stream: str,
                  block_ms: int) -> tuple[str, bytes, dict] | None:
        """Read one message from a stream via XREADGROUP."""
        result = self._rdb.xreadgroup(
            self.consumer_group,
            self.consumer_name,
            {stream: ">"},
            count=1,
            block=block_ms,
        )
        if not result:
            return None

        # result: [(stream_name, [(msg_id, fields)])]
        stream_name, messages = result[0]
        if not messages:
            return None

        msg_id, fields = messages[0]
        return (stream_name.decode() if isinstance(stream_name, bytes) else stream_name,
                msg_id, fields)

    def _process_message(self, stream: str, msg_id: bytes,
                         fields: dict) -> None:
        """Process a single job message from the stream."""
        job_id = self._decode_field(fields, b"job_id")
        pipeline = self._decode_field(fields, b"pipeline")
        image_key = self._decode_field(fields, b"image_key")
        source_hash = self._decode_field(fields, b"source_hash")
        title = self._decode_field(fields, b"title")
        chapter = self._decode_field(fields, b"chapter")
        page_number = self._decode_field(fields, b"page_number")
        source_url = self._decode_field(fields, b"source_url")
        rerender_flag = self._decode_field(fields, b"rerender_from_metadata")
        rerender_from_metadata = rerender_flag in {"1", "true", "True"}

        if not job_id or not pipeline:
            log.warning("Malformed message %s: missing job_id or pipeline", msg_id)
            self._rdb.xack(stream, self.consumer_group, msg_id)
            return

        log.info("Processing job %s (pipeline=%s, stream=%s)",
                 job_id, pipeline, stream)

        # Fetch image bytes from Redis
        image_bytes = self._rdb.get(image_key) if image_key else None
        if not image_bytes and source_hash:
            # Redis key expired or missing — try v2 content-addressed cache.
            image_bytes = self._page_cache.load_object(source_hash)
            if image_bytes:
                log.info("Loaded source from v2 cache for job %s", job_id)
        if not image_bytes:
            log.error("Image not found for job %s (key=%s)", job_id, image_key)
            self._store_result(ProcessingResult(
                job_id=job_id, status="failed",
                error=f"Image not found: {image_key}",
            ))
            self._rdb.xack(stream, self.consumer_group, msg_id)
            return
        if not source_hash:
            source_hash = hashlib.sha256(image_bytes).hexdigest()

        # Progress callback
        def progress_cb(stage: str, detail: str, percent: int):
            self._publish_progress(job_id, stage, detail, percent)

        # Resolve metadata payload for rerender jobs.
        metadata_payload = None
        if rerender_from_metadata and source_hash:
            metadata_payload = self._page_cache.load_metadata_by_hash(
                pipeline, source_hash)
            if metadata_payload is None:
                log.warning("Metadata not found for rerender job %s (%s/%s)",
                            job_id, pipeline, source_hash)

        # Process
        job = ProcessingJob(
            job_id=job_id,
            pipeline=pipeline,
            image_bytes=image_bytes,
            title=title,
            chapter=chapter,
            page_number=page_number,
            source_url=source_url,
            source_hash=source_hash,
            rerender_from_metadata=rerender_from_metadata,
            metadata_payload=metadata_payload,
        )
        result = process_job(job, progress_cb=progress_cb)
        if not result.source_hash and source_hash:
            result.source_hash = source_hash

        # Save to robust filesystem cache v2 when image + metadata are available.
        if result.image_bytes and result.metadata_payload:
            self._cache_to_v2(
                pipeline=pipeline,
                source_hash=result.source_hash or source_hash,
                source_image_bytes=image_bytes,
                rendered_image_bytes=result.image_bytes,
                metadata_payload=result.metadata_payload,
                title=title,
                chapter=chapter,
                page_number=page_number,
                result=result,
            )

        # Store result and notify
        self._store_result(result)
        self._rdb.xack(stream, self.consumer_group, msg_id)

        log.info("Job %s completed: status=%s, bubbles=%d, time=%dms",
                 job_id, result.status, result.bubble_count,
                 result.processing_time_ms)

    def _store_result(self, result: ProcessingResult) -> None:
        """Store result metadata and image bytes in Redis."""
        meta = {
            "job_id": result.job_id,
            "status": result.status,
            "error": result.error,
            "processing_time_ms": result.processing_time_ms,
            "bubble_count": result.bubble_count,
            "pipeline": result.pipeline,
            "source_hash": result.source_hash,
            "content_hash": result.content_hash,
            "render_hash": result.render_hash,
        }
        meta_key = f"{RESULT_KEY_PREFIX}{result.job_id}"
        self._rdb.set(meta_key, json.dumps(meta), ex=RESULT_TTL)

        if result.image_bytes:
            img_key = f"{RESULT_IMG_PREFIX}{result.job_id}"
            self._rdb.set(img_key, result.image_bytes, ex=RESULT_TTL)

        # Publish notification for WebSocket subscribers
        notify_channel = f"{NOTIFY_PREFIX}{result.job_id}"
        self._rdb.publish(notify_channel, json.dumps(meta))

    def _publish_progress(self, job_id: str, stage: str, detail: str,
                          percent: int) -> None:
        """Publish a progress update via Redis SET + Pub/Sub."""
        progress = {
            "type": "progress",
            "job_id": job_id,
            "stage": stage,
            "detail": detail,
            "percent": percent,
        }
        progress_json = json.dumps(progress)
        # Store current progress (for polling)
        progress_key = f"{PROGRESS_PREFIX}{job_id}"
        self._rdb.set(progress_key, progress_json, ex=PROGRESS_TTL)
        # Publish for WebSocket subscribers
        notify_channel = f"{NOTIFY_PREFIX}{job_id}"
        self._rdb.publish(notify_channel, progress_json)

    def _cache_to_v2(self, *, pipeline: str, source_hash: str,
                     source_image_bytes: bytes, rendered_image_bytes: bytes,
                     metadata_payload: dict, title: str, chapter: str,
                     page_number: str, result: ProcessingResult) -> None:
        """Save processed output to robust cache v2 and update hashes."""
        if not source_hash:
            source_hash = hashlib.sha256(source_image_bytes).hexdigest()
        try:
            manifest = self._page_cache.store_page(
                pipeline=pipeline,
                source_hash=source_hash,
                source_image_bytes=source_image_bytes,
                rendered_image_bytes=rendered_image_bytes,
                metadata_payload=metadata_payload,
                title=title,
                chapter=chapter,
                page_number=page_number,
            )
            result.source_hash = source_hash
            result.content_hash = str(manifest.get("content_hash", ""))
            result.render_hash = str(manifest.get("render_hash", ""))
            log.info(
                "Cached v2 result pipeline=%s source=%s content=%s",
                pipeline,
                source_hash[:12],
                result.content_hash[:12] if result.content_hash else "-",
            )
        except Exception as e:
            log.error("Failed to cache v2 result for %s/%s: %s",
                      pipeline, source_hash[:12], e)

    def _cache_to_filesystem(self, pipeline: str, title: str, chapter: str,
                             page_number: str, image_bytes: bytes) -> None:
        """Legacy image-only cache write kept for backward compatibility."""
        slug = _slugify(title)
        cache_path = os.path.join(self.cache_dir, pipeline, slug, chapter,
                                  f"{page_number}.png")
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "wb") as f:
                f.write(image_bytes)
            log.info("Cached legacy result to %s", cache_path)
        except OSError as e:
            log.warning("Failed to cache legacy result: %s", e)

    def _heartbeat(self) -> None:
        """Update worker heartbeat key in Redis."""
        key = f"{HEARTBEAT_PREFIX}{self.consumer_name}:heartbeat"
        self._rdb.set(key, str(int(time.time())), ex=HEARTBEAT_TTL)
        self._last_heartbeat = time.monotonic()

    def _handle_signal(self, signum, frame) -> None:
        log.info("Received signal %d, shutting down...", signum)
        self._running = False

    @staticmethod
    def _decode_field(fields: dict, key: bytes) -> str:
        """Decode a field value from Redis bytes to string."""
        val = fields.get(key, b"")
        return val.decode() if isinstance(val, bytes) else str(val)
