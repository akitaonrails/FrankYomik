"""Tests for worker consumer — Redis interaction logic."""

import json
import time
from unittest.mock import MagicMock, patch, call

from worker.consumer import (
    Consumer,
    HEARTBEAT_PREFIX,
    IMAGE_KEY_PREFIX,
    NOTIFY_PREFIX,
    PROGRESS_PREFIX,
    PROGRESS_TTL,
    RESULT_IMG_PREFIX,
    RESULT_KEY_PREFIX,
    RESULT_TTL,
    HEARTBEAT_TTL,
    STREAM_HIGH,
    STREAM_LOW,
    _slugify,
)
from worker.job import ProcessingResult


def _make_consumer(**kwargs) -> Consumer:
    """Create a Consumer with mock Redis."""
    defaults = {"redis_url": "redis://localhost:6379", "consumer_name": "test-w"}
    defaults.update(kwargs)
    c = Consumer(**defaults)
    c._rdb = MagicMock()
    return c


# --- Init ---


class TestConsumerInit:
    def test_defaults(self):
        c = Consumer(redis_url="redis://localhost:6379")
        assert c.consumer_group == "workers"
        assert c.heartbeat_interval == 30
        assert c.job_timeout == 300
        assert c._running is False
        assert c._rdb is None
        assert c._last_heartbeat == 0.0
        assert c._high_streak == 0

    def test_custom_params(self):
        c = Consumer(
            redis_url="redis://custom:6380",
            consumer_group="mygroup",
            consumer_name="w-1",
            heartbeat_interval=10,
            job_timeout=60,
        )
        assert c.consumer_name == "w-1"
        assert c.consumer_group == "mygroup"
        assert c.heartbeat_interval == 10
        assert c.job_timeout == 60

    def test_default_consumer_name_includes_pid(self):
        import os
        c = Consumer(redis_url="redis://localhost:6379")
        assert c.consumer_name == f"worker-{os.getpid()}"


# --- _decode_field ---


class TestDecodeField:
    def test_bytes_value(self):
        assert Consumer._decode_field({b"key": b"value"}, b"key") == "value"

    def test_missing_key(self):
        assert Consumer._decode_field({}, b"key") == ""

    def test_string_value(self):
        assert Consumer._decode_field({b"key": "already_str"}, b"key") == "already_str"

    def test_numeric_value_coerced(self):
        assert Consumer._decode_field({b"key": 42}, b"key") == "42"

    def test_empty_bytes(self):
        assert Consumer._decode_field({b"key": b""}, b"key") == ""


# --- _handle_signal ---


class TestHandleSignal:
    def test_sets_running_false(self):
        c = _make_consumer()
        c._running = True
        c._handle_signal(2, None)  # SIGINT = 2
        assert c._running is False

    def test_idempotent(self):
        c = _make_consumer()
        c._running = True
        c._handle_signal(15, None)
        c._handle_signal(15, None)
        assert c._running is False


# --- _heartbeat ---


class TestHeartbeat:
    def test_sets_redis_key_with_ttl(self):
        c = _make_consumer()
        before = time.monotonic()
        c._heartbeat()
        after = time.monotonic()

        expected_key = f"{HEARTBEAT_PREFIX}test-w:heartbeat"
        c._rdb.set.assert_called_once()
        args, kwargs = c._rdb.set.call_args
        assert args[0] == expected_key
        assert kwargs["ex"] == HEARTBEAT_TTL
        # Value is unix timestamp
        ts = int(args[1])
        assert abs(ts - int(time.time())) <= 2

    def test_updates_last_heartbeat_timestamp(self):
        c = _make_consumer()
        assert c._last_heartbeat == 0.0
        c._heartbeat()
        assert c._last_heartbeat > 0


# --- connect ---


