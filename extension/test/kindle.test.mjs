// Tests for the Kindle strategy's failure handling.
//
// A page that fails for a reason it cannot change — the wrong pipeline for the
// book — fails identically every time, and Kindle regenerates its blob URLs on
// its own, so without a stop the same pixels are resubmitted for as long as
// the tab stays open. That is exactly what happened on a manga volume left on
// the book pipeline: a failed job every twenty seconds, indefinitely.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadContentScripts, makeImage } from './helpers/dom-stub.mjs';

const READER_SETTINGS = {
  configured: true,
  kindleEnabled: true,
  mangaPipeline: 'book_furigana',
};

function setup() {
  const page = makeImage({ left: 100, top: 50, width: 400, height: 600 });
  const env = loadContentScripts(['lens.js', 'overlay.js', 'kindle.js'], [page], {
    readerRoot: null,
  });
  env.window.FrankKindle.start({ ...READER_SETTINGS });
  return { ...env, kindle: env.window.FrankKindle, page };
}

const failure = (error) => ({
  type: 'FRANK_JOB_FAILED',
  site: 'kindle',
  pageId: 'kindle-1',
  error,
});

const PIPELINE_MISMATCH =
  'page does not look like typeset prose (0 text columns found); ' +
  'use a manga pipeline for this book';

// A failure the page cannot fix and that names no remedy: the reader stops
// resubmitting rather than switching anything.
const REPEATED_ERROR = 'server unavailable';

test('a single failure does not stop anything', () => {
  const { kindle, sendToContent } = setup();

  sendToContent(failure(REPEATED_ERROR));

  assert.equal(kindle.state().autoSubmitPaused, false);
  assert.equal(kindle.state().consecutiveFailures, 1);
});

test('the same failure repeating stops auto-submission', () => {
  const { kindle, sendToContent } = setup();

  for (let i = 0; i < 3; i++) sendToContent(failure(REPEATED_ERROR));

  assert.equal(kindle.state().autoSubmitPaused, true);
  assert.equal(kindle.state().lastFailureError, REPEATED_ERROR);
});

test('unrelated failures do not accumulate towards the stop', () => {
  // Transient trouble — a dropped connection, a timeout — should not look
  // like a page the server will never accept.
  const { kindle, sendToContent } = setup();

  sendToContent(failure('connection reset'));
  sendToContent(failure('timeout'));
  sendToContent(failure('connection reset'));

  assert.equal(kindle.state().autoSubmitPaused, false);
  assert.equal(kindle.state().consecutiveFailures, 1);
});

test('changing the pipeline resumes and re-reads the page', () => {
  const { kindle, sendToContent } = setup();
  for (let i = 0; i < 3; i++) sendToContent(failure(REPEATED_ERROR));
  assert.equal(kindle.state().autoSubmitPaused, true);

  kindle.updateSettings({ ...READER_SETTINGS, mangaPipeline: 'manga_furigana' });

  assert.equal(kindle.state().autoSubmitPaused, false);
  assert.equal(kindle.state().pipeline, 'manga_furigana');
  assert.equal(kindle.state().consecutiveFailures, 0);
});

test('settings that do not change the pipeline leave the stop in place', () => {
  const { kindle, sendToContent } = setup();
  for (let i = 0; i < 3; i++) sendToContent(failure(REPEATED_ERROR));

  kindle.updateSettings({ ...READER_SETTINGS, lensZoom: 3 });

  assert.equal(kindle.state().autoSubmitPaused, true, 'the page still fails');
});

test('the strategy reports the pipeline it is running', () => {
  const { kindle } = setup();
  assert.equal(kindle.state().started, true);
  assert.equal(kindle.state().pipeline, 'book_furigana');
});

// --- a volume keeps its own pipeline ---------------------------------------
// A manga volume and a novel need different pipelines, and the reader moves
// between them. With one global setting, switching to a novel left the manga
// on the book pipeline and switching back left the novel on the manga one.

const NOVEL = 'B0ABCDEFGH';

test('a book with no choice of its own follows the default', () => {
  const { kindle } = setup();
  assert.equal(kindle.state().book, NOVEL);
  assert.equal(kindle.state().pipeline, 'book_furigana');
  assert.equal(kindle.state().defaultPipeline, 'book_furigana');
});

