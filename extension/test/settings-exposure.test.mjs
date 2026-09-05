// The service worker hands content scripts an allowlisted subset of settings,
// so the auth token never reaches a page. The failure mode is asymmetric: a
// setting wrongly included leaks, and a setting wrongly *omitted* does not
// fail — it silently falls back to a default.
//
// That is what happened. bookPipelines was missing, so a book set to the
// text-book pipeline kept submitting as manga; readerMode and lensZoom were
// missing, so those popup settings never left the popup. Nothing errored.
//
// This derives the requirement from the content scripts themselves.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');

function contentScriptSettingKeys() {
  const dir = join(root, 'src/content');
  const keys = new Set();
  for (const file of readdirSync(dir).filter((f) => f.endsWith('.js'))) {
    const source = readFileSync(join(dir, file), 'utf8');
    for (const [, key] of source.matchAll(/\bsettings\.([a-zA-Z][\w]*)/g)) keys.add(key);
  }
  return keys;
}

/// The object literal getSettingsForSender returns for a page sender.
function allowlistedKeys() {
  const source = readFileSync(join(root, 'src/background/service_worker.js'), 'utf8');
  const start = source.indexOf('if (!sender?.tab) return settings;');
  assert.ok(start > 0, 'getSettingsForSender should return early for the popup');
  const body = source.slice(start, source.indexOf('\n}', start));
  return new Set([...body.matchAll(/^\s{4}([a-zA-Z][\w]*):/gm)].map((m) => m[1]));
}

test('every setting a content script reads is actually sent to it', () => {
  const needed = contentScriptSettingKeys();
  const sent = allowlistedKeys();
  const missing = [...needed].filter((key) => !sent.has(key));

  assert.deepEqual(missing, [],
    `content scripts read these but never receive them: ${missing.join(', ')}`);
});

test('the auth token is never sent to a page', () => {
  const sent = allowlistedKeys();
  assert.equal(sent.has('authToken'), false);
  // configured says whether a token exists without disclosing it.
  assert.equal(sent.has('configured'), true);
});

test('the allowlist sends nothing a content script does not use', () => {
  // Not a security boundary, just hygiene: the list should stay legible.
  const needed = contentScriptSettingKeys();
  const extra = [...allowlistedKeys()]
    .filter((key) => key !== 'configured')   // says a token exists, not what it is
    .filter((key) => !needed.has(key));

  assert.deepEqual(extra, [], `sent but unused: ${extra.join(', ')}`);
});

// The same shape as the settings allowlist, and the same silent failure: a
// capture field the content scripts read but sanitizeCapture drops comes back
// undefined, and the code quietly falls back instead of erroring.
test('every capture field a content script reads survives sanitising', async () => {
  const { sanitizeCapture } = await import('../src/shared/policy.js');
  const dir = join(root, 'src/content');
  const read = new Set();
  for (const file of readdirSync(dir).filter((f) => f.endsWith('.js'))) {
    const source = readFileSync(join(dir, file), 'utf8');
    for (const [, key] of source.matchAll(/\bcapture\.([a-zA-Z][\w]*)/g)) read.add(key);
  }

  const sanitized = sanitizeCapture({
    imgSrc: 'blob:x', originalSrc: 'https://x/1.jpg', groupId: 'g1',
    pageId: 'kindle-1', kindlePage: 'Page 6 of 275', side: 'left',
    index: 3, pageMode: 'single', rect: { x: 0, y: 0, width: 10, height: 20 },
  });

  const dropped = [...read].filter((key) => sanitized[key] === undefined);
  assert.deepEqual(dropped, [],
    `content scripts read these but sanitizeCapture drops them: ${dropped.join(', ')}`);
});
