"""Worker startup order: preflight must pass before Redis consumption."""

from unittest.mock import MagicMock, patch

import pytest

from worker.main import main
from worker.preflight import ALL_PIPELINES, PreflightError, PreflightReport


@patch("worker.main._load_yaml_config", return_value={})
@patch("worker.main.Consumer")
@patch("worker.main.preflight_worker")
def test_main_connects_only_after_successful_preflight(
    preflight, consumer_class, _config, monkeypatch,
):
    report = PreflightReport(ALL_PIPELINES, "pipeline-test", ("models",))
    preflight.return_value = report
    consumer = MagicMock()
    consumer_class.return_value = consumer
    monkeypatch.setattr("sys.argv", ["worker", "--pipeline", "both"])

    main()

    preflight.assert_called_once_with(ALL_PIPELINES)
    assert consumer_class.call_args.kwargs["capabilities"] is report
    consumer.connect.assert_called_once_with()
    consumer.run.assert_called_once_with()


@patch("worker.main._load_yaml_config", return_value={})
@patch("worker.main.Consumer")
@patch("worker.main.preflight_worker", side_effect=PreflightError("missing OCR"))
def test_main_never_connects_when_preflight_fails(
    _preflight, consumer_class, _config, monkeypatch,
):
    monkeypatch.setattr("sys.argv", ["worker", "--pipeline", "both"])

    with pytest.raises(SystemExit):
        main()

    consumer_class.assert_not_called()
