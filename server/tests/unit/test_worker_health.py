"""Tests for worker health check utilities."""

import time
from unittest.mock import MagicMock, patch, PropertyMock

from worker.health import check_health


class TestCheckHealthRedisDown:
    @patch("worker.health.redis")
    def test_returns_unhealthy_on_connection_error(self, mock_redis_module):
        mock_redis_module.ConnectionError = ConnectionError
        mock_redis_module.TimeoutError = TimeoutError
        mock_redis_module.from_url.side_effect = ConnectionError("refused")

        result = check_health("redis://localhost:6379")
        assert result["status"] == "unhealthy"
        assert "unreachable" in result["error"].lower()

    @patch("worker.health.redis")
    def test_returns_unhealthy_on_timeout(self, mock_redis_module):
        mock_redis_module.ConnectionError = ConnectionError
        mock_redis_module.TimeoutError = TimeoutError
        mock_redis_module.from_url.side_effect = TimeoutError("timed out")

        result = check_health("redis://localhost:6379")
        assert result["status"] == "unhealthy"

    @patch("worker.health.redis")
    def test_returns_unhealthy_on_ping_failure(self, mock_redis_module):
        mock_rdb = MagicMock()
        mock_redis_module.from_url.return_value = mock_rdb
        mock_redis_module.ConnectionError = ConnectionError
        mock_redis_module.TimeoutError = TimeoutError
        mock_rdb.ping.side_effect = ConnectionError("reset")

        result = check_health("redis://localhost:6379")
        assert result["status"] == "unhealthy"


class TestCheckHealthHealthy:
    def _setup_mock(self, mock_redis_module):
        mock_rdb = MagicMock()
        mock_redis_module.from_url.return_value = mock_rdb
        mock_redis_module.ConnectionError = ConnectionError
        mock_redis_module.TimeoutError = TimeoutError
        mock_redis_module.ResponseError = Exception
        mock_rdb.keys.return_value = []
        return mock_rdb

    @patch("worker.health.redis")
    def test_returns_healthy(self, mock_redis_module):
        mock_rdb = self._setup_mock(mock_redis_module)
        mock_rdb.xlen.return_value = 0
        mock_rdb.xpending.return_value = {"pending": 0}

        result = check_health("redis://localhost:6379")
        assert result["status"] == "healthy"

    @patch("worker.health.redis")
    def test_includes_queue_lengths(self, mock_redis_module):
        mock_rdb = self._setup_mock(mock_redis_module)

        def xlen_side(stream):
            if "high" in stream:
                return 5
            return 12
        mock_rdb.xlen.side_effect = xlen_side
        mock_rdb.xpending.return_value = {"pending": 0}

        result = check_health("redis://localhost:6379")
        assert result["queue_high"] == 5
        assert result["queue_low"] == 12

    @patch("worker.health.redis")
    def test_queue_length_handles_missing_stream(self, mock_redis_module):
        mock_rdb = self._setup_mock(mock_redis_module)
        mock_rdb.xlen.side_effect = Exception("no such key")
        mock_rdb.xpending.return_value = {"pending": 0}

        result = check_health("redis://localhost:6379")
        assert result["queue_high"] == 0
        assert result["queue_low"] == 0

    @patch("worker.health.redis")
    def test_finds_active_workers(self, mock_redis_module):
        mock_rdb = self._setup_mock(mock_redis_module)
        mock_rdb.xlen.return_value = 0
        mock_rdb.xpending.return_value = {"pending": 0}

        now = int(time.time())
        mock_rdb.keys.return_value = [
            "frank:worker:w-1:heartbeat",
            "frank:worker:w-2:heartbeat",
        ]
        mock_rdb.get.side_effect = [str(now - 10), str(now - 200)]

        result = check_health("redis://localhost:6379")
        assert len(result["active_workers"]) == 1
        assert result["active_workers"][0]["name"] == "w-1"

    @patch("worker.health.redis")
    def test_stale_workers_excluded(self, mock_redis_module):
        mock_rdb = self._setup_mock(mock_redis_module)
        mock_rdb.xlen.return_value = 0
        mock_rdb.xpending.return_value = {"pending": 0}

        now = int(time.time())
        mock_rdb.keys.return_value = ["frank:worker:old:heartbeat"]
        mock_rdb.get.return_value = str(now - 300)  # 5 minutes old

        result = check_health("redis://localhost:6379")
        assert len(result["active_workers"]) == 0

    @patch("worker.health.redis")
    def test_includes_pending_counts(self, mock_redis_module):
        mock_rdb = self._setup_mock(mock_redis_module)
        mock_rdb.xlen.return_value = 0

        def xpending_side(stream, group):
            if "high" in stream:
                return {"pending": 3}
            return {"pending": 1}
        mock_rdb.xpending.side_effect = xpending_side

        result = check_health("redis://localhost:6379")
        assert result["pending"]["high"] == 3
        assert result["pending"]["low"] == 1

    @patch("worker.health.redis")
    def test_pending_handles_error(self, mock_redis_module):
        mock_rdb = self._setup_mock(mock_redis_module)
        mock_rdb.xlen.return_value = 0
        mock_rdb.xpending.side_effect = Exception("NOGROUP")

        result = check_health("redis://localhost:6379")
        assert result["pending"]["high"] == 0
        assert result["pending"]["low"] == 0

    @patch("worker.health.redis")
    def test_custom_consumer_group(self, mock_redis_module):
        mock_rdb = self._setup_mock(mock_redis_module)
        mock_rdb.xlen.return_value = 0
        mock_rdb.xpending.return_value = {"pending": 0}

        result = check_health("redis://localhost:6379", consumer_group="custom")
        assert result["status"] == "healthy"
        # Verify xpending was called with custom group
        for c in mock_rdb.xpending.call_args_list:
            assert c[0][1] == "custom"
