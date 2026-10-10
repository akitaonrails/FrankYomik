# Phase 3 page-translation evaluation

## Decision

Replace independent per-region translation with one validated page request for
normal pages. Keep the existing pinned Ollama model and target-language API.
The pre-Phase 3 implementation preserved region order and failed closed, but it
could not use neighboring dialogue to resolve names, pronouns, omitted subjects,
or continuity. It also repeated model verification and translation requests for
every eligible region. The production page contract now supplies that context
and verifies the model once for the full page operation, including fallback.

The deterministic contract gate passes. Subjective Qwen translation quality was
not rescored because the current `temperature: 0.3` endpoint does not provide a
stable frozen comparison. The curated fixture proves that the request supplies
the information needed for contextual translation and that response handling is
safe; it does not claim that a fake provider measures prose quality.

## Pinned current baseline

The pre-Phase 3 baseline at commit `6bb78f9` covers these tracked
implementations:

| Component | SHA-256 |
|---|---|
| `server/kindle/translator.py` | `f082a60c17e91b7334ad1fd25c5f7a1cf17f302691325fbe1fabb5c26944840d` |
| `server/webtoon/translator.py` | `055e123774a5d6789cb657de488c14325e4fb76800e4070416012a77c40f5562` |
| `server/worker/job.py` | `4633061f0a6e430ad1d3d0f7e0dd332af5cb16549be74f2fa63292cf0782bee5` |
| `server/webtoon/processor.py` | `6e8416ec1531278fb9e2ae2329dd7e57aba706421c47e0fe01fe3f740bf836e7` |

The stage manifest pins `qwen3:14b` at Ollama digest
`bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8`,
with temperature 0.3, `num_predict` 1024, and thinking disabled.

### Pre-Phase 3 manga behavior

- Every valid OCR region makes its own `/api/tags` verification and `/api/chat`
  request. Requests run concurrently with at most eight worker threads.
- The prompt asks for natural concise dialogue, tone preservation, unchanged
  Japanese names, nicknames, and honorific-based names.
- A request contains one source string. It has no stable region ID, neighboring
  text, reading order, semantic kind, terminology map, or prior-page context.
- Manga SFX and artwork text use the dialogue prompt. There is no manga SFX
  translation policy.
- Completed futures write back to their original region objects, so asynchronous
  completion does not reorder the stored page regions.
- HTTP, model-verification, malformed JSON, and empty-output errors propagate.
  One failed region fails the page job; completed sibling responses are not
  saved as a partial page.
- A plain-string response has no IDs, so duplicate and unknown region responses
  cannot be detected. There is no partial structured response to recover.

### Pre-Phase 3 webtoon behavior

Dialogue regions also use one request per region, concurrently. SFX requests
run separately and sequentially with a dedicated prompt that uppercases the
result. Invalid Korean and non-Hangul SFX are skipped. A dialogue or SFX failure
propagates to the page job.

### Privacy boundary

The current Ollama request sends the source text and translation instructions;
it does not send the page image, geometry, account ID, source hash, cache data,
or provider credentials. The configured Ollama URL can point to another host,
so the deployment boundary determines where dialogue leaves the worker.

Current worker logs can contain source OCR and translated dialogue at info
level. The page contract adds no secrets, account/job metadata, images, OCR
alternatives, or raw provider responses to its payload or logs. Only fields
required for translation cross the provider boundary.

## Deterministic page fixture

The tracked fixture is
[`fixtures/translation_page.json`](fixtures/translation_page.json), SHA-256
`7f1e7283d93e5d174dfdf5620404e768f0dfcf243b11295b87f6c01e368f5e00`.
It contains eight ordered regions:

- four connected dialogue turns covering the name `Haruka`, pronouns, an
  omitted object, and dialogue continuity;
- one SFX and one sign requiring semantic-kind-specific treatment;
- one target-script nickname and one ellipsis that remain outside translation
  through the existing OCR eligibility boundary;
- private sentinel metadata that must never enter a provider request.

Expected translations are curated assertions for the fake provider. They make
the contract test readable and ensure ID mapping, policies, and ordering remain
observable. They are not reference translations for comparing language models.
The evaluator imports `server/pipeline/translation.py` directly and exercises
its real `PageTranslationRequest`, prompt builder, response schema, parser, and
bounded-recovery function. It does not maintain a parallel contract.

Run the evaluation from the repository root:

```bash
python evaluation/koharu_baseline/scripts/evaluate_page_translation.py
```

The script writes its ignored report to
`evaluation/koharu_baseline/outputs/phase3-page-translation.json`.

## Results

