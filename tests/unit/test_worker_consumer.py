"""Tests for worker consumer — Redis interaction logic."""

import json
import time
from unittest.mock import MagicMock, patch, call

from worker.consumer import (
    Consumer,
    HEARTBEAT_PREFIX,
    IMAGE_KEY_PREFIX,
    NOTIFY_PREFIX,
    RESULT_IMG_PREFIX,
    RESULT_KEY_PREFIX,
    RESULT_TTL,
    HEARTBEAT_TTL,
    STREAM_HIGH,
    STREAM_LOW,
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
