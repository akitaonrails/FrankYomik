# Phase 5 rendering and reader evaluation

Audit date: October 10, 2026

## Decision

Keep Frank's specialized manga, webtoon, book, and magnifier behavior. Adopt
Koharu's useful renderer properties as narrow contracts: content-addressed
render inputs, explicit overflow evidence, and glyph coverage checks. Do not
import Koharu's studio renderer wholesale.

Frank already has strong product-specific behavior that Koharu does not replace:

- manga dialogue is fitted page-wide, clipped to the detected balloon mask,
  and long Latin words are split with a visible hyphen;
- webtoon dialogue preserves local contrast and sampled text colors and clips
  both background and glyphs to a usable bubble mask;
- book furigana is added to the correct gutter without redrawing the
  publisher's base text or overwriting existing ruby; and
- the reader keeps the original page visible and reveals the translated
  raster through a bounded 1.5x, 2x, or 3x lens.

The current target languages are English and Brazilian Portuguese. The shipped
OFL dialogue font covers both. Koharu's bidirectional layout, grapheme-safe font
fallback, and vertical typesetting are real advantages, but they do not improve
the current Latin translation targets. Reconsider those parts when another
target script is added.

## Fixture inventory

The server has two committed canonical PageDocument JSON fixtures:
`server/tests/fixtures/page_document_v2_canonical.json` and
`page_document_v2_normal_form.json`. Renderer unit tests construct known page
geometry in memory for manga, webtoon, and book cases. There was no committed
page-level raster golden, so Phase 5 uses small generated geometry and portable
pixel-containment assertions instead of accepting a host-specific PNG hash.

The extension has two committed 2200 by 1257 browser fixtures:
`extension/test/browser/fixtures/page.png` and `render-of-page.png`, with
SHA-256 values
`6d40d617bed7ab528d4e83f2ca4bb9bcdd4e93d07f27cae4d99c9bc1c1c91983`
and `f4abd163ca81f886ce7a8b24babadee0ad9b4db568eb2b1af49eb7134f11262d`.
These are the real-browser signature and lens fixtures. The Flutter client has
no raster fixture; `client/test/js/lens_module.test.mjs` supplies its DOM image
geometry and the Dart suite executes that Node test.

## Reproducible structural gate

The Phase 5 evaluator calls the production PageDocument rerender boundary for
manga and webtoon and the unchanged production book renderer. Its frozen
synthetic cases check:

1. output dimensions, RGB mode, and PNG decoding;
2. changed-pixel containment in the region and stored mask;
3. deterministic rerender from the same document and artifacts;
4. reading-order application when document order differs from render order;
5. a manual translation edit changing only the selected render; and
6. no detector, OCR, translation, transform, or cleanup call during metadata
   rerender (all forbidden stage seams fail the run if invoked); and
7. forced manga, webtoon, and furigana overflow outcomes, including furigana
   changed-pixel containment within its PageDocument regions.

Run it after the production stage manifest is internally consistent:

```bash
cd server
.venv/bin/python ../evaluation/koharu_baseline/scripts/evaluate_phase5_rendering.py
```

For a visual review bundle, add an ignored output directory:

```bash
cd server
.venv/bin/python ../evaluation/koharu_baseline/scripts/evaluate_phase5_rendering.py \
  --visual-output-dir ../evaluation/koharu_baseline/outputs/phase5-rendering
```

Review each source/render pair at 100% and through the 3x lens. Accept when
dialogue has a visible margin, no glyph crosses the region, long words retain
their hyphen, book ruby aligns with its source span, the base book column is
pixel-identical, and the edited image changes only its selected region. The
bundle contains manga and webtoon source/render/edited triplets plus the book
source/render pair. These images are diagnostic outputs; the machine gate
below remains the repeatable decision criterion.

The evaluator reports pixel hashes as same-environment observations. It does
not freeze those hashes across Pillow, FreeType, or HarfBuzz versions because
font rasterization can change without a structural regression. Containment,
dimensions, mode, order, stage isolation, and repeat equality are the portable
golden assertions.

