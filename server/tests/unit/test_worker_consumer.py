"""Tests for worker consumer — Redis interaction logic."""

import json
import time
from unittest.mock import MagicMock, call, patch

from worker.consumer import (
    DEFAULT_HEARTBEAT_TTL,
    DEFAULT_PROGRESS_TTL,
    DEFAULT_RESULT_TTL,
    HEARTBEAT_PREFIX,
    IMAGE_KEY_PREFIX,
    LATEST_KEY_PREFIX,
    NOTIFY_PREFIX,
    PROGRESS_PREFIX,
    RESULT_IMG_PREFIX,
    RESULT_KEY_PREFIX,
    STREAM_HIGH,
    STREAM_LOW,
    Consumer,
)
from worker.preflight import ALL_PIPELINES, PreflightReport
from pipeline.revision import DEFAULT_PIPELINE_REVISION
from worker.job import ProcessingResult

# Aliases for backward-compat with test assertions
RESULT_TTL = DEFAULT_RESULT_TTL
HEARTBEAT_TTL = DEFAULT_HEARTBEAT_TTL
PROGRESS_TTL = DEFAULT_PROGRESS_TTL


def _make_consumer(**kwargs) -> Consumer:
    """Create a Consumer with mock Redis."""
    defaults = {
        "redis_url": "redis://localhost:6379",
        "consumer_name": "test-w",
        "pipeline_revision": "legacy-v1",
    }
    defaults.update(kwargs)
    defaults.setdefault("capabilities", PreflightReport(
        ALL_PIPELINES, defaults.get("pipeline_revision", DEFAULT_PIPELINE_REVISION),
        ("test-models",),
    ))
    c = Consumer(**defaults)
    c._rdb = MagicMock()
    return c


# --- Init ---


class TestConsumerInit:
    def test_requires_successful_preflight_report(self):
        import pytest

        with pytest.raises(ValueError, match="preflight"):
            Consumer(redis_url="redis://localhost:6379")

    def test_defaults(self):
        c = Consumer(
            redis_url="redis://localhost:6379",
            capabilities=PreflightReport(
                ALL_PIPELINES, DEFAULT_PIPELINE_REVISION, ("test-models",),
            ),
        )
        assert c.consumer_group == "workers"
        assert c.heartbeat_interval == 30
        assert c.job_timeout == 300
        assert c._running is False
        assert c._rdb is None
        assert c._last_heartbeat == 0.0
        assert c._high_streak == 0
        assert c.pipeline_revision == DEFAULT_PIPELINE_REVISION

    def test_custom_params(self):
        c = Consumer(
            redis_url="redis://custom:6380",
            consumer_group="mygroup",
            consumer_name="w-1",
            heartbeat_interval=10,
            job_timeout=60,
            capabilities=PreflightReport(
                ALL_PIPELINES, DEFAULT_PIPELINE_REVISION, ("test-models",),
            ),
        )
        assert c.consumer_name == "w-1"
        assert c.consumer_group == "mygroup"
        assert c.heartbeat_interval == 10
        assert c.job_timeout == 60

    def test_default_consumer_name_includes_pid(self):
        import os
        c = Consumer(
            redis_url="redis://localhost:6379",
            capabilities=PreflightReport(
                ALL_PIPELINES, DEFAULT_PIPELINE_REVISION, ("test-models",),
            ),
        )
        assert c.consumer_name == f"worker-{os.getpid()}"

    def test_explicit_pipeline_revision(self):
        c = Consumer(
            redis_url="redis://localhost:6379",
            pipeline_revision="detector-v2",
            capabilities=PreflightReport(
                ALL_PIPELINES, "detector-v2", ("test-models",),
            ),
        )
        assert c.pipeline_revision == "detector-v2"


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
        c._heartbeat()

        expected_key = f"{HEARTBEAT_PREFIX}test-w:heartbeat"
        pipe = c._rdb.pipeline.return_value
        assert pipe.set.call_count == 2
        args, kwargs = pipe.set.call_args_list[0]
        assert args[0] == expected_key
        assert kwargs["ex"] == HEARTBEAT_TTL
        # Value is unix timestamp
        ts = int(args[1])
        assert abs(ts - int(time.time())) <= 2
        capability_args, capability_kwargs = pipe.set.call_args_list[1]
        assert capability_args[0] == f"{HEARTBEAT_PREFIX}test-w:capabilities"
        payload = json.loads(capability_args[1])
        assert payload["pipelines"] == list(ALL_PIPELINES)
        assert payload["pipeline_revision"] == "legacy-v1"
        assert payload["preflight"] == "passed"
        assert capability_kwargs["ex"] == HEARTBEAT_TTL
        pipe.execute.assert_called_once_with()

    def test_updates_last_heartbeat_timestamp(self):
        c = _make_consumer()
        assert c._last_heartbeat == 0.0
        c._heartbeat()
        assert c._last_heartbeat > 0

    def test_rejects_capabilities_from_another_revision(self):
        import pytest

        with pytest.raises(ValueError, match="revision"):
            _make_consumer(capabilities=PreflightReport(
                ALL_PIPELINES, "different-revision", (),
            ))

    def test_rejects_partial_worker_before_it_can_claim_shared_queue(self):
        import pytest

        with pytest.raises(ValueError, match="every pipeline"):
            _make_consumer(capabilities=PreflightReport(
                ("webtoon",), "legacy-v1", ("easyocr-ko",),
            ))


