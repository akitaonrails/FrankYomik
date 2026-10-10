#!/usr/bin/env python3
"""Exercise the production page-translation contract with a deterministic fake."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common import BASE_DIR, read_json, write_json

SERVER_DIR = BASE_DIR.parents[1] / "server"
TRANSLATION_PATH = SERVER_DIR / "pipeline/translation.py"
SPEC = importlib.util.spec_from_file_location(
    "phase3_production_translation", TRANSLATION_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load production translation contract")
translation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = translation
SPEC.loader.exec_module(translation)

PageTranslationRequest = translation.PageTranslationRequest
TranslationContractError = translation.TranslationContractError
TranslationContextLimit = translation.TranslationContextLimit
TranslationItem = translation.TranslationItem
build_page_prompt = translation.build_page_prompt
parse_page_response = translation.parse_page_response
response_json_schema = translation.response_json_schema
translate_with_recovery = translation.translate_with_recovery

FIXTURE_PATH = BASE_DIR / "fixtures/translation_page.json"
EXPECTED_PROMPT_KEYS = {
    "schema_version",
    "source_language",
    "source_language_name",
    "target_language",
    "target_language_name",
    "context",
    "terms",
    "style",
    "items",
}
EXPECTED_ITEM_KEYS = {"id", "reading_order", "kind", "source"}
PRIVATE_SENTINELS = (
    "PRIVATE_ACCOUNT_SENTINEL",
    "PRIVATE_HASH_SENTINEL",
    "PRIVATE_TOKEN_SENTINEL",
    "PRIVATE_IMAGE_SENTINEL",
)


def make_request(page: dict[str, Any]) -> tuple[PageTranslationRequest, dict[str, str]]:
    translated = [item for item in page["items"] if item["eligible_for_translation"]]
    request = PageTranslationRequest(
        source_language=page["source_language"],
        target_language=page["target_language"],
        items=tuple(
            TranslationItem(
                item["id"], item["reading_order"], item["kind"], item["source_text"]
            )
            for item in translated
        ),
        context=tuple(page["context"]),
        terms=tuple(tuple(term) for term in page["terms"]),
        style=tuple(page["style"]),
    )
    expected = {item["id"]: item["expected"] for item in page["items"]}
    return request, expected


def response(translations: list[tuple[str, str]]) -> str:
    body = {
        "schema_version": 1,
        "translations": dict(translations),
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


@dataclass
class FakeProvider:
    expected: dict[str, str]
    scenario: str
    calls: list[PageTranslationRequest] = field(default_factory=list)

    def __call__(self, request: PageTranslationRequest) -> str:
        self.calls.append(request)
        items = list(request.items)
        if self.scenario == "timeout":
            raise TimeoutError("deterministic timeout")
        if self.scenario == "context_limit":
            raise TranslationContextLimit("deterministic context limit")
        if self.scenario == "malformed":
            return "not-json"
        if self.scenario == "duplicate":
            item = items[0]
            text = self.expected[item.id]
            return (
                '{"schema_version":1,"translations":{"'
                + item.id
                + '":'
                + json.dumps(text, ensure_ascii=False)
                + ',"'
                + item.id
                + '":"duplicate"}}'
            )
        if self.scenario == "unknown":
            return response([("unknown-region", "bad")])
        selected = items[:-1] if self.scenario == "partial" and len(self.calls) == 1 else items
        return response(
            [(item.id, self.expected[item.id]) for item in reversed(selected)]
        )


@dataclass
class SingleFallback:
    expected: dict[str, str]
    calls: list[str] = field(default_factory=list)

    def __call__(self, item: TranslationItem, _target_language: str) -> str:
        self.calls.append(item.id)
        return self.expected[item.id]


def merge_ineligible(
    page: dict[str, Any], translated: dict[str, str]
) -> dict[str, str]:
    return {
        item["id"]: (
            item["source_text"]
            if not item["eligible_for_translation"]
            else translated[item["id"]]
        )
        for item in sorted(page["items"], key=lambda value: value["reading_order"])
    }


def assert_prompt_privacy(request: PageTranslationRequest) -> None:
    prompt = build_page_prompt(request)
    if any(sentinel in prompt for sentinel in PRIVATE_SENTINELS):
        raise AssertionError("private fixture metadata escaped into provider prompt")
    payload = json.loads(prompt.split("REQUEST=", 1)[1])
    if set(payload) != EXPECTED_PROMPT_KEYS:
        raise AssertionError("production prompt fields changed")
    if any(set(item) != EXPECTED_ITEM_KEYS for item in payload["items"]):
        raise AssertionError("production prompt item fields changed")


def parser_rejections(expected_ids: list[str]) -> dict[str, str]:
    duplicate_id = expected_ids[0]
    duplicate = (
        '{"schema_version":1,"translations":{"'
        + duplicate_id
        + '":"one","'
        + duplicate_id
        + '":"two"}}'
    )
    candidates = {
        "malformed": "not-json",
        "duplicate": duplicate,
        "unknown": response([("unknown-region", "bad")]),
    }
    rejected = {}
    for name, raw in candidates.items():
        try:
            parse_page_response(raw, expected_ids)
        except TranslationContractError as error:
            rejected[name] = str(error)
        else:
            raise AssertionError(f"production parser accepted {name} response")
    return rejected


def evaluate(page: dict[str, Any]) -> dict[str, Any]:
    request, expected = make_request(page)
    expected_ids = [item.id for item in request.items]
    assert_prompt_privacy(request)
    schema = response_json_schema(expected_ids)
    assert schema["properties"]["translations"]["required"] == expected_ids

    valid_provider = FakeProvider(expected, "valid")
    valid_fallback = SingleFallback(expected)
    valid = translate_with_recovery(request, valid_provider, valid_fallback)
    valid_result = merge_ineligible(page, valid.by_id())
    assert valid_result == expected
    assert list(valid.by_id()) == expected_ids
    assert len(valid_provider.calls) == 1
    assert not valid_fallback.calls

    partial_provider = FakeProvider(expected, "partial")
    partial_fallback = SingleFallback(expected)
    partial = translate_with_recovery(request, partial_provider, partial_fallback)
    assert merge_ineligible(page, partial.by_id()) == expected
    assert len(partial_provider.calls) == 2
    assert not partial_fallback.calls

    recovery = {}
    for scenario in (
        "malformed",
        "duplicate",
        "unknown",
        "timeout",
        "context_limit",
    ):
        provider = FakeProvider(expected, scenario)
        fallback = SingleFallback(expected)
        result = translate_with_recovery(request, provider, fallback)
        assert merge_ineligible(page, result.by_id()) == expected
        if result.batch_attempts > 8:
            raise AssertionError("production recovery exceeded its attempt bound")
        recovery[scenario] = {
            "batch_attempts": result.batch_attempts,
            "fallback_ids": list(result.fallback_ids),
        }

    translated_count = len(request.items)
    return {
        "schema_version": 2,
        "fixture": str(FIXTURE_PATH.relative_to(BASE_DIR)),
        "production_module": "server/pipeline/translation.py",
        "segments": len(page["items"]),
        "translated_segments": translated_count,
        "ineligible_preserved_segments": len(page["items"]) - translated_count,
        "legacy_translation_post_calls": translated_count,
        "normal_page_batch_calls": len(valid_provider.calls),
        "normal_page_batch_attempts": valid.batch_attempts,
        "partial_response_batch_calls": len(partial_provider.calls),
        "partial_response_fallback_ids": list(partial.fallback_ids),
        "source_order_preserved_from_reversed_response": True,
        "ineligible_regions_preserved": True,
        "parser_rejections": parser_rejections(expected_ids),
        "bounded_recovery": recovery,
        "privacy_allowlist_passed": True,
        "subjective_model_quality_measured": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    parser.add_argument(
        "--output",
        type=Path,
        default=BASE_DIR / "outputs/phase3-page-translation.json",
    )
    args = parser.parse_args()
    report = evaluate(read_json(args.fixture))
    write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
