import json
import hashlib
from unittest.mock import MagicMock, patch

import pytest

from kindle.translator import verify_ollama_model
from pipeline.model_verification import verify_file_sha256, verify_ollama_tags


def test_ollama_tag_accepts_exact_pinned_digest():
    digest = "a" * 64
    verify_ollama_tags(
        [{"name": "qwen3:14b", "digest": digest}],
        "qwen3:14b",
        "qwen3:14b",
        f"sha256:{digest}",
    )


@pytest.mark.parametrize(
    "models, configured, message",
    [
        ([], "qwen3:14b", "not installed"),
        ([{"name": "qwen3:14b", "digest": "b" * 64}], "qwen3:14b", "mismatch"),
        ([{"name": "qwen3:32b", "digest": "a" * 64}], "qwen3:14b", "not installed"),
    ],
)
def test_ollama_tag_rejects_unverified_model(models, configured, message):
    with pytest.raises(RuntimeError, match=message):
        verify_ollama_tags(models, configured, "qwen3:14b", "a" * 64)


def test_ollama_tag_rejects_different_configured_model():
    with pytest.raises(RuntimeError, match="does not match"):
        verify_ollama_tags([], "qwen3:32b", "qwen3:14b", "a" * 64)


def test_model_file_accepts_exact_bytes(tmp_path):
    model = tmp_path / "model.pt"
    model.write_bytes(b"weights")
    digest = hashlib.sha256(b"weights").hexdigest()

    assert verify_file_sha256(model, f"sha256:{digest}") == model


def test_model_file_hashing_does_not_use_unbounded_read_bytes(tmp_path, monkeypatch):
    model = tmp_path / "model.pt"
    model.write_bytes(b"weights")
    digest = hashlib.sha256(b"weights").hexdigest()
    monkeypatch.setattr(
        type(model),
        "read_bytes",
        lambda _self: (_ for _ in ()).throw(AssertionError("unbounded read")),
    )

    assert verify_file_sha256(model, digest) == model


def test_model_file_rejects_byte_drift(tmp_path):
    model = tmp_path / "model.pt"
    model.write_bytes(b"different")

    with pytest.raises(RuntimeError, match="digest mismatch"):
        verify_file_sha256(model, "a" * 64)


def test_translator_checks_ollama_tags_endpoint():
    response = MagicMock(status_code=200)
    response.iter_content.return_value = [
        json.dumps({"models": "unexpected"}).encode(),
    ]

    with (
        patch("kindle.translator.requests.get", return_value=response) as get,
        pytest.raises(TypeError, match="invalid Ollama"),
    ):
        verify_ollama_model("manga_translate")

    response.raise_for_status.assert_called_once_with()
    response.close.assert_called_once_with()
    assert get.call_args.args[0].endswith("/api/tags")
    assert get.call_args.kwargs["stream"] is True
