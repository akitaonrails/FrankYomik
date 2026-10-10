"""Contract tests for structured page translation and bounded recovery."""

import json

import pytest

from pipeline.translation import (
    MAX_RESPONSE_CHARS,
    PageTranslationRequest,
    PageTranslationResult,
    TranslationContextLimit,
    TranslationContractError,
    TranslationItem,
    TranslationValue,
    build_page_prompt,
    parse_page_response,
    response_json_schema,
    translate_with_recovery,
)


def _request(count: int = 3) -> PageTranslationRequest:
    return PageTranslationRequest(
        "ja", "pt-br",
        tuple(TranslationItem(f"region-{index}", index, "balloon", f"原文{index}")
              for index in range(count)),
        context=("前のページ",),
        terms=(("先生", "sensei"),),
        style=("Keep it concise.",),
    )


def _response(values: dict[str, str]) -> str:
    return json.dumps({"schema_version": 1, "translations": values})


def test_prompt_contains_stable_order_kind_target_context_terms_and_style():
    prompt = build_page_prompt(_request(2))
    payload = json.loads(prompt.split("REQUEST=", 1)[1])
    assert payload["target_language"] == "pt-br"
    assert payload["target_language_name"] == "Brazilian Portuguese"
    assert payload["context"] == ["前のページ"]
    assert payload["terms"] == [{"source": "先生", "target": "sensei"}]
    assert payload["style"] == ["Keep it concise."]
    assert [(item["id"], item["reading_order"], item["kind"])
            for item in payload["items"]] == [
        ("region-0", 0, "balloon"), ("region-1", 1, "balloon"),
    ]


def test_schema_is_keyed_by_exact_ids():
    translations = response_json_schema(["one", "two"])["properties"]["translations"]
    assert translations["required"] == ["one", "two"]
    assert set(translations["properties"]) == {"one", "two"}
    assert translations["additionalProperties"] is False


@pytest.mark.parametrize("raw, message", [
    ('{"schema_version":1,"translations":{"one":"A","one":"B"}}',
     "duplicate"),
    (_response({"unknown": "A"}), "unknown"),
    ("not-json", "malformed"),
    ('{"schema_version":true,"translations":{"one":"A"}}', "version"),
    (json.dumps({"schema_version": 1, "translations": {"one": ""}}),
     "non-empty"),
])
def test_parser_rejects_duplicate_unknown_malformed_and_empty(raw, message):
    with pytest.raises(TranslationContractError, match=message):
        parse_page_response(raw, ["one"])


def test_parser_bounds_provider_response_before_json_decode():
    with pytest.raises(TranslationContractError, match="too large"):
        parse_page_response(" " * (MAX_RESPONSE_CHARS + 1), ["one"])


def test_reordered_response_is_returned_in_source_order_with_one_call():
    request = _request()
    calls = []

    def provider(batch):
        calls.append(batch)
        return _response({"region-2": "C", "region-0": "A", "region-1": "B"})

    result = translate_with_recovery(request, provider, lambda *_: "unused")
    assert [(value.id, value.text) for value in result.values] == [
        ("region-0", "A"), ("region-1", "B"), ("region-2", "C"),
    ]
    assert len(calls) == 1
    assert result.fallback_ids == ()


def test_missing_item_recovers_only_missing_id():
    request = _request()
    batches = []
    singles = []

    def provider(batch):
        batches.append(tuple(item.id for item in batch.items))
        return _response({"region-0": "A", "region-2": "C"})

    def single(item, target):
        singles.append((item.id, target))
        return "B"

    result = translate_with_recovery(request, provider, single)
    assert result.by_id() == {"region-0": "A", "region-1": "B", "region-2": "C"}
    assert batches == [("region-0", "region-1", "region-2"), ("region-1",)]
    assert singles == [("region-1", "pt-br")]


@pytest.mark.parametrize("failure", [
    TimeoutError("timeout"), TranslationContextLimit("context"),
])
def test_timeout_and_context_limit_split_deterministically(failure):
    request = _request(4)
    calls = []

    def provider(batch):
        ids = tuple(item.id for item in batch.items)
        calls.append(ids)
        if len(ids) > 2:
            raise failure
        return _response({item_id: item_id.upper() for item_id in ids})

    result = translate_with_recovery(request, provider, lambda *_: "unused")
    assert calls == [
        ("region-0", "region-1", "region-2", "region-3"),
        ("region-0", "region-1"), ("region-2", "region-3"),
    ]
    assert len(result.values) == 4


def test_malformed_batches_have_fixed_attempt_bound_then_single_fallback():
    request = _request(4)
    calls = []
    singles = []

    def provider(batch):
        calls.append(tuple(item.id for item in batch.items))
        return "bad"

    def single(item, _target):
        singles.append(item.id)
        return f"single-{item.id}"

    result = translate_with_recovery(
        request, provider, single, max_batch_attempts=2,
    )
    assert calls == [
        ("region-0", "region-1", "region-2", "region-3"),
        ("region-0", "region-1"),
    ]
    assert singles == ["region-0", "region-1", "region-2", "region-3"]
    assert result.fallback_ids == tuple(singles)


def test_unexpected_provider_error_fails_without_fallback():
    singles = []

    def provider(_batch):
        raise PermissionError("auth failed")

    with pytest.raises(PermissionError, match="auth failed"):
        translate_with_recovery(
            _request(), provider,
            lambda item, _target: singles.append(item.id) or "unused",
        )
    assert singles == []


def test_request_rejects_duplicate_ids_and_unbounded_context():
    item = TranslationItem("same", 0, "bubble", "a")
    with pytest.raises(ValueError, match="unique"):
        PageTranslationRequest("ja", "en", (item, item))
    with pytest.raises(ValueError, match="context is too large"):
        PageTranslationRequest(
            "ja", "en", (item,), context=("x" * 2_001, "y" * 2_000),
        )
    with pytest.raises(ValueError, match="1..512"):
        PageTranslationRequest(
            "ja", "en",
            tuple(TranslationItem(f"r{index}", index, "bubble", "a")
                  for index in range(513)),
        )


def test_result_rejects_duplicate_ids_before_application():
    with pytest.raises(ValueError, match="unique"):
        PageTranslationResult((
            TranslationValue("same", "A"), TranslationValue("same", "B"),
        ), batch_attempts=1)