# --- connect ---


class TestConnect:
    @patch("worker.consumer.redis")
    def test_creates_consumer_groups(self, mock_redis_module):
        mock_rdb = MagicMock()
        mock_redis_module.from_url.return_value = mock_rdb
        mock_redis_module.ResponseError = Exception

        c = _make_consumer()
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

        c = _make_consumer()
        c.connect()  # Should not raise

    @patch("worker.consumer.redis")
    def test_reraises_non_busygroup_error(self, mock_redis_module):
        mock_rdb = MagicMock()
        mock_redis_module.from_url.return_value = mock_rdb

        class FakeResponseError(Exception):
            pass
        mock_redis_module.ResponseError = FakeResponseError
        mock_rdb.xgroup_create.side_effect = FakeResponseError("WRONGTYPE some error")

        c = _make_consumer()
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

    @patch("worker.consumer.process_job")
    def test_pipeline_revision_mismatch_is_rejected(self, mock_process):
        c = _make_consumer(pipeline_revision="pipeline-v2")
        fields = {
            b"job_id": b"j-stale",
            b"pipeline": b"manga_translate",
            b"pipeline_revision": b"pipeline-v1",
            b"image_key": b"frank:images:abc",
        }

        result = c._process_message(STREAM_HIGH, b"2-1", fields)

        assert result == "skipped"
        mock_process.assert_not_called()
        c._rdb.get.assert_not_called()
        stored = json.loads(c._rdb.set.call_args_list[0][0][1])
        assert stored["status"] == "failed"
        assert stored["pipeline_revision"] == "pipeline-v2"
        assert "Pipeline revision mismatch" in stored["error"]
        c._rdb.xack.assert_called_once_with(STREAM_HIGH, "workers", b"2-1")

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
        c = _make_consumer(pipeline_revision="pipeline-v2")
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
            b"pipeline_revision": b"pipeline-v2",
            b"image_key": b"frank:images:hash123",
        }
        c._process_message(STREAM_HIGH, b"3-0", fields)

        # Verify process_job was called with correct job
        mock_process.assert_called_once()
        job = mock_process.call_args[0][0]
        assert job.job_id == "j-ok"
        assert job.pipeline == "manga_translate"
        assert job.image_bytes == b"fake-png-bytes"
        assert job.pipeline_revision == "pipeline-v2"

        # Verify result stored (meta + image)
        assert c._rdb.set.call_count == 2
        # Verify ACK
        c._rdb.xack.assert_called_once_with(STREAM_HIGH, "workers", b"3-0")
        # Verify notification published
        c._rdb.publish.assert_called_once()


