import hashlib

import pytest
from PIL import Image

import kindle.hayai_ocr as hayai
from kindle.hayai_ocr import HayaiOcrBackend, HayaiTimeout, HayaiUnavailable


class _HungProcess:
    def __init__(self):
        self.terminated = False
        self.killed = False

    def is_alive(self):
        return not self.terminated and not self.killed

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def join(self, timeout=None):
        return None


class _HungConnection:
    def __init__(self):
        self.closed = False

    def send(self, _payload):
        return None

    def poll(self, _timeout):
        return False

    def close(self):
        self.closed = True


def test_timeout_terminates_inference_process():
    backend = HayaiOcrBackend(timeout_seconds=0.01)
    process = _HungProcess()
    connection = _HungConnection()
    backend._process = process
    backend._connection = connection

    try:
        backend(Image.new("RGB", (10, 10)))
    except HayaiTimeout:
        pass
    else:
        raise AssertionError("timeout was not raised")

    assert process.terminated is True
    assert connection.closed is True
    assert backend._process is None


def test_snapshot_bytes_are_checked_before_model_load(tmp_path, monkeypatch):
    artifact = tmp_path / "config.json"
    artifact.write_bytes(b"pinned")
    monkeypatch.setattr(
        hayai, "MODEL_FILES",
        {"config.json": hashlib.sha256(b"pinned").hexdigest()},
    )
    options = {}

    def snapshot(**kwargs):
        options.update(kwargs)
        return str(tmp_path)

    monkeypatch.setattr("huggingface_hub.snapshot_download", snapshot)

    assert hayai._verified_snapshot() == tmp_path
    assert options["revision"] == hayai.MODEL_REVISION
    assert options["local_files_only"] is True

    artifact.write_bytes(b"changed")
    with pytest.raises(HayaiUnavailable, match="byte check failed"):
        hayai._verified_snapshot()