At final pipeline revision
`sha256:002121bf406cd1b3a8803c062d75389574f655c4ca59cca0d210705c96c1415b`,
two consecutive runs produced byte-identical JSON with SHA-256
`7f423cfeff654e71ad320c539056ae362416c58a274fd085d4a12f4a869651f6`.
Their identical stderr contains only the expected `overflow` and `ellipsized`
observations and has SHA-256
`16e000416711ab7450e0276d5b09f964c3ff0b19077479a91c8f5eed80e79c56`.
Manga and webtoon each rendered two regions in semantic
reading order, changed zero pixels outside the region union, invoked zero
forbidden upstream stages, and confined a manual edit to its selected region.
The manga edit changed 898 pixels and the webtoon edit changed 1,914 pixels,
with zero changes outside the selected region. The book case changed 155
gutter pixels, preserved every base-column pixel, and reproduced the same
render. Every output decoded as RGB PNG at its expected dimensions. The forced
manga case persisted `overflow`, 10 px, 70 lines, and `ellipsized: false`. The
forced webtoon case persisted `ellipsized`, 8 px, six retained lines, and
`ellipsized: true`. The forced furigana PageDocument case persisted
`overflow`, 10 px, four columns, and `ellipsized: false`; it changed 6,467
pixels and zero outside its declared regions. All three forced outcomes invoked
zero detector, OCR, translation, or cleanup stages.

Existing integration coverage remains part of the gate:

```bash
cd server
.venv/bin/python -m pytest -q \
  tests/integration/test_worker_pipeline_contract.py \
  tests/unit/test_text_renderer.py \
  tests/unit/test_webtoon_processor.py \
  tests/unit/test_book_layout.py \
  tests/unit/test_book_processor.py \
  tests/unit/test_book_renderer.py \
  tests/unit/test_book_pipeline.py \
  tests/unit/test_pipeline_document.py
```

This focused suite passed **262 tests** at the final Phase 5 revision. The two
warnings are Python's existing `fork()` deprecation warning from the supervised
cleanup test; no renderer assertion was skipped or failed.
`test_furigana_render_is_contained_and_reports_actual_layout` directly checks
the production renderer's transactional restoration outside its declared box
and exact overflow outcome.
`test_artwork_text_uses_cleanup_mask_and_specialized_document_renderer`
specifically verifies that a semantic `text_free` manga detection becomes an
`artwork_text` region, carries its cleanup mask, and uses the specialized
artwork renderer with the cleaned surface.

`test_worker_message_cache_and_rerender_contract` exercises every pipeline
through the worker/cache boundary. For editable manga and webtoon pages it
modifies cached metadata and proves that layout, detection, OCR, translation,
transform, and cleanup call counts do not increase. It also asserts the stable
semantic reading order. Book pages deliberately cannot replay edits from
metadata because their ruby is anchored to OCR geometry and publisher layout.

## Client and browser gate

The Flutter/WebView client gate is:

```bash
cd client
flutter test
```

It passed all **82 tests** on October 10, 2026. The suite covers the Dart-to-JS
registration contract, untouched reader images, page retargeting, the three
documented zoom levels, gesture ownership, pending translations, and bounded
page retention. The embedded Node DOM suite is included in that count.

The Chromium extension gate is:

```bash
cd extension
npm test
```

It passed **134 unit tests and 14 real-Chromium tests** on October 10, 2026.
The browser tests use trusted DevTools input and the committed
`test/browser/fixtures/page.png` and `render-of-page.png`. They verify that a
held peek cannot drag or turn the reader, a tap still turns it, the lens does
not intercept the pointer, and the 16 by 16 image signature accepts the page's
own render while refusing a different page.

The production lens geometry can be observed directly:

```bash
node evaluation/koharu_baseline/scripts/evaluate_phase5_lens.mjs
```

On Chromium's 1000 by 657 CSS-pixel viewport, a 700 by 400 fixture held at its
center produced the following actual styles. The lens stayed 276 pixels wide,
used `pointer-events: none`, and had z-index 2147483646 in every case.

| Zoom | Background size | Background position | Lens top-left |
| ---: | --- | --- | --- |
| 1.5x | 1050 x 600 px | -387, -162 px | 212, 62 px |
| 2x | 1400 x 800 px | -562, -262 px | 212, 62 px |
| 3x | 2100 x 1200 px | -912, -462 px | 212, 62 px |

These are computed by the production `extension/src/content/lens.js` after a
real held mouse gesture. The output is JSON and contains no timing fields.

## Font provenance and coverage

Run the font gate with:

```bash
server/.venv/bin/python \
  evaluation/koharu_baseline/scripts/evaluate_phase5_fonts.py
```

