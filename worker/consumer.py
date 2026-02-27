"""Redis stream consumer for processing jobs with priority queues."""

import json
import logging
import os
import re
import signal
import time

import redis

from .job import ProcessingJob, ProcessingResult, process_job

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
        self._running = False
        self._rdb: redis.Redis | None = None
        self._last_heartbeat = 0.0

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
        """One iteration: check high queue, then low queue, then heartbeat."""
        # Check high-priority stream (short block)
        msg = self._read_one(STREAM_HIGH, block_ms=100)
        if msg is None:
            # Nothing in high — check low-priority (longer block)
            msg = self._read_one(STREAM_LOW, block_ms=1000)

        if msg is not None:
            stream, msg_id, fields = msg
            self._process_message(stream, msg_id, fields)

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
        title = self._decode_field(fields, b"title")
        chapter = self._decode_field(fields, b"chapter")
        page_number = self._decode_field(fields, b"page_number")
        source_url = self._decode_field(fields, b"source_url")

        if not job_id or not pipeline:
            log.warning("Malformed message %s: missing job_id or pipeline", msg_id)
            self._rdb.xack(stream, self.consumer_group, msg_id)
            return

        log.info("Processing job %s (pipeline=%s, stream=%s)",
                 job_id, pipeline, stream)

        # Fetch image bytes from Redis
        image_bytes = self._rdb.get(image_key) if image_key else None
        if not image_bytes:
            log.error("Image not found for job %s (key=%s)", job_id, image_key)
            self._store_result(ProcessingResult(
                job_id=job_id, status="failed",
                error=f"Image not found: {image_key}",
            ))
            self._rdb.xack(stream, self.consumer_group, msg_id)
            return

        # Progress callback
        def progress_cb(stage: str, detail: str, percent: int):
            self._publish_progress(job_id, stage, detail, percent)

        # Process
        job = ProcessingJob(
            job_id=job_id,
            pipeline=pipeline,
            image_bytes=image_bytes,
            title=title,
            chapter=chapter,
            page_number=page_number,
            source_url=source_url,
        )
        result = process_job(job, progress_cb=progress_cb)

        # Save to filesystem cache if metadata is present
        if result.image_bytes and title and chapter and page_number:
            self._cache_to_filesystem(pipeline, title, chapter, page_number,
                                      result.image_bytes)

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

    def _cache_to_filesystem(self, pipeline: str, title: str, chapter: str,
                             page_number: str, image_bytes: bytes) -> None:
        """Save processed image to filesystem cache."""
        slug = _slugify(title)
        cache_path = os.path.join(self.cache_dir, pipeline, slug, chapter,
                                  f"{page_number}.png")
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "wb") as f:
                f.write(image_bytes)
            log.info("Cached result to %s", cache_path)
        except OSError as e:
            log.warning("Failed to cache result: %s", e)

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