| Check | Result |
|---|---:|
| Total regions | 8 |
| Provider-translated regions | 6 |
| Ineligible regions preserved without provider calls | 2 |
| Current per-region translation POSTs for equivalent eligible work | 6 |
| Normal page-batch translation calls | **1** |
| Partial batch with one missing ID | 2 calls including one bounded fallback |
| Reversed provider output restored to source order | Pass |
| Malformed response rejected by parser | Pass |
| Duplicate ID rejected by parser | Pass |
| Unknown ID rejected by parser | Pass |
| Malformed/duplicate/unknown recovery | 8 bounded batch attempts, then six single-item fallbacks |
| Timeout/context-limit recovery | 8 bounded batch attempts, then six single-item fallbacks |
| Existing eligibility boundary preserves untranslated items | Pass |
| Private-field payload allowlist | Pass |

The production provider prompt contains only schema version, source and target
language codes and display names, context, terms, style, and ordered items with
stable ID, reading order, semantic kind, and source text. The response is a
keyed object and may arrive in any order. The validator maps by ID and emits
results in source order.

A syntactically valid partial response retains its valid items and retries only
the missing IDs as a smaller structured batch. Malformed structure, duplicate
IDs, unknown IDs, empty text, and extra response fields are parser errors. The
production recovery policy splits errored batches and eventually invokes the
existing single-item primitive after its eight-attempt bound; it never attaches
text to a guessed region. Provider timeout, context-limit, streamed response
limits on HTTP 400/413, connection cleanup, and single-verification fallback
have adapter-level production tests. A regression test proves that HTTP 500
remains an unexpected failure even when its body contains `object too large`.
The evaluation fake does not model transport timing or tokenization.

The fixture supplies eligibility as the upstream OCR result: its two ineligible
items never enter `PageTranslationRequest` and their source is retained. Worker
integration coverage proves that `Kacchan`, an ellipsis, and ineligible webtoon
SFX remain outside the provider request and persist without translation
provenance. The evaluation script separately verifies the contract fixture's
eligibility allowlist.

## Verified production contract

1. Sort eligible regions by stored reading order and submit one
   `PageTranslationRequest` containing stable string IDs, semantic kinds,
   source text, target language, context, `terms`, and `style`.
2. Preserve the existing OCR eligibility boundary: invalid source-language
   regions such as target-script nicknames and punctuation-only ellipses must
   not become batch items and must remain untouched. Do not add a second
   preserve marker to the provider schema.
3. Instruct the model to use adjacent segments for disambiguation without
   merging or splitting them. Give SFX and signs explicit kind-specific rules.
4. Accept only the versioned JSON envelope with a `translations` object keyed
   by submitted IDs and non-empty string values. Reject duplicate JSON keys,
   unknown IDs, malformed envelopes, and invalid values before mutation.
5. Preserve valid partial items and retry missing IDs with bounded smaller
   batches, then the existing single-region adapter. Apply no page mutation
   until the complete result has passed validation or recovery.
6. Preserve input order regardless of response order. Store selected text and
   prompt/schema/model provenance, without raw provider responses.
7. Verify the pinned model once per page operation. Keep the current fail-closed
   digest check and target-language behavior.
8. Use an explicit payload allowlist. Do not send page images or private
   metadata unless a separately documented vision mode is intentionally enabled.

## Promotion gates

- Deterministic production tests reproduce every fake scenario in this
  evaluation, plus timeout, context-limit chunking, and fallback exhaustion.
- A normal page uses one model-verification request and one translation request.
- Missing-item fallback never loses already validated translations and never
  exceeds its configured request bound.
- The fixture's names, terminology, semantic kinds, preservation rules, and
  reading order appear in the actual provider payload.
- Full manga and webtoon worker integration tests remain green, including SFX,
  cache provenance, target languages, and rerender behavior.
- A later frozen-model review may measure language quality, but page batching is
  justified now by the verified context and response-integrity improvements.

## Verification performed

```bash
python -m json.tool \
  evaluation/koharu_baseline/fixtures/translation_page.json
uvx ruff check \
  evaluation/koharu_baseline/scripts/evaluate_page_translation.py
python -m py_compile \
  evaluation/koharu_baseline/scripts/evaluate_page_translation.py
PYTHONPATH=server server/.venv/bin/python -m pytest -q \
  server/tests/unit/test_pipeline_translation.py \
  server/tests/unit/test_translator.py \
  server/tests/unit/test_webtoon_translator.py \
  server/tests/unit/test_webtoon_processor.py \
  server/tests/integration/test_worker_pipeline_contract.py
```

The deterministic evaluator passed and wrote schema version 2 with one normal
page call, six translated regions, and two preserved regions. The focused suite
passed 131 tests. Verification invokes pytest through the environment's Python
executable so it does not depend on the launcher shebang.
