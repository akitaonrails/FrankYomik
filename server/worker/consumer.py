"""Redis stream consumer for processing jobs with priority queues."""

import json
import logging
import os
import signal
import time
import hashlib
from urllib.parse import urlparse, urlunparse

import redis

from .job import ProcessingJob, ProcessingResult, process_job
from .page_cache import PageCache

log = logging.getLogger(__name__)

# Stream and key names
STREAM_HIGH = "frank:jobs:high"
STREAM_LOW = "frank:jobs:low"
IMAGE_KEY_PREFIX = "frank:images:"
LATEST_KEY_PREFIX = "frank:latest:"
RESULT_KEY_PREFIX = "frank:results:"
RESULT_IMG_PREFIX = "frank:results:img:"
NOTIFY_PREFIX = "frank:notify:"
HEARTBEAT_PREFIX = "frank:worker:"
PROGRESS_PREFIX = "frank:progress:"
META_BY_HASH_PREFIX = "frank:meta:"

# Default TTLs (overridden by config.yaml worker: section)
DEFAULT_RESULT_TTL = 3600  # 1 hour
DEFAULT_HEARTBEAT_TTL = 60  # seconds
DEFAULT_PROGRESS_TTL = 60  # seconds
DEFAULT_HIGH_BURST_BEFORE_LOW = 3

# Redis/network timing. Values are deliberately conservative because workers run
# on a LAN server and should recover from brief Redis restarts without dropping
# stream entries.
REDIS_SOCKET_TIMEOUT_SECONDS = 30
REDIS_CONNECT_TIMEOUT_SECONDS = 10
RECONNECT_DELAY_SECONDS = 5
ERROR_RETRY_DELAY_SECONDS = 1

# Stream scheduling and PEL recovery.
HIGH_POLL_BLOCK_MS = 100
LOW_PROBE_BLOCK_MS = 100
LOW_POLL_BLOCK_MS = 1000
PENDING_CLAIM_INTERVAL_SECONDS = 30
PENDING_MIN_IDLE_MS = 60_000
PENDING_CLAIM_COUNT = 10


def _redact_url(url_str: str) -> str:
    """Mask the password in a Redis URL for safe logging."""
    try:
        parsed = urlparse(url_str)
        if parsed.password:
            replaced = parsed._replace(
                netloc=f"{parsed.username}:***@{parsed.hostname}"
                + (f":{parsed.port}" if parsed.port else "")
            )
            return urlunparse(replaced)
    except Exception:
        return "<invalid-url>"
    return url_str