class TestConnect:
    @patch("worker.consumer.redis")
    def test_creates_consumer_groups(self, mock_redis_module):
        mock_rdb = MagicMock()
        mock_redis_module.from_url.return_value = mock_rdb
        mock_redis_module.ResponseError = Exception

        c = Consumer(redis_url="redis://localhost:6379")
        c.connect()

        assert mock_rdb.xgroup_create.call_count == 2
        calls = mock_rdb.xgroup_create.call_args_list
        assert calls[0] == call(STREAM_HIGH, "workers", id="0", mkstream=True)
        assert calls[1] == call(STREAM_LOW, "workers", id="0", mkstream=True)

    @patch("worker.consumer.redis")
    def test_handles_busygroup_error(self, mock_redis_module):
        """BUSYGROUP means group already exists — should not raise."""
        mock_rdb = MagicMock()
        mock_redis_module.from_url.return_value = mock_rdb

        class FakeResponseError(Exception):
            pass
        mock_redis_module.ResponseError = FakeResponseError
        mock_rdb.xgroup_create.side_effect = FakeResponseError("BUSYGROUP already exists")

        c = Consumer(redis_url="redis://localhost:6379")
        c.connect()  # Should not raise

    @patch("worker.consumer.redis")
    def test_reraises_non_busygroup_error(self, mock_redis_module):
        mock_rdb = MagicMock()
        mock_redis_module.from_url.return_value = mock_rdb

        class FakeResponseError(Exception):
            pass
        mock_redis_module.ResponseError = FakeResponseError
        mock_rdb.xgroup_create.side_effect = FakeResponseError("WRONGTYPE some error")

        c = Consumer(redis_url="redis://localhost:6379")
        import pytest
        with pytest.raises(FakeResponseError, match="WRONGTYPE"):
            c.connect()


# --- _read_one ---


class TestReadOne:
    def test_returns_none_on_empty_result(self):
        c = _make_consumer()
        c._rdb.xreadgroup.return_value = []
        assert c._read_one(STREAM_HIGH, block_ms=100) is None

    def test_returns_none_on_none_result(self):
        c = _make_consumer()
        c._rdb.xreadgroup.return_value = None
        assert c._read_one(STREAM_HIGH, block_ms=100) is None

    def test_returns_none_on_empty_messages(self):
        c = _make_consumer()
        c._rdb.xreadgroup.return_value = [(b"frank:jobs:high", [])]
        assert c._read_one(STREAM_HIGH, block_ms=100) is None

    def test_parses_message_correctly(self):
        c = _make_consumer()
        msg_id = b"1234567890-0"
        fields = {b"job_id": b"j-1", b"pipeline": b"manga_translate"}
        c._rdb.xreadgroup.return_value = [
            (b"frank:jobs:high", [(msg_id, fields)])
        ]

        result = c._read_one(STREAM_HIGH, block_ms=100)
        assert result is not None
        stream, mid, f = result
        assert stream == "frank:jobs:high"
        assert mid == msg_id
        assert f == fields

    def test_decodes_string_stream_name(self):
        c = _make_consumer()
        c._rdb.xreadgroup.return_value = [
            ("frank:jobs:low", [(b"1-0", {b"job_id": b"j-2"})])
        ]
        result = c._read_one(STREAM_LOW, block_ms=1000)
        assert result[0] == "frank:jobs:low"

    def test_passes_correct_xreadgroup_params(self):
        c = _make_consumer()
        c._rdb.xreadgroup.return_value = []
        c._read_one(STREAM_HIGH, block_ms=250)

        c._rdb.xreadgroup.assert_called_once_with(
            "workers", "test-w",
            {STREAM_HIGH: ">"},
            count=1, block=250,
        )


# --- _process_message ---


