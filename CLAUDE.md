# Frank Yomik

This repo has two moving parts:

- `server/`: Go API plus Python workers for manga and webtoon processing
- `client/`: Flutter reader app that wraps Kindle and Naver Webtoon in a WebView

If you need to get oriented fast, start with `server/main.go`, `server/handlers.go`, `server/worker/consumer.py`, and `client/lib/screens/reader_screen.dart`.

## What the system does

The API accepts page images, hashes them, deduplicates them, and queues jobs in Redis. Python workers pull from Redis, run OCR and translation, render a new image, cache the result on disk, and publish completion events. The Flutter client watches those events and overlays translated images back into the reader.

There are four pipelines:

- `manga_translate`: Japanese manga page -> English render
- `manga_furigana`: Japanese manga page -> furigana annotations
- `book_furigana`: Japanese prose page -> furigana in the gutters
- `webtoon`: Korean webtoon page -> English render

## Repo map

### Server

- `server/main.go`: boot, env parsing, Redis wiring, graceful shutdown
- `server/handlers.go`: REST API, cache endpoints, metadata patching, health
- `server/middleware.go`: bearer-token auth
- `server/queue.go`: Redis Streams submission and dedup
- `server/results.go`: Redis-backed job status and image retrieval
- `server/cache.go`: disk cache v2, content-addressed objects, manifest/ref layout
- `server/websocket.go`: websocket upgrade and origin checks
- `server/worker/consumer.py`: Redis stream consumer, result publishing, cache writes
- `server/worker/job.py`: pipeline routing and metadata payload generation
- `server/worker/page_cache.py`: Python-side cache v2 writer/reader
- `server/kindle/`: manga OCR, translation, rendering, furigana, bubble detection
- `server/kindle/book_layout.py`: column/gutter analysis for prose pages
- `server/kindle/book_processor.py`: per-column OCR and reading placement
- `server/kindle/book_renderer.py`: furigana drawn into the gutters
- `server/webtoon/`: webtoon OCR, translation, rendering, scraper

### Client

- `client/lib/screens/home_screen.dart`: launcher with arbitrary URL entry
- `client/lib/screens/reader_screen.dart`: main runtime, capture flow, overlay logic
- `client/lib/services/api_service.dart`: HTTP client for job submit/status/image download
- `client/lib/services/websocket_service.dart`: realtime progress/completion feed
- `client/lib/providers/jobs_provider.dart`: job state, cache lookup, polling fallback
- `client/lib/webview/js_bridge.dart`: JS handler registration and strategy selection
- `client/lib/webview/lens_controller.dart`: magnifier lens — the default way translations are shown
- `client/lib/webview/strategies/kindle_strategy.dart`: Kindle page detection and capture
- `client/lib/webview/strategies/naver_webtoon_strategy.dart`: Naver detection and capture

## Runtime flow

### Kindle / manga flow

1. Flutter loads `read.amazon.co.jp` in a WebView.
2. `KindleStrategy` injects JS that watches the visible blob-backed page image.
3. On page detection, `ReaderScreen` captures the visible page.
4. The client submits the PNG to `POST /api/v1/jobs`.
5. Go stores the source image in Redis and the disk object store, then enqueues the job.
6. The worker processes the page with the manga pipeline and writes the rendered image plus metadata to cache v2.
7. The worker stores transient job status in Redis and publishes a websocket notification.
8. The client downloads the result and overlays it on the reader.

### Webtoon flow

1. Flutter loads `comic.naver.com` in a WebView.
2. `NaverWebtoonStrategy` discovers page images and reports them back to Dart.
3. The client captures each image through JS `fetch()` first, then falls back to an app-side HTTP fetch if needed.
4. The worker runs the webtoon pipeline and returns the translated image.

## Reading modes

Both clients show the **original** page by default and reveal the translation
through a magnifier lens. Full-page replacement is still there as a mode.

- **Lens (default)**: the reader's own image is never touched. A finished
  translation is registered against the page element and revealed only under a
  circular magnifier. Press and hold for 200ms to open it; it tracks the
  pointer and closes on release. Magnification is 1.5x / 2x / 3x.
- **Full page**: the older behavior — the translated render replaces
  `img.src`, with the reapply/recovery timers that keep it stuck against
  Kindle repaints.