class Consumer:
    """Redis stream consumer with two-stream priority."""

    def __init__(self, redis_url: str, consumer_group: str = "workers",
                 consumer_name: str | None = None,
                 heartbeat_interval: int = 30,
                 job_timeout: int = 300,
                 cache_dir: str = "./cache",
                 result_ttl: int = DEFAULT_RESULT_TTL,
                 heartbeat_ttl: int = DEFAULT_HEARTBEAT_TTL,
                 progress_ttl: int = DEFAULT_PROGRESS_TTL,
                 high_burst_before_low: int = DEFAULT_HIGH_BURST_BEFORE_LOW):
        self.redis_url = redis_url
        self.consumer_group = consumer_group
        self.consumer_name = consumer_name or f"worker-{os.getpid()}"
        self.heartbeat_interval = heartbeat_interval
        self.job_timeout = job_timeout
        self.cache_dir = cache_dir
        self.result_ttl = result_ttl
        self.heartbeat_ttl = heartbeat_ttl
        self.progress_ttl = progress_ttl
        # Process at most this many high-priority jobs consecutively before
        # giving low-priority prefetch jobs a chance.
        self.high_burst_before_low = high_burst_before_low
        self._page_cache = PageCache(cache_dir)
        self._running = False
        log.info("Cache v2 directory: %s", os.path.abspath(cache_dir))
        self._rdb: redis.Redis | None = None
        self._last_heartbeat = 0.0
        self._last_claim = 0.0
        self._high_streak = 0

    def connect(self) -> None:
        """Connect to Redis and ensure consumer groups exist."""
        self._rdb = redis.from_url(
            self.redis_url,
            decode_responses=False,
            socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
        )
        self._rdb.ping()
        log.info("Connected to Redis: %s", _redact_url(self.redis_url))

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
        self._claim_pending()

        while self._running:
            try:
                self._tick()
            except redis.ConnectionError:
                log.warning("Redis connection lost, reconnecting in %ds...",
                            RECONNECT_DELAY_SECONDS)
                time.sleep(RECONNECT_DELAY_SECONDS)
                try:
                    self.connect()
                except Exception:
                    log.exception("Reconnection failed")
            except redis.ResponseError as e:
                if "NOGROUP" in str(e):
                    log.warning("Consumer group gone, re-creating...")
                    try:
                        self.connect()
                    except Exception:
                        log.exception("Re-create consumer group failed")
                    time.sleep(ERROR_RETRY_DELAY_SECONDS)
                else:
                    log.exception("Redis error in consumer loop")
                    time.sleep(ERROR_RETRY_DELAY_SECONDS)
            except Exception:
                log.exception("Unexpected error in consumer loop")
                time.sleep(ERROR_RETRY_DELAY_SECONDS)

        log.info("Worker %s shutting down", self.consumer_name)

    def _tick(self) -> None:
        """One iteration with weighted high/low scheduling + heartbeat."""
        msg = None

        # After a burst of high jobs, probe low first to avoid starvation.
        if self._high_streak >= self.high_burst_before_low:
            msg = self._read_one(STREAM_LOW, block_ms=LOW_PROBE_BLOCK_MS)
            if msg is None:
                msg = self._read_one(STREAM_HIGH, block_ms=HIGH_POLL_BLOCK_MS)
        else:
            # Normal path: prefer current-page responsiveness.
            msg = self._read_one(STREAM_HIGH, block_ms=HIGH_POLL_BLOCK_MS)
            if msg is None:
                msg = self._read_one(STREAM_LOW, block_ms=LOW_POLL_BLOCK_MS)

        if msg is not None:
            stream, msg_id, fields = msg
            outcome = self._process_message(stream, msg_id, fields)
            if stream == STREAM_HIGH and outcome == "processed":
                self._high_streak += 1
            elif stream == STREAM_LOW:
                self._high_streak = 0

        # Periodic heartbeat and pending entry reclaim
        now = time.monotonic()
        if now - self._last_heartbeat >= self.heartbeat_interval:
            self._heartbeat()
        if now - self._last_claim >= PENDING_CLAIM_INTERVAL_SECONDS:
            self._claim_pending()

    def _claim_pending(self) -> None:
        """Claim orphaned entries from the PEL (pending entry list).

        When a consumer crashes between delivery and ACK, entries stay in the
        PEL and are never re-delivered by XREADGROUP ... >.  XAUTOCLAIM
        transfers entries idle for >60s to this consumer so they get processed.
        """
        self._last_claim = time.monotonic()
        for stream in (STREAM_HIGH, STREAM_LOW):
            try:
                # XAUTOCLAIM: claim entries idle for >60 seconds
                # Returns (next_start_id, [(msg_id, fields), ...], [deleted_ids])
                result = self._rdb.xautoclaim(
                    stream, self.consumer_group, self.consumer_name,
                    min_idle_time=PENDING_MIN_IDLE_MS,
                    start_id="0-0",
                    count=PENDING_CLAIM_COUNT,
                )
                if not result or not result[1]:
                    continue
                claimed = result[1]
                log.info("Claimed %d pending entries from %s", len(claimed), stream)
                for msg_id, fields in claimed:
                    if fields:  # Not a deleted/trimmed entry
                        self._process_message(stream, msg_id, fields)
                    else:
                        # Entry was trimmed from stream — just ACK it
                        self._rdb.xack(stream, self.consumer_group, msg_id)
            except redis.ResponseError as e:
                if "NOGROUP" not in str(e):
                    log.warning("XAUTOCLAIM error on %s: %s", stream, e)
            except Exception:
                log.exception("Error claiming pending entries from %s", stream)

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
                         fields: dict) -> str:
        """Process a single job message from the stream."""
        job_id = self._decode_field(fields, b"job_id")
        pipeline = self._decode_field(fields, b"pipeline")
        image_key = self._decode_field(fields, b"image_key")
        source_hash = self._decode_field(fields, b"source_hash")
        title = self._decode_field(fields, b"title")
        chapter = self._decode_field(fields, b"chapter")
        page_number = self._decode_field(fields, b"page_number")
        source_url = self._decode_field(fields, b"source_url")
        target_lang = self._decode_field(fields, b"target_lang") or "en"
        rerender_flag = self._decode_field(fields, b"rerender_from_metadata")
        rerender_from_metadata = rerender_flag in {"1", "true", "True"}

        if not job_id or not pipeline:
            log.warning("Malformed message %s: missing job_id or pipeline", msg_id)
            self._rdb.xack(stream, self.consumer_group, msg_id)
            return "skipped"

        if self._defer_stale_interactive_job(stream, msg_id, fields, job_id):
            return "deferred"

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
            return "skipped"
        if not source_hash:
            source_hash = hashlib.sha256(image_bytes).hexdigest()

        # Progress callback
        def progress_cb(stage: str, detail: str, percent: int):
            self._publish_progress(job_id, stage, detail, percent)

        # Resolve metadata payload for rerender jobs.
        cache_pipeline_for_lookup = pipeline if target_lang == "en" else f"{pipeline}_{target_lang}"
        metadata_payload = None
        if rerender_from_metadata and source_hash:
            metadata_payload = self._page_cache.load_metadata_by_hash(
                cache_pipeline_for_lookup, source_hash)
            if metadata_payload is None:
                log.error(
                    "Metadata not found for rerender job %s (%s/%s) — "
                    "cannot rerender without metadata",
                    job_id, pipeline, source_hash,
                )
                self._store_result(ProcessingResult(
                    job_id=job_id,
                    status="failed",
                    error="Metadata not found for rerender — "
                          "source may have been evicted from cache",
                    pipeline=pipeline,
                    source_hash=source_hash,
                ))
                self._rdb.xack(stream, self.consumer_group, msg_id)
                return "skipped"

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
            target_lang=target_lang,
        )
        result = process_job(job, progress_cb=progress_cb)
        if not result.source_hash and source_hash:
            result.source_hash = source_hash

        # Save to robust filesystem cache v2 when image + metadata are available.
        cache_pipeline = pipeline if target_lang == "en" else f"{pipeline}_{target_lang}"
        has_image = bool(result.image_bytes)
        has_metadata = bool(result.metadata_payload)
        log.info(
            "cache-v2 check: job=%s has_image=%s has_metadata=%s source_hash=%s",
            job_id, has_image, has_metadata,
            (result.source_hash or source_hash or "")[:12],
        )
        if has_image and has_metadata:
            self._cache_to_v2(
                pipeline=cache_pipeline,
                source_hash=result.source_hash or source_hash,
                source_image_bytes=image_bytes,
                rendered_image_bytes=result.image_bytes,
                metadata_payload=result.metadata_payload,
                title=title,
                chapter=chapter,
                page_number=page_number,
                result=result,
            )
        else:
            log.warning(
                "cache-v2 skipped for job %s: image=%s metadata=%s",
                job_id, has_image, has_metadata,
            )

        # Store result and notify
        self._store_result(result)
        self._rdb.xack(stream, self.consumer_group, msg_id)

        log.info("Job %s completed: status=%s, bubbles=%d, time=%dms",
                 job_id, result.status, result.bubble_count,
                 result.processing_time_ms)
        return "processed"

    def _defer_stale_interactive_job(self, stream: str, msg_id: bytes,
                                     fields: dict, job_id: str) -> bool:
        """Move stale Kindle high-priority jobs to low before expensive work.

        Redis Streams are FIFO within each stream. A rapid Kindle page flip can
        enqueue many high-priority pages before the page where the reader stops.
        The server records the latest visible page token per Kindle session;
        high-stream entries with an older token are requeued to the low stream
        so the current page can be processed first while older pages still catch
        up eventually.
        """
        if stream != STREAM_HIGH:
            return False
        if self._decode_field(fields, b"source_site") != "kindle":
            return False

        latest_token = self._decode_field(fields, b"latest_token")
        latest_key = self._decode_field(fields, b"latest_key")
        latest_group = self._decode_field(fields, b"latest_group")
        if not latest_token:
            return False
        if not latest_key and latest_group:
            latest_key = f"{LATEST_KEY_PREFIX}{hashlib.sha256(latest_group.encode()).hexdigest()}"
        if not latest_key:
            return False

        current_token = self._rdb.get(latest_key)
        if current_token is None:
            return False
        if isinstance(current_token, bytes):
            current_token = current_token.decode(errors="replace")
        else:
            current_token = str(current_token)
        if "\n" in current_token:
            current_token = current_token.split("\n", 1)[1]
        if current_token == latest_token:
            return False

        deferred_fields = dict(fields)
        deferred_fields[b"deferred_from_high"] = b"1"
        self._rdb.xadd(STREAM_LOW, deferred_fields)
        self._rdb.xack(stream, self.consumer_group, msg_id)
        log.info(
            "Deferred stale Kindle job %s from high to low (token=%s latest=%s)",
            job_id, latest_token, current_token,
        )
        return True

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
            "page_kind": result.page_kind,
        }
        meta_key = f"{RESULT_KEY_PREFIX}{result.job_id}"
        self._rdb.set(meta_key, json.dumps(meta), ex=self.result_ttl)

        if result.image_bytes:
            img_key = f"{RESULT_IMG_PREFIX}{result.job_id}"
            self._rdb.set(img_key, result.image_bytes, ex=self.result_ttl)

        # Store metadata payload in Redis keyed by pipeline:source_hash so the
        # server can serve it even when the v2 disk cache path doesn't match.
        if result.metadata_payload and result.source_hash and result.pipeline:
            meta_hash_key = (
                f"{META_BY_HASH_PREFIX}{result.pipeline}:{result.source_hash}"
            )
            meta_hash_val = json.dumps({
                "source_hash": result.source_hash,
                "content_hash": result.content_hash,
                "render_hash": result.render_hash,
                "metadata": result.metadata_payload,
            })
            self._rdb.set(meta_hash_key, meta_hash_val, ex=self.result_ttl)

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
        self._rdb.set(progress_key, progress_json, ex=self.progress_ttl)
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

    def _heartbeat(self) -> None:
        """Update worker heartbeat key in Redis."""
        key = f"{HEARTBEAT_PREFIX}{self.consumer_name}:heartbeat"
        self._rdb.set(key, str(int(time.time())), ex=self.heartbeat_ttl)
        self._last_heartbeat = time.monotonic()

    def _handle_signal(self, signum, frame) -> None:
        log.info("Received signal %d, shutting down...", signum)
        self._running = False

    @staticmethod
    def _decode_field(fields: dict, key: bytes) -> str:
        """Decode a field value from Redis bytes to string."""
        val = fields.get(key, b"")
        return val.decode() if isinstance(val, bytes) else str(val)