class TestProcessMessage:
    def test_malformed_message_acks_and_returns(self):
        """Missing job_id or pipeline should ACK and skip."""
        c = _make_consumer()
        fields = {b"image_key": b"frank:images:abc"}  # no job_id, no pipeline
        c._process_message(STREAM_HIGH, b"1-0", fields)

        c._rdb.xack.assert_called_once_with(STREAM_HIGH, "workers", b"1-0")
        # Should NOT call process_job (no set on result keys)
        c._rdb.get.assert_not_called()

    def test_missing_image_stores_failed_result(self):
        c = _make_consumer()
        c._rdb.get.return_value = None  # image not found

        fields = {
            b"job_id": b"j-1",
            b"pipeline": b"manga_translate",
            b"image_key": b"frank:images:abc",
        }
        c._process_message(STREAM_HIGH, b"1-0", fields)

        # Should store failed result
        meta_call = c._rdb.set.call_args_list[0]
        meta_key = meta_call[0][0]
        meta_json = json.loads(meta_call[0][1])
        assert meta_key == f"{RESULT_KEY_PREFIX}j-1"
        assert meta_json["status"] == "failed"
        assert "not found" in meta_json["error"].lower()

        # Should ACK
        c._rdb.xack.assert_called_once()

    def test_missing_image_key_field_stores_failed(self):
        """Empty image_key field should fail gracefully."""
        c = _make_consumer()
        fields = {
            b"job_id": b"j-2",
            b"pipeline": b"manga_translate",
            b"image_key": b"",
        }
        c._process_message(STREAM_HIGH, b"2-0", fields)

        meta_call = c._rdb.set.call_args_list[0]
        meta_json = json.loads(meta_call[0][1])
        assert meta_json["status"] == "failed"
        c._rdb.xack.assert_called_once()

    @patch("worker.consumer.process_job")
    def test_successful_processing(self, mock_process):
        c = _make_consumer()
        c._rdb.get.return_value = b"fake-png-bytes"

        mock_process.return_value = ProcessingResult(
            job_id="j-ok",
            status="completed",
            image_bytes=b"\x89PNG-result",
            bubble_count=5,
            processing_time_ms=1200,
        )

        fields = {
            b"job_id": b"j-ok",
            b"pipeline": b"manga_translate",
            b"image_key": b"frank:images:hash123",
        }
        c._process_message(STREAM_HIGH, b"3-0", fields)

        # Verify process_job was called with correct job
        mock_process.assert_called_once()
        job = mock_process.call_args[0][0]
        assert job.job_id == "j-ok"
        assert job.pipeline == "manga_translate"
        assert job.image_bytes == b"fake-png-bytes"

        # Verify result stored (meta + image)
        assert c._rdb.set.call_count == 2
        # Verify ACK
        c._rdb.xack.assert_called_once_with(STREAM_HIGH, "workers", b"3-0")
        # Verify notification published
        c._rdb.publish.assert_called_once()


# --- _store_result ---


class TestStoreResult:
    def test_stores_metadata_json(self):
        c = _make_consumer()
        result = ProcessingResult(
            job_id="test-1", status="completed",
            image_bytes=b"fake", processing_time_ms=500, bubble_count=3,
        )
        c._store_result(result)

        meta_call = c._rdb.set.call_args_list[0]
        meta_key = meta_call[0][0]
        meta_json = json.loads(meta_call[0][1])

        assert meta_key == f"{RESULT_KEY_PREFIX}test-1"
        assert meta_json["job_id"] == "test-1"
        assert meta_json["status"] == "completed"
        assert meta_json["processing_time_ms"] == 500
        assert meta_json["bubble_count"] == 3
        assert meta_json["error"] == ""
        assert meta_call[1]["ex"] == RESULT_TTL

    def test_stores_image_bytes_with_ttl(self):
        c = _make_consumer()
        result = ProcessingResult(
            job_id="img-1", status="completed", image_bytes=b"png-data",
        )
        c._store_result(result)

        img_call = c._rdb.set.call_args_list[1]
        assert img_call[0][0] == f"{RESULT_IMG_PREFIX}img-1"
        assert img_call[0][1] == b"png-data"
        assert img_call[1]["ex"] == RESULT_TTL

    def test_skips_image_when_none(self):
        c = _make_consumer()
        result = ProcessingResult(
            job_id="fail-1", status="failed", error="bad image",
        )
        c._store_result(result)

        assert c._rdb.set.call_count == 1  # Only meta
        c._rdb.publish.assert_called_once()

    def test_publishes_notification(self):
        c = _make_consumer()
        result = ProcessingResult(job_id="n-1", status="completed")
        c._store_result(result)

        c._rdb.publish.assert_called_once()
        channel = c._rdb.publish.call_args[0][0]
        payload = json.loads(c._rdb.publish.call_args[0][1])
        assert channel == f"{NOTIFY_PREFIX}n-1"
        assert payload["job_id"] == "n-1"
        assert payload["status"] == "completed"

    def test_failed_result_includes_error_in_notification(self):
        c = _make_consumer()
        result = ProcessingResult(
            job_id="e-1", status="failed", error="decode failed",
        )
        c._store_result(result)

        payload = json.loads(c._rdb.publish.call_args[0][1])
        assert payload["error"] == "decode failed"


# --- _tick ---