Two passive marks are made on the page, and nothing else: the magnifier, and a
9px status dot in the corner — amber while a page is being translated, green
when it can be peeked, red when it was given up on. Holding before a render
arrives shows an empty pulsing ring, because a hold that returns nothing is
indistinguishable from a broken lens.

Both need care about *when* they stop:

- The dot is not reset on detection. Kindle churns blob URLs several times a
  second, and hiding the dot on each one meant it never survived its own
  fade-in — which is why it appeared to do nothing at all.
- Closing hides the element whether it was showing a render or the ring. The
  ring never sets the "open" flag, so an early return on that flag left the
  circle on screen after the reader let go.
- The ring becomes the lens only once the render has been checked against the
  page. Opening as soon as a render arrives means opening before it is known
  to depict the page, and closing again when it turns out not to — which reads
  as the lens flickering out from under the reader.
- The waiting ring is only shown for pages still plausibly waiting. A pending
  mark expires, and giving up on a page clears it, because a ring that waits
  for ever is a worse answer than no ring: it was left on screen permanently
  once submission stopped.

Where the pieces live:

| | Flutter client | Chromium extension |
|---|---|---|
| lens module | `client/lib/webview/lens_controller.dart` (injected JS) | `extension/src/content/lens.js` |
| mode routing | `reader_screen.dart` -> `_registerLens` | `extension/src/content/overlay.js` |
| controls | in-page toolbar (mode + zoom buttons) | options page (`readerMode`, `lensZoom`) |
| persistence | `SharedPreferences` (`reader_lens_mode`, `reader_lens_zoom`) | extension storage, live via `chrome.storage.onChanged` |

Gesture rules that keep the reader usable:

- A tap under 200ms is left alone, so Kindle page turns still work.
- More than 12px of travel before the hold elapses reclassifies the press as a
  scroll or swipe, so webtoon scrolling still works.
- A peek swallows the `click`/`mouseup` it would otherwise spawn, so releasing
  the lens does not also turn the page.
- While a peek is held, pointer/mouse/touch moves are stopped from reaching the
  page at all (`stopPropagation` + `stopImmediatePropagation`, registered on
  `window` in capture phase). `preventDefault` alone is not enough: it stops
  the browser's default action, but Kindle's drag handler is a listener like
  any other and would still pan the page under the lens.
- A hold on a page whose translation has not arrived shows nothing but is still
  absorbed, so waiting for a render never costs the reader their page. If the
  render lands while the press is still down, the lens opens under the pointer.
  Kindle marks the page on screen through `setActivePage`; webtoon marks each
  page as it is submitted.
- Kindle starts selecting from the press itself, so nothing swallowed after it
  stops the highlight/copy/note menu: neither `preventDefault`, nor a
  `pointercancel` at the page element, nor registering our listeners first at
  `document_start` (all three were tried against a real reader). The extension
  therefore takes the pointerdown before the reader sees it and hands a tap
  back as a synthetic click, so pages still turn — mouse only, since a touch
  press is also how the reader scrolls and swipes, and that cannot be handed
  back. Only the Kindle strategy opts in; webtoon presses are untouched.
- **A render is checked against the page before it is bound.** Blob URL, page
  id and element identity have each pointed at the wrong page at some point,
  and a real render of the wrong page reads as a translation of what is on
  screen — the worst possible failure. A render is its page plus annotations,
  so both are reduced to a 16x16 brightness-normalised signature and compared:
  measured on real pages, a page against its own render differs by 0.05 and
  against a different page by 0.90, so the threshold sits at 0.5. Unreadable
  pixels (a tainted canvas) trust the binding rather than dropping a good
  render.
- Kindle reuses the same `<img>` across page turns *and across books*, so
  `setActivePage` releases the previous page's translation on every detection,
  navigation clears every registration, and a render whose page no longer has
  its shape is dropped rather than shown. Without those, a peek in a novel
  magnified a page of the manga read before it.
- Detection keeps running while submission is paused, because detection is also
  what releases the page the reader has left.

The pipeline is unchanged: pages are still captured, queued and rendered the
same way. Only the presentation differs.

Some behaviour cannot be judged outside a browser, and the assumptions that
cost the most today were exactly those. `extension/test/browser/` covers them:

