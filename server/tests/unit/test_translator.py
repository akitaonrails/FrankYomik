"""Unit tests for translator module (mocked Ollama)."""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from kindle.translator import (
    MAX_HTTP_RESPONSE_BYTES,
    LANG_MAP,
    _clean_response,
    translate,
    translate_page,
)
from pipeline.translation import (
    PageTranslationRequest,
    TranslationContractError,
    TranslationItem,
)


def _chat_response(content: str, *, status_code: int = 200) -> MagicMock:
    response = MagicMock(status_code=status_code)
    body = json.dumps({"message": {"content": content}}).encode()
    response.iter_content.return_value = [body]
    return response


def _error_response(status_code: int, body: str) -> MagicMock:
    response = MagicMock(status_code=status_code)
    response.iter_content.return_value = [body.encode()]
    response.raise_for_status.side_effect = requests.HTTPError(
        f"{status_code} provider error",
    )
    return response


@pytest.fixture(autouse=True)
def verified_model():
    with patch("kindle.translator.verify_ollama_model") as verify:
        yield verify


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
        mock_resp = _chat_response("Hello")
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
        mock_resp = _chat_response("<think>hmm</think>")
        mock_post.return_value = mock_resp

        with pytest.raises(RuntimeError, match="empty translation"):
            translate("こんにちは")

    @patch("kindle.translator.requests.post")
    def test_pt_br_prompt_contains_portuguese(self, mock_post):
        mock_resp = _chat_response("Olá")
        mock_post.return_value = mock_resp

        result = translate("こんにちは", target_lang="pt-br")
        assert result == "Olá"
        payload = mock_post.call_args[1]["json"]
        prompt = payload["messages"][0]["content"]
        assert "Brazilian Portuguese" in prompt


class TestTranslatePage:
    @patch("kindle.translator.requests.post")
    def test_one_structured_request_for_normal_page(self, mock_post):
        mock_resp = _chat_response(
            '{"schema_version":1,"translations":{"r2":"B","r1":"A"}}',
        )
        mock_post.return_value = mock_resp
        request = PageTranslationRequest(
            "ja", "pt-br",
            (
                TranslationItem("r1", 0, "balloon", "一"),
                TranslationItem("r2", 1, "artwork_text", "二"),
            ),
        )

        result = translate_page(request)

        assert result.by_id() == {"r1": "A", "r2": "B"}
        assert mock_post.call_count == 1
        payload = mock_post.call_args.kwargs["json"]
        assert mock_post.call_args.kwargs["stream"] is True
        assert payload["format"]["properties"]["translations"]["required"] == [
            "r1", "r2",
        ]
        assert "Brazilian Portuguese" in payload["messages"][0]["content"]
        assert '"target_language":"pt-br"' in payload["messages"][0]["content"]

    @patch("kindle.translator.requests.post")
    def test_outer_envelope_is_streamed_and_bounded_before_error_access(
        self, mock_post,
    ):
        class OversizedResponse:
            status_code = 500

            def __init__(self):
                self.closed = False

            def iter_content(self, chunk_size):
                assert chunk_size == 8_192
                yield b"x" * (MAX_HTTP_RESPONSE_BYTES + 1)

            @property
            def text(self):
                raise AssertionError("unbounded text access")

            def json(self):
                raise AssertionError("unbounded json access")

            def raise_for_status(self):
                raise AssertionError("body cap must run before HTTP error")

            def close(self):
                self.closed = True

        response = OversizedResponse()
        mock_post.return_value = response
        request = PageTranslationRequest(
            "ja", "en", (TranslationItem("r1", 0, "balloon", "一"),),
        )

        with pytest.raises(TranslationContractError, match="body is too large"):
            translate_page(request)
        assert response.closed is True

    @patch("kindle.translator.requests.post")
    def test_fallback_reuses_single_model_verification(
        self, mock_post, verified_model,
    ):
        mock_post.side_effect = [
            _chat_response("not-json"),
            _chat_response("Fallback"),
        ]
        request = PageTranslationRequest(
            "ja", "en", (TranslationItem("r1", 0, "balloon", "一"),),
        )

        result = translate_page(request)

        assert result.by_id() == {"r1": "Fallback"}
        assert result.fallback_ids == ("r1",)
        verified_model.assert_called_once_with("manga_translate")
        assert mock_post.call_count == 2

    @patch("kindle.translator.requests.post")
    def test_unexpected_500_too_large_text_fails_without_fallback(
        self, mock_post, verified_model,
    ):
        response = _error_response(500, "object too large")
        mock_post.return_value = response
        request = PageTranslationRequest(
            "ja", "en", (TranslationItem("r1", 0, "balloon", "一"),),
        )

        with pytest.raises(requests.HTTPError, match="500 provider error"):
            translate_page(request)

        verified_model.assert_called_once_with("manga_translate")
        assert mock_post.call_count == 1
        response.close.assert_called_once_with()

    @patch("kindle.translator.requests.post")
    def test_requests_timeout_uses_bounded_single_fallback(
        self, mock_post, verified_model,
    ):
        mock_post.side_effect = [
            requests.Timeout("read timeout"),
            _chat_response("Fallback"),
        ]
        request = PageTranslationRequest(
            "ja", "en", (TranslationItem("r1", 0, "balloon", "一"),),
        )

        result = translate_page(request)

        assert result.by_id() == {"r1": "Fallback"}
        assert result.batch_attempts == 1
        assert result.fallback_ids == ("r1",)
        verified_model.assert_called_once_with("manga_translate")
        assert mock_post.call_count == 2

    @pytest.mark.parametrize("status_code", [400, 413])
    @patch("kindle.translator.requests.post")
    def test_http_context_limit_uses_bounded_single_fallback(
        self, mock_post, status_code, verified_model,
    ):
        context_response = _error_response(
            status_code, "model context length is too large",
        )
        mock_post.side_effect = [
            context_response,
            _chat_response("Fallback"),
        ]
        request = PageTranslationRequest(
            "ja", "en", (TranslationItem("r1", 0, "balloon", "一"),),
        )

        result = translate_page(request)

        assert result.by_id() == {"r1": "Fallback"}
        assert result.batch_attempts == 1
        assert result.fallback_ids == ("r1",)
        verified_model.assert_called_once_with("manga_translate")
        assert mock_post.call_count == 2
        context_response.close.assert_called_once_with()

    @patch("kindle.translator.requests.post")
    def test_oversized_page_splits_into_fixed_size_requests(
        self, mock_post, verified_model,
    ):
        def respond(*_args, **kwargs):
            required = kwargs["json"]["format"]["properties"]["translations"][
                "required"
            ]
            return _chat_response(json.dumps({
                "schema_version": 1,
                "translations": {item_id: f"T:{item_id}" for item_id in required},
            }))

        mock_post.side_effect = respond
        request = PageTranslationRequest(
            "ja", "en",
            tuple(TranslationItem(f"r{index}", index, "balloon", "一")
                  for index in range(65)),
        )

        result = translate_page(request)

        assert len(result.values) == 65
        assert [
            len(call.kwargs["json"]["format"]["properties"]["translations"][
                "required"
            ])
            for call in mock_post.call_args_list
        ] == [64, 1]
        verified_model.assert_called_once_with("manga_translate")
