"""Unit tests for webtoon Korean translator (mocked Ollama)."""

from unittest.mock import MagicMock, patch

from webtoon.translator import translate, translate_sfx, _fallback_translate


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
        mock_fallback.assert_called_once_with("안녕하세요", "en")

    @patch("webtoon.translator._fallback_translate", return_value="Fallback")
    @patch("webtoon.translator.requests.post")
    def test_fallback_on_empty_response(self, mock_post, mock_fallback):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "<think>hmm</think>"}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = translate("안녕하세요")
        assert result == "Fallback"

    @patch("webtoon.translator.requests.post")
    def test_pt_br_prompt_contains_portuguese(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "Olá"}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = translate("안녕하세요", target_lang="pt-br")
        assert result == "Olá"
        payload = mock_post.call_args[1]["json"]
        prompt = payload["messages"][0]["content"]
        assert "Brazilian Portuguese" in prompt

    @patch("webtoon.translator._fallback_translate", return_value="Fallback")
    @patch("webtoon.translator.requests.post", side_effect=Exception("timeout"))
    def test_pt_br_fallback_passes_target_lang(self, mock_post, mock_fallback):
        translate("안녕하세요", target_lang="pt-br")
        mock_fallback.assert_called_once_with("안녕하세요", "pt-br")


class TestTranslateSfx:
    """SFX translation uses a specialized prompt for onomatopoeia."""

    @patch("webtoon.translator.requests.post")
    def test_sfx_prompt_contains_sound_effect(self, mock_post):
        """Prompt must mention sound effect for proper SFX translation."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "CRASH"}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        translate_sfx("꽈양")
        payload = mock_post.call_args[1]["json"]
        prompt = payload["messages"][0]["content"]
        assert "sound effect" in prompt.lower()

    @patch("webtoon.translator.requests.post")
    def test_sfx_returns_uppercase(self, mock_post):
        """SFX result is always uppercased."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "crash"}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = translate_sfx("꽈양")
        assert result == "CRASH"
        assert result == result.upper()

    @patch("webtoon.translator.requests.post")
    def test_sfx_strips_prefix(self, mock_post):
        """LLM sometimes adds 'SFX:' prefix — must be stripped."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "SFX: SHUSH"}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = translate_sfx("셈")
        assert result == "SHUSH"

    @patch("webtoon.translator._fallback_translate_sfx", return_value="BOOM")
    @patch("webtoon.translator.requests.post", side_effect=Exception("timeout"))
    def test_sfx_fallback_on_failure(self, mock_post, mock_fallback):
        """Falls back to Google Translate on Ollama failure."""
        result = translate_sfx("쾅")
        assert result == "BOOM"
        mock_fallback.assert_called_once_with("쾅", "en")

    @patch("webtoon.translator.requests.post")
    def test_sfx_pt_br_prompt(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "ESTRONDO"}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = translate_sfx("쾅", target_lang="pt-br")
        assert result == "ESTRONDO"
        payload = mock_post.call_args[1]["json"]
        prompt = payload["messages"][0]["content"]
        assert "Brazilian Portuguese" in prompt