- **The render check** (`signature.test.mjs`) — where the thresholds come from.
- **The status indicator** (`status.test.mjs`) — whether it can actually be
  seen. It caught a real one: a CSS animation outranks an inline style for the
  property it animates, so a pulsing dot could not be hidden at all.
- **The gestures** (`lens-gesture.test.mjs`) — driven through the browser's own
  input pipeline via `Input.dispatchMouseEvent`, not synthetic events. A
  synthetic event carries `isTrusted: false` and skips hit testing, and a test
  built on one proved the opposite of the truth here twice before real input
  settled it: a held peek does keep its events from the reader, and a quick tap
  does still reach it.

Browser tests run serially (`--test-concurrency=1`); several Chromium instances
at once made them flaky. They skip when no browser can be *driven* — a binary
existing is not the same thing, and a runner with Chrome installed but no
usable debugging port should not fail a release over an instrument. CI installs
a browser so they actually run rather than quietly skipping.

 `extension/test/browser/`
drives a real headless Chromium over the DevTools protocol — no dependencies,
skipped when no Chromium is present — using a real captured page and the real
render the server made from it as fixtures.

That is where the render check's thresholds come from, and it is the only place
they can be established. A dark novel page reduced to a 16-pixel signature has
a standard deviation of about 0.02; normalising by that turned sampling
differences into apparent mismatch, so a correct render measured 0.70 against
its own page. Manga, with high-contrast balloons, never hit it. Measured in the
browser with a deviation floor of 0.05:

| compared | difference |
|---|---|
| a page and its own render | 0.05 |
| the same page at the element's natural size | 0.14 |
| a different page of the same book | 0.31 |

Both lens modules are also tested against a DOM stub rather than a live reader:

- `client/test/js/lens_module.test.mjs`, run by `flutter test` through
  `client/test/lens_test.dart` when node is installed
- `extension/test/lens.test.mjs` and `extension/test/overlay.test.mjs`, sharing
  `extension/test/helpers/dom-stub.mjs`

The two implementations are separate files and must be kept in step: the
gesture thresholds, the magnifier math and the retention rules are the same
contract on both sides.

## Memory retention

Translated pages are whole PNGs, so nothing may hold them for the life of a
session:

- **Flutter jobs** (`jobs_provider.dart`): `pruneJobs` keeps bytes for the 20
  most recent renders and records for 200 pages; the captured source is
  released as soon as its render reaches the local cache. Jobs in flight are
  never evicted.
- **Lens registrations**: capped at 8 pages in both clients. Kindle also
  releases every non-active page on each page turn (`setActivePage`), which is
  what stops a reused `<img>` from peeking the page just left.
- **Extension full-page renders** (`overlay.js`): object URLs are tracked per
  page, capped at 8, revoked on page turn and on mode switch — not held until
  the tab closes.
- **Extension debug entries**: metadata for 40 pages, but whole-page data URLs
  only for the 3 most recent (the debug export only ever acts on the current
  page).

## Prefetch depth

A page takes tens of seconds to translate, so the submitted frontier has to run
ahead of the reader:

- **Webtoon (Flutter)**: batches of 8, refilled when the reader comes within 4
  pages of the frontier.
- **Webtoon (extension)**: images within 2400px of the viewport are queued,
  3 at a time; the options page offers whole-episode prefetch instead.
- **Kindle**: nothing can be prefetched — only the page the reader has open is
  rendered in the DOM, so there is nothing to capture ahead. Lens mode softens
  this: the original page is always readable while its translation is still in
  the queue.

## Text books (`book_furigana`)

A wide page image means two facing pages in manga, so detection splits it. A
novel is typeset to the window, so on a landscape screen a single prose page is
wide too — splitting one cuts its columns down the middle, annotates each half
as if it were a page, and stitches something that matches no page at all. Book
pages are therefore never split, in either client.


Kindle rasterises reflowable novels exactly like manga: the page is one
blob-backed `<img>` inside `#kr-renderer`, with no text in the DOM (the only
Japanese there is the title in the reader chrome). So a book cannot be
annotated by injecting `<ruby>`; it goes through the image pipeline like
everything else, and the lens is what keeps the page itself untouched.

What makes prose tractable is its regularity:

- Columns come from a projection profile — a page of this novel gives 29
  columns, ~35px wide, with ~34px gutters, uniform to within a few pixels.
  Manga has neither the count nor the uniformity, which is what
  `PageLayout.is_prose` tests before the pipeline will touch a page.