test('a book with its own choice keeps it whatever the default is', () => {
  const { kindle } = setup();

  kindle.updateSettings({
    ...READER_SETTINGS,
    mangaPipeline: 'manga_furigana',
    bookPipelines: { [NOVEL]: 'book_furigana' },
  });

  assert.equal(kindle.state().pipeline, 'book_furigana', 'the novel stays a novel');
  assert.equal(kindle.state().defaultPipeline, 'manga_furigana');
});

test('another volume is unaffected by this one', () => {
  const { kindle } = setup();

  kindle.updateSettings({
    ...READER_SETTINGS,
    mangaPipeline: 'manga_furigana',
    bookPipelines: { B0ZZZZZZZZ: 'book_furigana' },
  });

  assert.equal(kindle.state().pipeline, 'manga_furigana',
    'a choice made on a different book must not follow the reader here');
});

test('choosing a pipeline for this book resumes a paused reader', () => {
  const { kindle, sendToContent } = setup();
  for (let i = 0; i < 3; i++) sendToContent(failure(REPEATED_ERROR));
  assert.equal(kindle.state().autoSubmitPaused, true);

  kindle.updateSettings({
    ...READER_SETTINGS,
    bookPipelines: { [NOVEL]: 'manga_furigana' },
  });

  assert.equal(kindle.state().autoSubmitPaused, false);
  assert.equal(kindle.state().pipeline, 'manga_furigana');
});

// --- the loader gate must not disable the reader for good -------------------
// The selector matches any class containing "loader", so an unrelated element
// that never disappears used to stop detection for the life of the page, in
// silence.

function withLoader() {
  const clock = { now: 1_000_000 };
  const page = makeImage({ left: 100, top: 50, width: 400, height: 600 });
  const loader = makeImage({ left: 0, top: 0, width: 300, height: 300 }, { className: 'app-loader' });
  const env = loadContentScripts(['lens.js', 'overlay.js', 'kindle.js'], [page, loader], { clock });
  env.window.FrankKindle.start({ ...READER_SETTINGS });
  return { ...env, kindle: env.window.FrankKindle, clock };
}

test('a loader on screen holds detection back', () => {
  const { kindle } = withLoader();
  const s = kindle.state();
  assert.equal(s.loaderPresent, true);
  assert.equal(s.loaderBlocking, true, 'the reader is still painting');
});

test('a loader that never leaves stops holding detection back', () => {
  // The selector matches any class containing "loader"; an unrelated element
  // that never disappears used to disable the reader for the life of the page.
  const { kindle, clock } = withLoader();
  assert.equal(kindle.state().loaderBlocking, true);

  clock.now += 6000;

  assert.equal(kindle.state().loaderPresent, true, 'still there');
  assert.equal(kindle.state().loaderBlocking, false, 'but no longer believed');
});

test('a page with no loader is never held back', () => {
  const { kindle } = setup();
  assert.equal(kindle.state().loaderPresent, false);
  assert.equal(kindle.state().loaderBlocking, false);
});

test('state explains why nothing is happening', () => {
  const { kindle } = setup();
  const s = kindle.state();

  assert.equal(s.started, true);
  assert.equal(s.configured, true);
  assert.equal(s.kindleEnabled, true);
  assert.equal(s.pageImageFound, true);
  assert.equal(s.pageImageSize, '400x600');
  assert.equal(typeof s.loaderPresent, 'boolean');
  assert.equal(typeof s.frameHostsReader, 'boolean');
});

test('state reports a missing page image rather than staying silent', () => {
  const env = loadContentScripts(['lens.js', 'overlay.js', 'kindle.js'], []);
  env.window.FrankKindle.start({ ...READER_SETTINGS });

  const s = env.window.FrankKindle.state();

  assert.equal(s.pageImageFound, false);
  assert.equal(s.pageImageSize, null);
  assert.equal(s.pagesDetected, 0);
});

