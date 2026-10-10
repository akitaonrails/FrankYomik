"""Unit tests for webtoon Korean translator (mocked Ollama)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from webtoon.translator import translate, translate_sfx


def _chat_response(content: str) -> MagicMock:
    response = MagicMock(status_code=200)
    response.iter_content.return_value = [
        json.dumps({"message": {"content": content}}).encode()
    ]
    return response


@pytest.fixture(autouse=True)
def verified_model():
    with patch("webtoon.translator.verify_ollama_model"):
        yield


class TestTranslate:
    @patch("kindle.translator.requests.post")
    def test_ollama_success(self, mock_post):
        mock_resp = _chat_response("Hello")
        mock_post.return_value = mock_resp

        result = translate("안녕하세요")
        assert result == "Hello"
        # Verify chat API endpoint is used
        call_url = mock_post.call_args[0][0]
        assert call_url.endswith("/api/chat")

    @patch("kindle.translator.requests.post")
    def test_prompt_contains_korean_context(self, mock_post):
        mock_resp = _chat_response("Hello")
        mock_post.return_value = mock_resp

        translate("안녕하세요")
        payload = mock_post.call_args[1]["json"]
        prompt = payload["messages"][0]["content"]
        assert "Korean" in prompt
        assert "manhwa" in prompt or "webtoon" in prompt

    @patch("kindle.translator.requests.post", side_effect=Exception("timeout"))
    def test_fails_closed_on_ollama_failure(self, mock_post):
        with pytest.raises(Exception, match="timeout"):
            translate("안녕하세요")

    @patch("kindle.translator.requests.post")
    def test_fails_closed_on_empty_response(self, mock_post):
        mock_resp = _chat_response("<think>hmm</think>")
        mock_post.return_value = mock_resp

        with pytest.raises(RuntimeError, match="empty translation"):
            translate("안녕하세요")

    @patch("kindle.translator.requests.post")
    def test_pt_br_prompt_contains_portuguese(self, mock_post):
        mock_resp = _chat_response("Olá")
        mock_post.return_value = mock_resp

        result = translate("안녕하세요", target_lang="pt-br")
        assert result == "Olá"
        payload = mock_post.call_args[1]["json"]
        prompt = payload["messages"][0]["content"]
        assert "Brazilian Portuguese" in prompt


class TestTranslateSfx:
    """SFX translation uses a specialized prompt for onomatopoeia."""

    @patch("kindle.translator.requests.post")
    def test_sfx_prompt_contains_sound_effect(self, mock_post):
        """Prompt must mention sound effect for proper SFX translation."""
        mock_resp = _chat_response("CRASH")
        mock_post.return_value = mock_resp

        translate_sfx("꽈양")
        payload = mock_post.call_args[1]["json"]
        prompt = payload["messages"][0]["content"]
        assert "sound effect" in prompt.lower()

    @patch("kindle.translator.requests.post")
    def test_sfx_returns_uppercase(self, mock_post):
        """SFX result is always uppercased."""
        mock_resp = _chat_response("crash")
        mock_post.return_value = mock_resp

        result = translate_sfx("꽈양")
        assert result == "CRASH"
        assert result == result.upper()

    @patch("kindle.translator.requests.post")
    def test_sfx_strips_prefix(self, mock_post):
        """LLM sometimes adds 'SFX:' prefix — must be stripped."""
        mock_resp = _chat_response("SFX: SHUSH")
        mock_post.return_value = mock_resp

        result = translate_sfx("셈")
        assert result == "SHUSH"

    @patch("kindle.translator.requests.post", side_effect=Exception("timeout"))
    def test_sfx_fails_closed_on_ollama_failure(self, mock_post):
        with pytest.raises(Exception, match="timeout"):
            translate_sfx("쾅")

    @patch("kindle.translator.requests.post")
    def test_sfx_pt_br_prompt(self, mock_post):
        mock_resp = _chat_response("ESTRONDO")
        mock_post.return_value = mock_resp

        result = translate_sfx("쾅", target_lang="pt-br")
        assert result == "ESTRONDO"
        payload = mock_post.call_args[1]["json"]
        prompt = payload["messages"][0]["content"]
        assert "Brazilian Portuguese" in prompt