- Columns are read in slices of ~12 glyphs. This matters: manga-ocr expects
  about one balloon and returns gibberish for a whole column
  (`無様な方のコンションは…` for a column that reads
  `無機質な外観の古いマンションの8階。…`). Slices also bound alignment error,
  since a chunk's height over the characters read from it gives the pitch.
- Slices follow the column's ink runs, never blank paper — OCR asked to read
  an empty crop invents text.
- Readings come from `annotate()` on the whole column, so MeCab has the
  sentence for context, and are located in that text by search rather than by
  accumulating segment lengths (annotate drops whitespace morphemes).
- Furigana is drawn only in the gutter, in the page's own ink colour. Gutters
  that already hold the publisher's ruby are left alone.

Nothing on the page is redrawn. `book_furigana` pages cannot be re-rendered
from metadata — there are no editable regions, only gutter annotations.

Which pipeline a Kindle title needs cannot be told from the page, since manga
and prose arrive the same way, so the reader chooses, and both clients remember
the choice per volume (ASIN): the Flutter toolbar cycles Furigana -> English ->
Book, and the extension's popup has one pipeline select whose meaning follows
the reader — with a Kindle book open it sets that book's pipeline and the hint
names the book, otherwise it sets the default new books start from. A second,
separate per-book control was tried first and was worse: readers changed the
obvious one at the top, which was still global.
Without per-volume memory, moving from a novel to a manga left the manga on the
book pipeline, and the server refused every page of it.

## Values that cross a boundary must be declared at every hop

The worst bugs in this repo have all been one shape: a value that has to be
re-declared at each hop, where *omitting* it fails silently rather than loudly.
A missing field is `undefined`, the reading code falls back to a default, and
everything downstream looks plausible.

Three of them shipped:

- `bookPipelines` was absent from the allowlist in `getSettingsForSender`, so
  content scripts never saw per-book pipelines. The popup showed
  `book_furigana` while the page submitted `manga_furigana`, for days.
- `page_kind` was added to `JobStatusResponse` but not `WSNotification`.
  Clients prefer the socket, so it arrived empty exactly where it was needed.
- `sanitizeCapture` dropped `pageId` and `kindlePage`, so job metadata quietly
  used a running index instead of the reader's own page label.

Each is now guarded by a test that derives the requirement from the consuming
code rather than restating it:

- `extension/test/settings-exposure.test.mjs` scans the content scripts for
  every `settings.x` and every `capture.x` they read, and fails if the
  allowlist or the sanitiser does not carry it.
- `server/notification_test.go` builds one worker result and asserts the poll
  response and the socket notification carry the same facts.

Both were verified by reintroducing the original bug and watching them fail.
When adding a field that clients consume, extend these rather than trusting a
checklist — and note that test doubles built by reading the consumer will
happily encode the same wrong assumption as the code.

## Getting the pipeline wrong is self-correcting

A book on the wrong pipeline used to be silent and confusing: the manga
pipeline clears balloons it thinks it found and redraws their text, so run over
a novel it returns a page rearranged beyond recognition. Both directions now
fix themselves, per book, from what the worker measured rather than from what
the render looked like afterwards:

- The manga pipelines report `page_kind` for every page they process. Two
  `prose` verdicts in a row switch that book to `book_furigana`.
- The server refusing a page as "not typeset prose" switches that book back to
  a manga pipeline instead of counting towards the failure stop.
- A book is corrected once. A volume with pages of both kinds would otherwise
  be switched back and forth for as long as it stayed open.

## One look is not a verdict

The render check compares a render against the page it is bound to at the
moment the render arrives — which can be while the reader is mid-repaint.
Uploading both images a moment later, through the debug pair, showed them
matching at 0.096: the render was right, the capture was right, and the
comparison had simply looked at the wrong instant.

So a failed comparison is now retried (250ms, 750ms, 1500ms) before the render
is refused, and a registration is *unverified* until it passes — a peek before
then shows the waiting ring rather than a render that may yet be thrown away.
A render of a genuinely different page stays different however long you look,
so nothing is weakened by waiting.

The debug pair is what settled this, after five hypotheses died against the
same number. `exportDebugPair` now snapshots the element as it is rather than
the copy kept from capture time, which is what makes the two images comparable
at all.

