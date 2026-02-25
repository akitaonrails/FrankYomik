"""Unit tests for webtoon Korean translator (mocked Ollama)."""

from unittest.mock import MagicMock, patch

from webtoon.translator import translate, _fallback_translate


class TestTranslate:
    @patch("webtoon.translator.requests.post")
    def test_ollama_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "Hello"}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = translate("안녕하세요")
        assert result == "Hello"
        # Verify chat API endpoint is used
        call_url = mock_post.call_args[0][0]
        assert call_url.endswith("/api/chat")

    @patch("webtoon.translator.requests.post")
    def test_prompt_contains_korean_context(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "Hello"}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        translate("안녕하세요")
        payload = mock_post.call_args[1]["json"]
        prompt = payload["messages"][0]["content"]
        assert "Korean" in prompt
        assert "manhwa" in prompt or "webtoon" in prompt

    @patch("webtoon.translator._fallback_translate", return_value="Fallback")
    @patch("webtoon.translator.requests.post", side_effect=Exception("timeout"))
    def test_fallback_on_failure(self, mock_post, mock_fallback):
        result = translate("안녕하세요")
        assert result == "Fallback"
        mock_fallback.assert_called_once_with("안녕하세요")

    @patch("webtoon.translator._fallback_translate", return_value="Fallback")
    @patch("webtoon.translator.requests.post")
    def test_fallback_on_empty_response(self, mock_post, mock_fallback):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "<think>hmm</think>"}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = translate("안녕하세요")
        assert result == "Fallback"
