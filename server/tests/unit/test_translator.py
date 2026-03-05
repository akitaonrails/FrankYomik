"""Unit tests for translator module (mocked Ollama)."""

from unittest.mock import patch, MagicMock

from kindle.translator import translate, _clean_response, LANG_MAP


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

    @patch("kindle.translator._fallback_translate", return_value="Fallback")
    @patch("kindle.translator.requests.post", side_effect=Exception("timeout"))
    def test_fallback_on_failure(self, mock_post, mock_fallback):
        result = translate("こんにちは")
        assert result == "Fallback"
        mock_fallback.assert_called_once_with("こんにちは", "en")

    @patch("kindle.translator._fallback_translate", return_value="Fallback")
    @patch("kindle.translator.requests.post")
    def test_fallback_on_empty_response(self, mock_post, mock_fallback):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "<think>hmm</think>"}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = translate("こんにちは")
        assert result == "Fallback"

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

    @patch("kindle.translator._fallback_translate", return_value="Fallback")
    @patch("kindle.translator.requests.post", side_effect=Exception("timeout"))
    def test_pt_br_fallback_passes_target_lang(self, mock_post, mock_fallback):
        translate("こんにちは", target_lang="pt-br")
        mock_fallback.assert_called_once_with("こんにちは", "pt-br")

    @patch("deep_translator.GoogleTranslator")
    def test_fallback_uses_google_translate_pt(self, mock_gt_cls):
        from kindle.translator import _fallback_translate
        mock_gt_cls.return_value.translate.return_value = "Olá"
        result = _fallback_translate("こんにちは", "pt-br")
        assert result == "Olá"
        mock_gt_cls.assert_called_once_with(source="ja", target="pt")