## Capture the page the reader can see, not one parked beside it

Kindle keeps the pages either side of the current one in the DOM, laid out
inside the viewport but hidden. They are the same size and in the same place,
so `findVisibleBlob` — which only measured rectangle overlap, with no
visibility test at all — could return one of them, and ties went to whichever
came first in the DOM.

Meanwhile `overlay.js` and `lens.js` *did* check visibility. So the capture
and the comparison were looking at different elements by construction: we
translated a page nobody was looking at, then measured the result against the
page on screen and refused it. Because a hidden page never changes, every such
capture was byte-identical — same hash, same cache entry, the same stale render
returned instantly, a constant 0.70 that survived every other fix.

Detection now requires the element to be visible *and* prefers the one that
hit-tests at its own centre, which is what the reader is actually pointing at.

The test for it was checked against the original code, not just the fix: with
both the visibility test and the hit-test scoring removed, it fails.

## A reflowable book is laid out progressively

This is why the novel behaved differently from manga, and why it looked like
one bug when it was two. Kindle paints a *provisional* page for a reflowable
book and replaces it once pagination finishes — the reader sees "Learning
reading speed…" while that happens. Capturing during it yields a page nobody
ever sees: on the sample novel, the text sits in the top 60% of the image with
black below.

It is deterministic, which is what made the symptom so confusing: every such
capture is byte-identical, so they all hash the same, hit the client cache, and
come back as the same stale render — a constant 0.71 against the settled page,
never varying, surviving every reload. Fixed-layout manga has no provisional
stage, which is why manga worked throughout.

A capture therefore waits for the page to hold still (`SETTLE_MS`) before it is
taken, and a page that moves during that window is left for the next detection.

## Capture only what the page has actually decoded

An `<img>` keeps painting its previous frame until a new `src` decodes, and
`drawImage` copies what is painted. Kindle regenerates blob URLs constantly and
detection notices within half a second, so capturing immediately yields *the
page before this one* — identically, every time.

That is what three separate jobs each returning exactly 244 ruby meant: the
same bytes going in. Those captures then hashed the same, hit the cache, and
came back as a render of the previous page, which measured a constant 0.71
against the page on screen. The stable number was the tell; a race would have
varied.

The extension awaits `target.decode()` and re-checks the src afterwards. The
Flutter capture is synchronous, so it refuses a target whose `complete` is
false and waits for the next detection.

The DOM stub models this: what a canvas draws is the element's last *decoded*
src, not its current one. A stub where drawing always yields the current page
cannot express this bug at all.

## A render belongs to the element it was captured from

Kindle keeps neighbouring pages in the DOM during a turn — same size, same
position, both visible — and regenerates blob URLs on its own, so by the time a
render returns, the element it came from no longer matches the src it was
captured from. Scoring then decides, and it cannot tell one page image from the
next: a render lands on the page beside the one it depicts.

That is what a difference of 0.71, repeated across sessions with identical
dimensions, was saying: not a race, a systematic comparison against the
neighbouring page. Captures now stamp their element with the page id and
results bind to that stamp, falling back to scoring only when it is gone.

The test for this only earns its place because it was checked against the bug:
with the identity lookup removed it must fail. The first version passed either
way, because the fixture let the scorer match by src — the ambiguity the bug
depends on was missing from it.

## A page that moves under a job in flight

A reflowable book re-paginates while a job is running — Kindle is still
settling for several seconds after a load — so the render that comes back is a
real render of a page the reader has already left. Discarding it is right;
leaving the reader with nothing is not, so a mismatch re-captures whatever is
on screen now, at most twice before giving up until the next page turn.

The thresholds come from measurement, not intuition, and the middle case is
the one that matters:

| compared | difference |
|---|---|
| a page and its own render | 0.06 |
| two different pages of the same novel | 0.65 |
| pages from different books | 0.88 |

A stale render of the previous page therefore lands near 0.65, which is why
the threshold sits at 0.5 rather than somewhere looser.

Content scripts can set the pipeline for the book they are reading through
`SET_BOOK_PIPELINE`, which validates the ASIN shape and the pipeline name;
everything else in settings, credentials included, stays out of their reach.

## When a page keeps failing

