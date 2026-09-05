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

test('a single failure does not stop anything', () => {
  const { kindle, sendToContent } = setup();

  sendToContent(failure(PIPELINE_MISMATCH));

  assert.equal(kindle.state().autoSubmitPaused, false);
  assert.equal(kindle.state().consecutiveFailures, 1);
});

test('the same failure repeating stops auto-submission', () => {
  const { kindle, sendToContent } = setup();

  for (let i = 0; i < 3; i++) sendToContent(failure(PIPELINE_MISMATCH));

  assert.equal(kindle.state().autoSubmitPaused, true);
  assert.equal(kindle.state().lastFailureError, PIPELINE_MISMATCH);
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
  for (let i = 0; i < 3; i++) sendToContent(failure(PIPELINE_MISMATCH));
  assert.equal(kindle.state().autoSubmitPaused, true);

  kindle.updateSettings({ ...READER_SETTINGS, mangaPipeline: 'manga_furigana' });

  assert.equal(kindle.state().autoSubmitPaused, false);
  assert.equal(kindle.state().pipeline, 'manga_furigana');
  assert.equal(kindle.state().consecutiveFailures, 0);
});

test('settings that do not change the pipeline leave the stop in place', () => {
  const { kindle, sendToContent } = setup();
  for (let i = 0; i < 3; i++) sendToContent(failure(PIPELINE_MISMATCH));

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
  for (let i = 0; i < 3; i++) sendToContent(failure(PIPELINE_MISMATCH));
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
  for (let i = 0; i < 3; i++) sendToContent(failure(PIPELINE_MISMATCH));
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
