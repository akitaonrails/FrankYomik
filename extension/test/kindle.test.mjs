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