Kindle regenerates blob URLs on its own, so a page that fails for a reason the
page cannot change — the wrong pipeline for the book, most likely — would be
resubmitted on every churn for as long as the tab stays open. Three identical
failures in a row pause auto-submission in `kindle.js` and report the server's
own message; changing a setting or forcing a reprocess resumes.

Popup settings now reach a reader that is already open: `bootstrap.js` hands
new settings to `FrankKindle.updateSettings` / `FrankWebtoon.updateSettings` on
every storage change, and a pipeline change re-reads the current page. Before
this, strategies kept whatever settings they started with until a reload.

`FrankKindle.state()` and `FrankLens.state()` report why nothing is happening;
read them from the extension's console context, not the page's.

Reloading or reinstalling the extension leaves the previous content scripts
running in open tabs against a dead runtime. Chrome injects the new copy, but
every module guards on `window.Frank*`, so the orphan used to keep the page —
with the settings it started with, which is how a book set to the text pipeline
kept submitting as manga. Each module now exposes `alive()` and `destroy()`:
a fresh copy stands the dead one down, taking its listeners and renders with
it. Reloading the page is still the cleanest reset, but is no longer required.

## Cache model

The server and worker share the same cache layout.

- Objects live at `cache/v2/objects/<aa>/<bb>/<sha256>`.
- Page manifests live at `cache/v2/pages/by-hash/<pipeline>/<source_hash>/manifest.json`.
- Optional metadata refs live at `cache/v2/pages/by-ref/<pipeline>/<title_slug>/<chapter>/<page>.json`.

The manifest is the source of truth. Redis is short-lived delivery state, not durable storage.

## API surface

Main endpoints:

- `POST /api/v1/jobs`
- `GET /api/v1/jobs/:id`
- `GET /api/v1/jobs/:id/image`
- `DELETE /api/v1/jobs/:id`
- `GET /api/v1/cache/...`
- `PATCH /api/v1/cache/by-hash/:pipeline/:source_hash/meta`
- `GET /api/v1/health`
- `GET /api/v1/ws`

Everything except `/api/v1/health` is bearer-token protected.

## Local dev

### Server

```bash
redis-server
cd server && AUTH_TOKEN=secret go run .
cd server && python -m worker --pipeline both
```

### Client

```bash
cd client
flutter pub get
flutter run -d linux
```

### Useful tests

```bash
cd server && go test ./...
cd server && pytest tests/unit/test_page_cache.py
cd client && flutter test
```

## Configuration that matters

- `AUTH_TOKEN`: required by the API
- `REDIS_URL`: Redis connection for the API
- `CACHE_DIR`: shared disk cache path for the API
- `server/config.yaml`: worker-side config for Ollama, OCR, fonts, cache, and webtoon settings
- `docker-compose.yml`: the normal multi-service deployment path

## Practical trust boundaries

These are the places worth treating as hostile input:

- image uploads to `POST /api/v1/jobs`
- metadata fields like `title`, `chapter`, and `page_number`
- websocket clients and job subscription lists
- web pages loaded inside the app WebView
- fallback image URLs surfaced by page JS in the client
- anything persisted under `cache/`

This is a token-gated bot, not a public consumer web app, so the security bar is different. The realistic threats are compromised tokens, malicious pages opened in the app, LAN sniffing when using plain HTTP, and cache/path abuse from authenticated clients.

## Security notes from this pass

Two issues were important enough to harden immediately:

- Cache path components were not consistently sanitized. `title` was slugified, but `chapter` and `page` could still flow into filesystem paths. The server and worker now reject unsafe path components instead of writing them.
- Site matching in the client used a regex over the full URL string. That meant a page like `https://evil.example/?next=read.amazon.co.jp` could activate a strategy. Matching now uses the parsed host, and the webtoon fallback fetch is limited to expected Naver image hosts.

Risks that still matter operationally:

- The websocket client still sends the auth token in a query string. That is convenient, but it can leak into proxy or tunnel logs.
- The Android app allows cleartext HTTP to local addresses. Fine for a trusted home LAN, not fine for untrusted Wi-Fi.
- The client stores the auth token in shared preferences. That is normal for this kind of side-loaded utility app, but it is not hardened secret storage.

## Server dependencies

`server/requirements.txt` bounds every direct dependency at the next major.
The file used to be unpinned, and a routine CVE rebuild silently moved opencv
from 4 to 5 in production — it happened to work, but nothing would have caught
it. Minor and patch releases still arrive on every rebuild.