class TestTick:
    def test_checks_high_first(self):
        """_tick should read high-priority stream before low."""
        c = _make_consumer()
        call_order = []

        def mock_read(stream, block_ms):
            call_order.append(stream)
            return None

        c._read_one = mock_read
        c._last_heartbeat = time.monotonic()  # prevent heartbeat call
        c._tick()

        assert call_order == [STREAM_HIGH, STREAM_LOW]

    def test_skips_low_when_high_has_message(self):
        """When high stream returns a message, low is not checked."""
        c = _make_consumer()
        call_order = []

        def mock_read(stream, block_ms):
            call_order.append(stream)
            if stream == STREAM_HIGH:
                return (STREAM_HIGH, b"1-0", {b"job_id": b"j-1", b"pipeline": b"manga_translate"})
            return None

        c._read_one = mock_read
        c._process_message = MagicMock()
        c._last_heartbeat = time.monotonic()
        c._tick()

        assert call_order == [STREAM_HIGH]
        c._process_message.assert_called_once()

    def test_processes_low_when_high_empty(self):
        c = _make_consumer()

        def mock_read(stream, block_ms):
            if stream == STREAM_LOW:
                return (STREAM_LOW, b"2-0", {b"job_id": b"j-2"})
            return None

        c._read_one = mock_read
        c._process_message = MagicMock()
        c._last_heartbeat = time.monotonic()
        c._tick()

        c._process_message.assert_called_once_with(
            STREAM_LOW, b"2-0", {b"job_id": b"j-2"}
        )

    def test_checks_low_first_after_high_burst(self):
        c = _make_consumer()
        c._high_streak = c.HIGH_BURST_BEFORE_LOW
        call_order = []

        def mock_read(stream, block_ms):
            call_order.append(stream)
            return None

        c._read_one = mock_read
        c._last_heartbeat = time.monotonic()
        c._tick()

        assert call_order == [STREAM_LOW, STREAM_HIGH]

    def test_resets_high_streak_when_low_processed(self):
        c = _make_consumer()
        c._high_streak = c.HIGH_BURST_BEFORE_LOW

        def mock_read(stream, block_ms):
            if stream == STREAM_LOW:
                return (STREAM_LOW, b"2-0", {b"job_id": b"j-2"})
            return None

        c._read_one = mock_read
        c._process_message = MagicMock()
        c._last_heartbeat = time.monotonic()
        c._tick()

        assert c._high_streak == 0

    def test_heartbeat_when_interval_elapsed(self):
        c = _make_consumer(heartbeat_interval=0)  # trigger immediately
        c._read_one = MagicMock(return_value=None)
        c._last_heartbeat = 0.0  # long ago
        c._tick()

        # Heartbeat should have been called (sets _last_heartbeat)
        assert c._last_heartbeat > 0

    def test_no_heartbeat_when_recent(self):
        c = _make_consumer(heartbeat_interval=9999)
        c._read_one = MagicMock(return_value=None)
        c._last_heartbeat = time.monotonic()
        old_hb = c._last_heartbeat
        c._tick()
        # Should not have updated
        assert c._last_heartbeat == old_hb


# --- Stream constants ---


class TestStreamConstants:
    def test_stream_names(self):
        assert STREAM_HIGH == "frank:jobs:high"
        assert STREAM_LOW == "frank:jobs:low"

    def test_key_prefixes(self):
        assert IMAGE_KEY_PREFIX == "frank:images:"
        assert RESULT_KEY_PREFIX == "frank:results:"
        assert RESULT_IMG_PREFIX == "frank:results:img:"
        assert NOTIFY_PREFIX == "frank:notify:"
        assert HEARTBEAT_PREFIX == "frank:worker:"

    def test_ttls(self):
        assert RESULT_TTL == 3600
        assert HEARTBEAT_TTL == 60

    def test_progress_constants(self):
        assert PROGRESS_PREFIX == "frank:progress:"
        assert PROGRESS_TTL == 60


# --- _slugify ---


class TestSlugify:
    def test_basic(self):
        assert _slugify("One Piece") == "one-piece"

    def test_already_slugified(self):
        assert _slugify("tower-of-god") == "tower-of-god"

    def test_strips_special_chars(self):
        assert _slugify("Test Manga!@#") == "test-manga"

    def test_strips_whitespace(self):
        assert _slugify("  spaces  ") == "spaces"

    def test_collapses_hyphens(self):
        assert _slugify("a---b") == "a-b"


# --- _publish_progress ---