test('an unconfigured extension says so instead of looking broken', () => {
  const env = loadContentScripts(['lens.js', 'overlay.js', 'kindle.js'], []);
  env.window.FrankKindle.start({ configured: false });

  assert.equal(env.window.FrankKindle.state().configured, false);
});

// --- leaving a book behind --------------------------------------------------

test('detection keeps running while submission is paused', () => {
  // Detection is also what releases the page the reader has left, so pausing
  // submissions must not stop it — otherwise a stale render survives the move
  // to another book.
  const { kindle, sendToContent } = setup();
  for (let i = 0; i < 3; i++) sendToContent(failure(REPEATED_ERROR));
  assert.equal(kindle.state().autoSubmitPaused, true);

  assert.equal(kindle.state().pageImageFound, true,
    'the page is still being looked at');
});

test('opening another book clears what the last one registered', async () => {
  const { kindle, window: win, sandbox } = setup();
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  await win.FrankLens.attach(img, 'kindle-1', 'data:image/png;base64,iVBORw0KGgo=');
  assert.equal(win.FrankLens.has('kindle-1'), true);

  sandbox.location.href = 'https://read.amazon.co.jp/?asin=B0ZZZZZZZZ';
  // A pipeline change schedules a detection tick, which is where the move
  // between books is noticed.
  kindle.updateSettings({ ...READER_SETTINGS, mangaPipeline: 'manga_furigana' });
  await new Promise((resolve) => setTimeout(resolve, 150));

  assert.equal(win.FrankLens.has('kindle-1'), false,
    'the previous book must not be peekable in this one');
});

// --- a novel is not a two-page spread ---------------------------------------
// A wide page image is two manga pages side by side, but a novel is typeset to
// the window: on a landscape screen a single prose page is wide too. Splitting
// one cuts its columns down the middle and stitches something that matches no
// page at all — which is what the render check started rejecting.

function withPage(rect, pipeline) {
  const page = makeImage(rect);
  const env = loadContentScripts(['lens.js', 'overlay.js', 'kindle.js'], [page]);
  env.window.FrankKindle.start({ ...READER_SETTINGS, mangaPipeline: pipeline });
  return { ...env, kindle: env.window.FrankKindle, page };
}

const WIDE = { left: 0, top: 0, width: 1600, height: 900 };
const TALL = { left: 0, top: 0, width: 700, height: 1000 };

test('a wide manga page is two facing pages', () => {
  assert.equal(withPage(WIDE, 'manga_furigana').kindle.state().pageMode, 'spread');
});

test('a wide text-book page is one page', () => {
  // This is the case that was breaking: a novel typeset to a landscape window.
  assert.equal(withPage(WIDE, 'book_furigana').kindle.state().pageMode, 'single');
});

test('a tall page is one page whatever the pipeline', () => {
  assert.equal(withPage(TALL, 'manga_furigana').kindle.state().pageMode, 'single');
  assert.equal(withPage(TALL, 'book_furigana').kindle.state().pageMode, 'single');
});

// --- a page that moved under a job in flight --------------------------------
// A reflowable book re-paginates while a job is running, so the render that
// comes back is a real render of a page the reader has already left. The
// render check refuses it; the reader should not be left with nothing.

const settle = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test('a mismatched render triggers a fresh capture', async () => {
  const env = setup();
  await settle(500);                       // the strategy's first detection
  const before = env.kindle.state().pagesDetected;
  assert.ok(before >= 1, 'the page was detected to begin with');

  env.window.FrankLens.__mismatch();
  await settle(1400);

  assert.ok(env.kindle.state().pagesDetected > before,
    'the page on screen is captured again rather than left untranslated');
  assert.equal(env.kindle.state().autoSubmitPaused, false);
});

test('a page that never settles stops rather than looping', async () => {
  const env = setup();
  await settle(500);

  for (let i = 0; i < 3; i++) {
    env.window.FrankLens.__mismatch();
    await settle(1400);
  }

  assert.equal(env.kindle.state().autoSubmitPaused, true,
    'chasing a moving page round a loop helps no one');
});

// --- the server's verdict beats our guesswork -------------------------------
// The worker measures the page itself: prose is many columns of one width,
// manga is not. That is a far better signal than a render that came back
// looking wrong, which only says something went awry somewhere.