Everything the code imports directly is declared there, even where another
package pulls it in today (numpy, fugashi, and the unidic-lite dictionary
`kindle/furigana.py` is tuned against).

The pip layer is otherwise cached against `requirements.txt` alone, so bump
`--build-arg DEPS_REFRESH=<date>` to pull dependencies fresh within their
bounds. Test the artifact rather than the dev venv — the local environment is
CUDA, production is ROCm:

```bash
docker run --rm --entrypoint python <worker-image> -m pytest tests/unit/ -q
```

## Versioning

Android `versionCode` is derived automatically from `git rev-list --count HEAD` in `client/android/app/build.gradle.kts`. This means every commit produces a higher build number — no manual bumping and no risk of version code regression (which causes "app not installed" errors on Android).

- **To release**: only bump the `version: X.Y.Z+1` display version in `client/pubspec.yaml`. The `+N` part is ignored for Android builds (git count is used instead) but kept for other platforms.
- **CI**: the release workflow uses `fetch-depth: 0` so the full git history is available for the commit count.
- **Fallback**: if git is unavailable (e.g. extracted tarball), the pubspec `+N` value is used.

Do NOT manually set `versionCode` in `build.gradle.kts` or rely on the `+N` in `pubspec.yaml` for Android.

## Where to look when something breaks

- job stuck at `queued`: `server/queue.go` (dedup returning stale job IDs), `server/handlers.go` (stale dedup re-enqueue), Redis Streams, worker logs
- result missing but worker finished: `server/results.go`, `server/worker/consumer.py`
- overlay wrong or stale: `client/lib/screens/reader_screen.dart`, `client/lib/webview/overlay_controller.dart`
- cache mismatch or rerender weirdness: `server/cache.go`, `server/worker/page_cache.py`, metadata patch route in `server/handlers.go`
- Kindle detection drift: `client/lib/webview/strategies/kindle_strategy.dart`
- Webtoon batching issues: `client/lib/screens/reader_screen.dart`, `client/lib/webview/strategies/naver_webtoon_strategy.dart`
- cache image 404 on download: `server/handlers.go` logs `WARN: cache image 404` with pipeline and hash; client retries with `force=true` via `jobs_provider.dart`

## Job reliability

Several layers protect against jobs getting stuck on high-latency or unreliable connections:

- **HTTP timeouts**: `api_service.dart` — 30s submit, 10s status poll, 45s image download.
- **Retry with backoff**: `jobs_provider.dart` — `_withRetry()` retries retryable errors up to 3 times with exponential backoff.
- **Stale dedup re-enqueue**: `handlers.go` — if dedup returns a job ID whose result expired from Redis (>60s old, no result), the server re-enqueues with `force=true`.
- **PEL claiming**: `consumer.py` — `XAUTOCLAIM` recovers orphaned Redis Stream entries from crashed workers every 30s.
- **Stale job timeout**: `page_job.dart` / `jobs_provider.dart` — jobs active >5 min are marked failed so the UI spinner stops.
- **Cache download fallback**: `jobs_provider.dart` — if a `cached-v2-*` image download fails (404), the client resubmits with `force=true` instead of failing permanently.
- **Targeted blob capture**: `kindle_strategy.dart` — captures the specific blob URL from the detection event, not whatever is currently visible (fixes wrong-page captures during rapid flipping).

## Editing guidance

If you change cache paths, update both implementations:

- Go: `server/cache.go`
- Python: `server/worker/page_cache.py`

If you change the job payload or metadata shape, check all three spots:

- `server/handlers.go`
- `server/worker/consumer.py`
- `client/lib/providers/jobs_provider.dart`

If you change reader/capture/job-submission behavior in one client, keep the
other client in sync before finishing:

- Flutter client: `client/lib/screens/reader_screen.dart`, `client/lib/services/api_service.dart`, `client/lib/providers/jobs_provider.dart`
- Chromium extension: `extension/src/content/`, `extension/src/background/service_worker.js`, `extension/src/shared/`

This includes Kindle/Naver detection, capture batching, job metadata fields,
priority/session sequencing, cache/download fallback, diagnostics, and API
request shape.

If you change WebView capture behavior, test both:

- Kindle single-page and spread-page capture
- Naver Webtoon lazy-loaded pages and batch prefetch
