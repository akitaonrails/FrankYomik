"""Unit tests for translator module (mocked Ollama)."""

from unittest.mock import patch, MagicMock

from pipeline.translator import translate, _clean_response


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


class TestTranslate:
    @patch("pipeline.translator.requests.post")
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

    @patch("pipeline.translator._fallback_translate", return_value="Fallback")
    @patch("pipeline.translator.requests.post", side_effect=Exception("timeout"))
    def test_fallback_on_failure(self, mock_post, mock_fallback):
        result = translate("こんにちは")
        assert result == "Fallback"
        mock_fallback.assert_called_once_with("こんにちは")

    @patch("pipeline.translator._fallback_translate", return_value="Fallback")
    @patch("pipeline.translator.requests.post")
    def test_fallback_on_empty_response(self, mock_post, mock_fallback):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "<think>hmm</think>"}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = translate("こんにちは")
        assert result == "Fallback"
