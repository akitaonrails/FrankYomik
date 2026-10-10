# Page-batched translation

Manga translation and webtoon translation use one structured Ollama request for
the eligible regions on a normal page. Manga and book furigana do not use this
stage. The configured `qwen3:14b` model and its registry digest verification are
unchanged.

## Request contract

The worker assigns each eligible region its final `PageDocument` ID before it
calls the translation stage. It then sorts the request by reading order. The
request contains:

- schema and prompt revision;
- source and target language codes and unambiguous display names;
- stable region ID, reading order, semantic kind, and source text;
- up to four preceding-page context entries and 4,000 context characters;
- up to 64 source-to-target terminology entries;
- up to 16 style rules.

Fields are bounded before an HTTP request is made. Source text and each context,
term, or style value are limited to 2,000 characters. The current API does not
supply preceding-page context or user terminology, so both are empty in
production requests. Manga and webtoon use checked-in style rules. Changing
those rules, the prompt, schema, recovery policy, model identity, or adapter
code changes the translation stage identity and global pipeline cache revision.
Pages may contain up to 512 eligible regions. Requests of more than 64 regions
are split into deterministic 64-region chunks and collected completely before
the worker mutates page state. Normal pages of 64 or fewer regions still use
one translation request and one model verification.

Only OCR-valid regions enter the request. Existing Japanese and Korean
validation remains authoritative, so preserved Latin text such as `Kacchan`,
punctuation-only regions, decorative titles, and OCR noise are not sent for
translation. Webtoon dialogue and eligible Korean SFX share the same page
request. SFX results are uppercased before rendering.
Ineligible SFX persist without a translation result, target language, or
translation provenance.

## Response schema

Ollama receives an exact JSON Schema through its `format` field. A response has
this shape:

```json
{
  "schema_version": 1,
  "translations": {
    "stable-region-id-1": "Translated text",
    "stable-region-id-2": "BOOM"
  }
}
```

The parser preserves JSON key occurrences so it can reject duplicate keys
before a normal JSON object would overwrite them. It also rejects unknown IDs,
extra envelope fields, the wrong schema version, empty values, malformed JSON,
and translation content larger than 256,000 characters. HTTP response envelopes
are streamed with limits of 512,000 bytes and 256,000 decoded characters before
JSON parsing or error inspection. The connection is closed after every attempt,
including size-limit and malformed-response failures. Provider ordering is ignored;
validated results are restored to source reading order by ID. Results are
applied to the page only after every eligible ID has a valid translation.

## Recovery and failures

A normal page uses one provider call. Missing IDs retry only the unresolved
items. Malformed output, duplicate or unknown IDs, timeouts, and explicit HTTP
400/413 context-limit responses split the unresolved items into deterministic
halves. A 5xx response always fails the page, regardless of its body text.
At most eight structured calls are attempted per 64-region chunk. Remaining
single items reuse the already verified legacy dialogue or SFX request
primitive, so fallback does not repeat model verification and successful
recovery cannot drop an eligible region.

Model digest verification failures, authentication failures, other HTTP
errors, non-string provider payloads, and unexpected exceptions fail the page.
They are not treated as recoverable model output. Existing single-region
fallback failures also fail the page.

## Privacy and provenance

Requests go only to the configured `OLLAMA_BASE_URL`; no provider catalog or
external translation fallback is used. The worker persists translated text and
normal stage provenance. It does not persist prompts, secrets, HTTP headers, or
full provider responses.

`PageDocument.stage_provenance.translation` records the composite adapter
revision, the exact Ollama model and weights digest, and a configuration hash
covering prompt and output policy. Individual `TranslationResult` records carry
the same stage provenance and preserve the requested target language.

## Troubleshooting

- A model verification error means the Ollama tag is missing or its registry
  digest differs from the pinned manifest. Install the exact pinned model.
- A context-limit error should recover through smaller batches. Repeated limits
  reach the existing single-region path within the fixed attempt bound.
- Duplicate, unknown, or missing IDs indicate schema drift or noncompliant model
  output. Check the configured model and the translation stage identity.
- A timeout is recoverable only within the bounded translation call. Other
  connectivity and HTTP failures fail the job so cache metadata never claims a
  translation that did not complete.