class TestPublishProgress:
    def test_publishes_set_and_pubsub(self):
        c = _make_consumer()
        c._publish_progress("j-1", "translating", "3/7 bubbles", 43)

        # Should SET progress key
        set_call = c._rdb.set.call_args
        assert set_call[0][0] == f"{PROGRESS_PREFIX}j-1"
        progress = json.loads(set_call[0][1])
        assert progress["type"] == "progress"
        assert progress["stage"] == "translating"
        assert progress["detail"] == "3/7 bubbles"
        assert progress["percent"] == 43
        assert set_call[1]["ex"] == PROGRESS_TTL

        # Should PUBLISH notification
        c._rdb.publish.assert_called_once()
        channel = c._rdb.publish.call_args[0][0]
        assert channel == f"{NOTIFY_PREFIX}j-1"

    def test_progress_payload_json(self):
        c = _make_consumer()
        c._publish_progress("j-2", "detecting_bubbles", "", 10)

        payload = json.loads(c._rdb.publish.call_args[0][1])
        assert payload["job_id"] == "j-2"
        assert payload["stage"] == "detecting_bubbles"
        assert payload["percent"] == 10


# --- _cache_to_filesystem ---


class TestCacheToFilesystem:
    def test_creates_file(self, tmp_path):
        c = _make_consumer(cache_dir=str(tmp_path))
        c._cache_to_filesystem("manga_translate", "One Piece", "1", "003",
                               b"fake-png")

        cached_file = tmp_path / "manga_translate" / "one-piece" / "1" / "003.png"
        assert cached_file.exists()
        assert cached_file.read_bytes() == b"fake-png"

    def test_handles_special_chars_in_title(self, tmp_path):
        c = _make_consumer(cache_dir=str(tmp_path))
        c._cache_to_filesystem("webtoon", "Tower of God!", "297", "038",
                               b"img-data")

        cached_file = tmp_path / "webtoon" / "tower-of-god" / "297" / "038.png"
        assert cached_file.exists()

    def test_no_error_on_bad_path(self, tmp_path):
        """Should log warning but not raise on write failure."""
        c = _make_consumer(cache_dir="/nonexistent/readonly/path")
        # This should not raise
        c._cache_to_filesystem("manga_translate", "test", "1", "1", b"data")


# --- _process_message with metadata ---