It hashes the shipped fonts, obtains coverage from Fontconfig, and fits four
frozen Latin dialogue cases through Frank's production word wrapper. Comic
Neue regular and bold fit the short, Portuguese, long dialogue, and unbroken
word cases without overflow. The unbroken 29-character Portuguese word is
hyphenated into three lines at 30 px. Both faces contain all tested ASCII and
Brazilian Portuguese characters.

| Role | File SHA-256 | Coverage relevant to Frank | License |
| --- | --- | --- | --- |
| Dialogue regular | `3ff9a69e2abcfeea638657de2cb497f3976c4be8508c31c053561a8bd7cc68f9` | English and Portuguese | SIL OFL 1.1 |
| Dialogue bold | `1cd63fb0576e42fbc8e76b95cc056c34df6f0cdff16572451f64bebbc52b415a` | English and Portuguese | SIL OFL 1.1 |
| GenEi Antique Pv5 | `eac60c8a4d9e38a0cc573ebdbd47b2416183626d708cdbbb74ce25148559f4fd` | Japanese plus Latin | SIL OFL 1.1 |
| Shippori Antique | `0c14dde46778f503412feb66c5f7c60c3628e098c6e277f3354f67b7e9bdc61d` | Japanese plus Latin | SIL OFL 1.1 |

The audited Latin fonts do not cover Japanese, Korean, Arabic, Hebrew,
Devanagari, or emoji. The Japanese fonts do not cover Korean, Arabic, Hebrew,
Devanagari, or emoji. That is acceptable for the current two translation
targets, but the server should reject a new target language until its configured
font chain passes a representative coverage fixture.

The former tracked BadaBoom BB license states that the font may not be
redistributed without author permission, restricts freeware use to independent
comic creation and nonprofit use, and requires a fee for commercial non-comic
use. The former Komika Text notice describes commercial use but contains no
clear redistribution grant. Their observed hashes were respectively
`807627b7055b5695a80f13a652ff166c5cf2bceb2f96afd2a132196090847ad3`
and `9fd8d1e74f53c7b8a440b9febee61eed5718cbe38b4bc04fa92eb2736db4ab4e`.
They must not be shipped without separate permission evidence. Comic Neue is
the verified OFL replacement; its frozen fit cases found no geometry blocker.

The installed Pillow build reports Raqm 0.10.3, FriBiDi 1.0.16, HarfBuzz
12.3.0, and FreeType 2.14.1. Frank's renderer does not pass an explicit
direction, language, feature set, or font fallback chain. This is not a current
English/Portuguese defect. It becomes a promotion gate for Arabic, Hebrew,
Indic, mixed-script, or emoji output.

## Comparison with Koharu

Koharu's renderer is more general. The checked source is commit
`2c78c0cd6eeec4dbf607f0a22e3a7e1f932b6ff3`. It uses HarfRust 0.13.3,
`unicode-bidi` 0.3.18, grapheme-safe font fallback, horizontal and vertical-rl
writing modes, contour-aware balanced line breaks, vertical punctuation
placement, and an explicit `overflowed` result. Its tests cover Arabic fallback,
mixed RTL runs, vertical CJK, mandatory breaks, hyphenation, and overflow.

Frank is stronger for its actual reader workflow: it has cleanup-mask
compositing, webtoon color sampling, publisher-ruby avoidance, page-wide manga
font normalization, metadata-only rerendering, and a tested Kindle/WebView lens.
Those behaviors are coupled to Frank's exact goals and should remain.

Frank now imports Koharu's useful overflow property into its persisted
contract. Render metadata records `fit_status`, final font size, line count,
and whether ellipsis was applied. Webtoon still falls back to 8 px and may
truncate remaining lines, but it reports `ellipsized` and logs the region.
Manga reports `overflow` when its minimum-size wrapped block cannot fit. This
prevents silent truncation from being mistaken for a successful fit while
preserving the bounded renderer behavior.

## Promotion checks

The current Phase 5 gate satisfies the structural, client, browser, font
redistribution, and current-language coverage checks. Before adding another
target language, require all of these again:

- the structural evaluator passes against the final stage-manifest revision;
- two consecutive evaluator executions have identical invariant fields and
  same-environment pixel hashes;
- Flutter, extension unit, and real-Chromium tests pass;
- the release contains only fonts with a recorded redistribution grant;
- every configured target language passes the glyph fixture; and
- render metadata reports truncation or minimum-size overflow instead of
  silently accepting it.

No large model, GPU, external service, or private page is required for this
gate.