function completion(pageKind) {
  return { type: 'FRANK_JOB_COMPLETE', site: 'kindle', pageId: 'kindle-1', pageKind };
}

test('one prose verdict is not enough', () => {
  const env = setup();
  const sent = [];
  env.sandbox.chrome.runtime.sendMessage = (m) => { sent.push(m); return Promise.resolve(); };
  env.window.FrankKindle.updateSettings({ ...READER_SETTINGS, mangaPipeline: 'manga_furigana' });

  env.sendToContent(completion('prose'));

  assert.equal(sent.filter((m) => m.type === 'SET_BOOK_PIPELINE').length, 0);
});

test('two prose verdicts move the book to the text-book pipeline', () => {
  const env = setup();
  const sent = [];
  env.sandbox.chrome.runtime.sendMessage = (m) => { sent.push(m); return Promise.resolve(); };
  env.window.FrankKindle.updateSettings({ ...READER_SETTINGS, mangaPipeline: 'manga_furigana' });

  env.sendToContent(completion('prose'));
  env.sendToContent(completion('prose'));

  const switches = sent.filter((m) => m.type === 'SET_BOOK_PIPELINE');
  assert.equal(switches.length, 1);
  assert.equal(switches[0].pipeline, 'book_furigana');
});

test('a single text page in a manga volume moves nothing', () => {
  const env = setup();
  const sent = [];
  env.sandbox.chrome.runtime.sendMessage = (m) => { sent.push(m); return Promise.resolve(); };
  env.window.FrankKindle.updateSettings({ ...READER_SETTINGS, mangaPipeline: 'manga_furigana' });

  env.sendToContent(completion('prose'));
  env.sendToContent(completion('artwork'));   // the run is broken
  env.sendToContent(completion('prose'));

  assert.equal(sent.filter((m) => m.type === 'SET_BOOK_PIPELINE').length, 0);
});

test('a book is never switched back and forth', () => {
  // A volume with pages of both kinds would otherwise oscillate.
  const env = setup();
  const sent = [];
  env.sandbox.chrome.runtime.sendMessage = (m) => { sent.push(m); return Promise.resolve(); };
  env.window.FrankKindle.updateSettings({ ...READER_SETTINGS, mangaPipeline: 'manga_furigana' });

  env.sendToContent(completion('prose'));
  env.sendToContent(completion('prose'));      // switches to book_furigana
  env.sendToContent(failure(PIPELINE_MISMATCH));   // would switch back
  env.sendToContent(failure(PIPELINE_MISMATCH));

  const switches = sent.filter((m) => m.type === 'SET_BOOK_PIPELINE');
  assert.equal(switches.length, 1, 'one correction, then it stops');
});

test('state names the build that is running', () => {
  // A released zip and a working copy carried the same version once, which
  // made every other diagnostic ambiguous.
  const { kindle, sandbox } = setup();
  sandbox.chrome.runtime.getManifest = () => ({ version: '9.9.9' });

  assert.equal(kindle.state().version, '9.9.9');
});

// --- capturing a page that has not decoded yet -------------------------------
// An <img> keeps painting its previous frame until a new src decodes, and
// drawImage copies what is painted. Kindle regenerates blob URLs constantly
// and detection notices within half a second, so capturing immediately yields
// the page before this one — identically, every time. That is why those
// captures all hashed the same, cache-hit, and never matched their page.

test('a page is decoded before it is captured', async () => {
  const env = setup();
  // Detection fires at 400ms, then the submit debounce runs for another 550ms.
  await settle(1200);

  assert.ok(env.page.decodeCalls >= 1, 'the capture waits for the frame it means to copy');
  assert.equal(env.page.decodedSrc, env.page.src, 'and captures the current page, not the last');
});

test('a page whose decode is abandoned is left for the next detection', async () => {
  const env = setup();
  env.page.decodeFails = true;         // Kindle replaced the src mid-decode
  const before = env.kindle.state().pagesSubmitted;

  env.page.src = 'blob:page-next';
  await settle(1400);

  assert.equal(env.kindle.state().pagesSubmitted, before,
    'better to wait than to submit the previous page');
});