class TestProcessMessageMetadata:
    @patch("worker.consumer.process_job")
    def test_decodes_metadata_fields(self, mock_process):
        c = _make_consumer(cache_dir="/tmp/test_cache")
        c._rdb.get.return_value = b"fake-png-bytes"
        mock_process.return_value = ProcessingResult(
            job_id="j-meta", status="completed", image_bytes=b"result",
        )

        fields = {
            b"job_id": b"j-meta",
            b"pipeline": b"manga_translate",
            b"image_key": b"frank:images:abc",
            b"title": b"One Piece",
            b"chapter": b"1084",
            b"page_number": b"003",
            b"source_url": b"https://example.com",
        }
        c._process_message(STREAM_HIGH, b"1-0", fields)

        job = mock_process.call_args[0][0]
        assert job.title == "One Piece"
        assert job.chapter == "1084"
        assert job.page_number == "003"
        assert job.source_url == "https://example.com"

    @patch("worker.consumer.process_job")
    def test_passes_progress_callback(self, mock_process):
        c = _make_consumer()
        c._rdb.get.return_value = b"fake-png"
        mock_process.return_value = ProcessingResult(
            job_id="j-cb", status="completed",
        )

        fields = {
            b"job_id": b"j-cb",
            b"pipeline": b"manga_translate",
            b"image_key": b"frank:images:abc",
        }
        c._process_message(STREAM_HIGH, b"2-0", fields)

        # process_job should be called with progress_cb kwarg
        _, kwargs = mock_process.call_args
        assert "progress_cb" in kwargs
        assert callable(kwargs["progress_cb"])

    @patch("worker.consumer.process_job")
    def test_caches_to_v2_on_success(self, mock_process, tmp_path):
        import hashlib
        c = _make_consumer(cache_dir=str(tmp_path))
        source_bytes = b"fake-png"
        c._rdb.get.return_value = source_bytes
        mock_process.return_value = ProcessingResult(
            job_id="j-cache", status="completed", image_bytes=b"png-result",
            metadata_payload={"regions": []},
        )

        fields = {
            b"job_id": b"j-cache",
            b"pipeline": b"manga_translate",
            b"image_key": b"frank:images:abc",
            b"title": b"test-manga",
            b"chapter": b"1",
            b"page_number": b"001",
        }
        c._process_message(STREAM_HIGH, b"3-0", fields)

        source_hash = hashlib.sha256(source_bytes).hexdigest()
        manifest_path = (tmp_path / "v2" / "pages" / "by-hash" /
                         "manga_translate" / source_hash / "manifest.json")
        assert manifest_path.exists()
        import json
        manifest = json.loads(manifest_path.read_bytes())
        assert manifest["pipeline"] == "manga_translate"
        assert manifest["source_hash"] == source_hash
        assert manifest["image_stale"] is False

    @patch("worker.consumer.process_job")
    def test_no_cache_without_metadata(self, mock_process, tmp_path):
        c = _make_consumer(cache_dir=str(tmp_path))
        c._rdb.get.return_value = b"fake-png"
        mock_process.return_value = ProcessingResult(
            job_id="j-nocache", status="completed", image_bytes=b"png-result",
        )

        fields = {
            b"job_id": b"j-nocache",
            b"pipeline": b"manga_translate",
            b"image_key": b"frank:images:abc",
            # No title/chapter/page_number
        }
        c._process_message(STREAM_HIGH, b"4-0", fields)

        # No v2 manifests should exist (no metadata_payload → skip)
        manifest_dir = tmp_path / "v2" / "pages" / "by-hash"
        assert not manifest_dir.exists()

    @patch("worker.consumer.process_job")
    def test_rerender_missing_metadata_fails_job(self, mock_process, tmp_path):
        """When rerender_from_metadata=1 but metadata can't be found,
        the consumer should fail the job explicitly instead of proceeding."""
        c = _make_consumer(cache_dir=str(tmp_path))
        c._rdb.get.return_value = b"fake-png"
        # process_job should NOT be called — consumer fails early
        mock_process.return_value = ProcessingResult(
            job_id="j-rerender", status="completed",
        )

        fields = {
            b"job_id": b"j-rerender",
            b"pipeline": b"manga_translate",
            b"image_key": b"frank:images:abc",
            b"source_hash": b"abc123" * 11,  # 66-char fake hash
            b"rerender_from_metadata": b"1",
        }
        c._process_message(STREAM_HIGH, b"5-0", fields)

        # process_job should NOT have been called — consumer failed early
        mock_process.assert_not_called()

        # _store_result should have been called with "failed" status
        import json
        result_key = "frank:results:j-rerender"
        set_calls = c._rdb.set.call_args_list
        result_call = [
            call for call in set_calls
            if call[0][0] == result_key
        ]
        assert len(result_call) == 1
        stored_meta = json.loads(result_call[0][0][1])
        assert stored_meta["status"] == "failed"
        assert "Metadata not found" in stored_meta["error"]

    @patch("worker.consumer.process_job")
    def test_rerender_with_metadata_proceeds(self, mock_process, tmp_path):
        """When rerender_from_metadata=1 and metadata IS found,
        the consumer should proceed normally."""
        import hashlib
        c = _make_consumer(cache_dir=str(tmp_path))
        source_bytes = b"fake-source-image"
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        c._rdb.get.return_value = source_bytes

        # Seed the v2 cache with metadata so the consumer can find it
        c._page_cache.store_page(
            pipeline="manga_translate",
            source_hash=source_hash,
            source_image_bytes=source_bytes,
            rendered_image_bytes=b"rendered-png",
            metadata_payload={"regions": [{"id": "r1"}]},
        )

        mock_process.return_value = ProcessingResult(
            job_id="j-rerender-ok", status="completed",
            image_bytes=b"rerendered-png",
            metadata_payload={"regions": [{"id": "r1"}]},
        )

        fields = {
            b"job_id": b"j-rerender-ok",
            b"pipeline": b"manga_translate",
            b"image_key": b"frank:images:abc",
            b"source_hash": source_hash.encode(),
            b"rerender_from_metadata": b"1",
        }
        c._process_message(STREAM_HIGH, b"6-0", fields)

        # process_job should have been called with metadata_payload populated
        mock_process.assert_called_once()
        job = mock_process.call_args[0][0]
        assert job.rerender_from_metadata is True
        assert job.metadata_payload is not None
        assert job.metadata_payload["regions"][0]["id"] == "r1"