class TestLatestPriorityDemotion:
    def test_stale_kindle_high_job_is_deferred_to_low(self):
        c = _make_consumer()
        latest_key = f"{LATEST_KEY_PREFIX}abc"
        c._rdb.get.return_value = b"page-2"

        fields = {
            b"job_id": b"j-old",
            b"pipeline": b"manga_translate",
            b"image_key": b"frank:images:old",
            b"source_site": b"kindle",
            b"latest_key": latest_key.encode(),
            b"latest_token": b"page-1",
        }
        outcome = c._process_message(STREAM_HIGH, b"10-0", fields)

        assert outcome == "deferred"
        c._rdb.get.assert_called_once_with(latest_key)
        c._rdb.xadd.assert_called_once()
        assert c._rdb.xadd.call_args[0][0] == STREAM_LOW
        deferred = c._rdb.xadd.call_args[0][1]
        assert deferred[b"job_id"] == b"j-old"
        assert deferred[b"deferred_from_high"] == b"1"
        c._rdb.xack.assert_called_once_with(STREAM_HIGH, "workers", b"10-0")

    @patch("worker.consumer.process_job")
    def test_current_kindle_high_job_processes_normally(self, mock_process):
        c = _make_consumer()
        latest_key = f"{LATEST_KEY_PREFIX}abc"
        c._rdb.get.side_effect = [b"7\npage-1", b"fake-png"]
        mock_process.return_value = ProcessingResult(
            job_id="j-current", status="completed", image_bytes=b"result",
        )

        fields = {
            b"job_id": b"j-current",
            b"pipeline": b"manga_translate",
            b"image_key": b"frank:images:current",
            b"source_site": b"kindle",
            b"latest_key": latest_key.encode(),
            b"latest_token": b"page-1",
        }
        outcome = c._process_message(STREAM_HIGH, b"11-0", fields)

        assert outcome == "processed"
        mock_process.assert_called_once()
        c._rdb.xadd.assert_not_called()
        c._rdb.xack.assert_called_once_with(STREAM_HIGH, "workers", b"11-0")

    @patch("worker.consumer.process_job")
    def test_missing_latest_marker_processes_normally(self, mock_process):
        c = _make_consumer()
        c._rdb.get.side_effect = [None, b"fake-png"]
        mock_process.return_value = ProcessingResult(
            job_id="j-missing", status="completed", image_bytes=b"result",
        )

        fields = {
            b"job_id": b"j-missing",
            b"pipeline": b"manga_translate",
            b"image_key": b"frank:images:missing",
            b"source_site": b"kindle",
            b"latest_key": f"{LATEST_KEY_PREFIX}missing".encode(),
            b"latest_token": b"page-1",
        }
        outcome = c._process_message(STREAM_HIGH, b"12-0", fields)

        assert outcome == "processed"
        mock_process.assert_called_once()
        c._rdb.xadd.assert_not_called()

    @patch("worker.consumer.process_job")
    def test_low_stream_stale_kindle_job_processes_normally(self, mock_process):
        c = _make_consumer()
        c._rdb.get.return_value = b"fake-png"
        mock_process.return_value = ProcessingResult(
            job_id="j-low", status="completed", image_bytes=b"result",
        )

        fields = {
            b"job_id": b"j-low",
            b"pipeline": b"manga_translate",
            b"image_key": b"frank:images:low",
            b"source_site": b"kindle",
            b"latest_key": f"{LATEST_KEY_PREFIX}low".encode(),
            b"latest_token": b"page-1",
        }
        outcome = c._process_message(STREAM_LOW, b"13-0", fields)

        assert outcome == "processed"
        mock_process.assert_called_once()
        c._rdb.xadd.assert_not_called()

    def test_failed_low_requeue_does_not_ack_high_message(self):
        c = _make_consumer()
        c._rdb.get.return_value = b"page-2"
        c._rdb.xadd.side_effect = RuntimeError("redis down")
        fields = {
            b"job_id": b"j-old",
            b"pipeline": b"manga_translate",
            b"image_key": b"frank:images:old",
            b"source_site": b"kindle",
            b"latest_key": f"{LATEST_KEY_PREFIX}abc".encode(),
            b"latest_token": b"page-1",
        }

        import pytest
        with pytest.raises(RuntimeError, match="redis down"):
            c._process_message(STREAM_HIGH, b"14-0", fields)
        c._rdb.xack.assert_not_called()

    def test_deferred_high_job_does_not_increment_high_burst(self):
        c = _make_consumer()
        c._read_one = MagicMock(return_value=(
            STREAM_HIGH, b"15-0", {b"job_id": b"j", b"pipeline": b"manga_translate"},
        ))
        c._process_message = MagicMock(return_value="deferred")
        c._last_heartbeat = time.monotonic()

        c._tick()

        assert c._high_streak == 0


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

    def test_stores_language_scoped_cache_pipeline(self):
        c = _make_consumer()
        result = ProcessingResult(
            job_id="translated-1", status="completed",
            pipeline="manga_translate",
        )

        c._store_result(result, cache_pipeline="manga_translate_pt-br")

        meta_json = json.loads(c._rdb.set.call_args_list[0][0][1])
        assert meta_json["pipeline"] == "manga_translate"
        assert meta_json["cache_pipeline"] == "manga_translate_pt-br"

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
        c._high_streak = c.high_burst_before_low
        call_order = []

        def mock_read(stream, block_ms):
            call_order.append(stream)

        c._read_one = mock_read
        c._last_heartbeat = time.monotonic()
        c._tick()

        assert call_order == [STREAM_LOW, STREAM_HIGH]

    def test_resets_high_streak_when_low_processed(self):
        c = _make_consumer()
        c._high_streak = c.high_burst_before_low

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
    def test_decodes_target_lang_field(self, mock_process):
        c = _make_consumer(cache_dir="/tmp/test_cache")
        c._rdb.get.return_value = b"fake-png-bytes"
        mock_process.return_value = ProcessingResult(
            job_id="j-lang", status="completed", image_bytes=b"result",
        )

        fields = {
            b"job_id": b"j-lang",
            b"pipeline": b"manga_translate",
            b"image_key": b"frank:images:abc",
            b"target_lang": b"pt-br",
        }
        c._process_message(STREAM_HIGH, b"1-0", fields)

        job = mock_process.call_args[0][0]
        assert job.target_lang == "pt-br"

    @patch("worker.consumer.process_job")
    def test_target_lang_defaults_to_en(self, mock_process):
        c = _make_consumer(cache_dir="/tmp/test_cache")
        c._rdb.get.return_value = b"fake-png-bytes"
        mock_process.return_value = ProcessingResult(
            job_id="j-lang-def", status="completed", image_bytes=b"result",
        )

        fields = {
            b"job_id": b"j-lang-def",
            b"pipeline": b"manga_translate",
            b"image_key": b"frank:images:abc",
        }
        c._process_message(STREAM_HIGH, b"2-0", fields)

        job = mock_process.call_args[0][0]
        assert job.target_lang == "en"

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
    def test_non_english_result_uses_language_cache_namespace(
        self, mock_process, tmp_path
    ):
        import hashlib

        c = _make_consumer(cache_dir=str(tmp_path))
        source_bytes = b"fake-png-pt-br"
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        c._rdb.get.return_value = source_bytes
        mock_process.return_value = ProcessingResult(
            job_id="j-cache-pt-br",
            status="completed",
            pipeline="manga_translate",
            image_bytes=b"png-result",
            metadata_payload={
                "schema_version": 2,
                "page_id": f"manga_translate:{source_hash}",
                "pipeline": "manga_translate",
                "pipeline_revision": "legacy-v1",
                "source_hash": source_hash,
                "image": {"width": 100, "height": 80},
                "regions": [],
                "stage_provenance": {},
            },
        )
        fields = {
            b"job_id": b"j-cache-pt-br",
            b"pipeline": b"manga_translate",
            b"target_lang": b"pt-br",
            b"image_key": b"frank:images:pt-br",
            b"source_hash": source_hash.encode(),
        }

        c._process_message(STREAM_HIGH, b"3-1", fields)

        manifest = c._page_cache.load_manifest_by_hash(
            "manga_translate_pt-br", source_hash)
        assert manifest is not None
        assert manifest["pipeline"] == "manga_translate_pt-br"
        metadata = c._page_cache.load_metadata_by_hash(
            "manga_translate_pt-br",
            source_hash,
            document_pipeline="manga_translate",
        )
        assert metadata is not None
        assert metadata["pipeline"] == "manga_translate"
        redis_metadata_keys = [
            call_args[0][0] for call_args in c._rdb.set.call_args_list
        ]
        assert (
            f"frank:meta:manga_translate_pt-br:{source_hash}"
            in redis_metadata_keys
        )

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

    @patch("worker.consumer.process_job")
    def test_non_english_rerender_loads_base_pipeline_document(
        self, mock_process, tmp_path
    ):
        import hashlib

        c = _make_consumer(cache_dir=str(tmp_path))
        source_bytes = b"pt-br-source"
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        c._rdb.get.return_value = source_bytes
        c._page_cache.store_page(
            pipeline="manga_translate_pt-br",
            document_pipeline="manga_translate",
            source_hash=source_hash,
            source_image_bytes=source_bytes,
            rendered_image_bytes=b"rendered-png",
            metadata_payload={
                "schema_version": 2,
                "page_id": f"manga_translate:{source_hash}",
                "pipeline": "manga_translate",
                "pipeline_revision": "legacy-v1",
                "source_hash": source_hash,
                "image": {"width": 100, "height": 80},
                "regions": [{
                    "id": "r1", "kind": "bubble",
                    "bbox": [1, 1, 20, 20], "reading_order": 0,
                }],
                "stage_provenance": {},
            },
        )
        mock_process.return_value = ProcessingResult(
            job_id="j-rerender-pt-br",
            status="completed",
            pipeline="manga_translate",
            image_bytes=b"rerendered-png",
            metadata_payload={
                "schema_version": 2,
                "page_id": f"manga_translate:{source_hash}",
                "pipeline": "manga_translate",
                "pipeline_revision": "legacy-v1",
                "source_hash": source_hash,
                "image": {"width": 100, "height": 80},
                "regions": [{
                    "id": "r1", "kind": "bubble",
                    "bbox": [1, 1, 20, 20], "reading_order": 0,
                }],
                "stage_provenance": {},
            },
        )
        fields = {
            b"job_id": b"j-rerender-pt-br",
            b"pipeline": b"manga_translate",
            b"target_lang": b"pt-br",
            b"image_key": b"frank:images:pt-br",
            b"source_hash": source_hash.encode(),
            b"rerender_from_metadata": b"1",
        }

        c._process_message(STREAM_HIGH, b"6-1", fields)

        job = mock_process.call_args[0][0]
        assert job.pipeline == "manga_translate"
        assert job.target_lang == "pt-br"
        assert job.metadata_payload["pipeline"] == "manga_translate"