// --- a reflowable book is laid out progressively -----------------------------
// Kindle paints a provisional page, then replaces it once it has finished
// paginating. Capturing during that yields a page the reader never sees —
// deterministically, so every such capture is byte-identical and cache-hits
// the same stale render. Fixed-layout manga settles at once.

function recordSubmissions(env) {
  const sent = [];
  const original = env.sandbox.chrome.runtime.sendMessage;
  env.sandbox.chrome.runtime.sendMessage = (message) => {
    sent.push(message);
    return original?.(message) ?? Promise.resolve();
  };
  return sent;
}

test('a page still being laid out is not captured', async () => {
  const env = setup();
  const sent = recordSubmissions(env);
  // Kindle replaces the provisional page midway through the settle window
  // (detection fires at 400ms, the submit debounce ends near 950ms, and the
  // page must then hold still for 1500ms).
  setTimeout(() => { env.page.src = 'blob:page-final'; }, 1500);

  await settle(2800);

  assert.equal(sent.filter((m) => m.type === 'SUBMIT_CAPTURE').length, 0,
    'a layout the reader never sees must not be submitted');
});

test('a settled page is captured', async () => {
  const env = setup();
  const sent = recordSubmissions(env);

  await settle(2600);

  assert.equal(sent.filter((m) => m.type === 'SUBMIT_CAPTURE').length, 1,
    'a page that stops changing is captured normally');
});

// --- the page the reader can actually see ------------------------------------
// Kindle parks the pages either side of the current one in the DOM, laid out
// inside the viewport but hidden. They are the same size and in the same
// place, so rectangle overlap cannot tell them apart. Capturing one produces a
// page nobody is looking at — and since it never changes, every such capture
// is byte-identical and cache-hits the same stale render.

function withHiddenNeighbour() {
  const hidden = makeImage({ left: 0, top: 0, width: 400, height: 600 }, { src: 'blob:hidden' });
  hidden.computedStyle = { display: 'block', visibility: 'visible', opacity: '0' };
  const visible = makeImage({ left: 0, top: 0, width: 400, height: 600 }, { src: 'blob:visible' });
  // The hidden one comes first, as the previous page does in Kindle's DOM.
  const env = loadContentScripts(['lens.js', 'status.js', 'overlay.js', 'kindle.js'],
    [hidden, visible]);
  env.window.FrankKindle.start({ ...READER_SETTINGS });
  return { ...env, kindle: env.window.FrankKindle, hidden, visible };
}

test('a hidden page is never the one captured', async () => {
  const env = withHiddenNeighbour();
  const sent = recordSubmissions(env);

  await settle(2600);

  const submitted = sent.find((m) => m.type === 'SUBMIT_CAPTURE');
  assert.ok(submitted, 'something was submitted');
  assert.equal(env.hidden.dataset.frankCapturedPage, undefined,
    'the hidden page must not be captured');
  assert.ok(env.visible.dataset.frankCapturedPage,
    'the page the reader sees is the one captured');
});

test('state reports the page image it would capture', () => {
  const env = withHiddenNeighbour();
  assert.equal(env.kindle.state().pageImageFound, true);
});

// --- the status dot ---------------------------------------------------------
// It reports where a page is: amber while translating, green once peekable,
// red when refused. Kindle churns blobs several times a second, and resetting
// the dot on every detection hid it before its fade-in ever finished.

test('detecting a page does not hide the dot', async () => {
  const env = setup();
  const states = [];
  env.window.FrankStatus = { set: (s) => states.push(s), alive: () => true };

  await settle(600);

  assert.ok(!states.includes('idle'),
    `a new page should go straight to capturing, saw ${states.join(' -> ')}`);
  assert.ok(states.includes('capturing'), 'and should say it is capturing');
});

test('giving up on a page shows that it failed', async () => {
  const env = setup();
  await settle(500);
  const states = [];
  env.window.FrankStatus = { set: (s) => states.push(s), alive: () => true };

  for (let i = 0; i < 3; i++) {
    env.window.FrankLens.__mismatch();
    await settle(1400);
  }

  assert.ok(states.includes('failed'), `expected a failure state, saw ${states.join(' -> ')}`);
});
