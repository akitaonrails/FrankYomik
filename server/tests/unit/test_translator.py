"""Unit tests for translator module (mocked Ollama)."""

from unittest.mock import MagicMock, patch

import pytest

from kindle.translator import LANG_MAP, _clean_response, translate


@pytest.fixture(autouse=True)
def verified_model():
    with patch("kindle.translator.verify_ollama_model"):
        yield


class TestCleanResponse:
    def test_strips_think_tags(self):
        assert _clean_response("<think>reasoning</think>Hello") == "Hello"

    def test_strips_xml_tags(self):
        assert _clean_response("<output>Hello</output>") == "Hello"

    def test_strips_quotes(self):
        assert _clean_response('"Hello world"') == "Hello world"
        assert _clean_response("'Hello world'") == "Hello world"

    def test_strips_whitespace(self):
        assert _clean_response("  Hello  ") == "Hello"

    def test_empty_after_strip(self):
        assert _clean_response("<think>only thinking</think>") == ""


class TestLangMap:
    def test_en_mapping(self):
        assert LANG_MAP["en"] == ("en", "English")

    def test_pt_br_mapping(self):
        assert LANG_MAP["pt-br"] == ("pt", "Brazilian Portuguese")


class TestTranslate:
    @patch("kindle.translator.requests.post")
    def test_ollama_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "Hello"}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = translate("こんにちは")
        assert result == "Hello"
        # Verify chat API endpoint is used
        call_url = mock_post.call_args[0][0]
        assert call_url.endswith("/api/chat")

    @patch("kindle.translator.requests.post", side_effect=Exception("timeout"))
    def test_fails_closed_on_ollama_failure(self, mock_post):
        with pytest.raises(Exception, match="timeout"):
            translate("こんにちは")

    @patch("kindle.translator.requests.post")
    def test_fails_closed_on_empty_response(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "<think>hmm</think>"}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        with pytest.raises(RuntimeError, match="empty translation"):
            translate("こんにちは")

    @patch("kindle.translator.requests.post")
    def test_pt_br_prompt_contains_portuguese(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "Olá"}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = translate("こんにちは", target_lang="pt-br")
        assert result == "Olá"
        payload = mock_post.call_args[1]["json"]
        prompt = payload["messages"][0]["content"]
        assert "Brazilian Portuguese" in prompt
