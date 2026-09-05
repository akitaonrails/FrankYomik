// Behavioral tests for the in-page lens module that lens_controller.dart
// injects. The module is extracted from the Dart source so there is exactly
// one copy of the script, and exercised against a minimal DOM stub: the
// magnifier math and the tap-vs-hold classification are the parts that would
// silently misbehave in a real reader.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
const dartSource = readFileSync(join(here, '../../lib/webview/lens_controller.dart'), 'utf8');
const match = dartSource.match(/const String _moduleScript = r'''\n([\s\S]*?)\n''';/);
assert.ok(match, 'lens module script not found in lens_controller.dart');
const moduleSource = match[1];

const VIEWPORT = { width: 1000, height: 800 };

function makeElement(tag) {
  return {
    tagName: tag.toUpperCase(),
    dataset: {},
    style: { cssText: '' },
    classList: (() => {
      const set = new Set();
      return {
        add: (c) => set.add(c),
        remove: (c) => set.delete(c),
        contains: (c) => set.has(c),
      };
    })(),
    isConnected: true,
    children: [],
    src: '',
    className: '',
    appendChild(child) { this.children.push(child); return child; },
    addEventListener() {},
    getBoundingClientRect() {
      const r = this._rect || { left: 0, top: 0, width: 0, height: 0 };
      return { ...r, right: r.left + r.width, bottom: r.top + r.height };
    },
  };
}

function makeImage(rect, { src = 'blob:page', className = '' } = {}) {
  const el = makeElement('img');
  el._rect = rect;
  el.src = src;
  el.className = className;
  return el;
}

/// Matches only the selector shapes the lens module actually uses.
function matches(el, selector) {
  if (selector === 'img') return el.tagName === 'IMG';
  if (selector === 'img.toon_image') return el.tagName === 'IMG' && el.className.includes('toon_image');
  if (selector === 'img[data-frank-lens-src]') return el.tagName === 'IMG' && !!el.dataset.frankLensSrc;
  const pageId = selector.match(/^img\[data-frank-lens-page-id="(.*)"\]$/);
  if (pageId) return el.tagName === 'IMG' && el.dataset.frankLensPageId === pageId[1];
  return false;
}

function buildSandbox(images) {
  const listeners = new Map();
  const revoked = [];
  let urlCounter = 0;

  const queryAll = (selector) => images.filter((el) => matches(el, selector));
  const body = makeElement('body');
  body.querySelectorAll = queryAll;

  const document = {
    body,
    head: makeElement('head'),
    documentElement: makeElement('html'),
    createElement: makeElement,
    querySelector: () => null,       // no Kindle reader root in the stub
    querySelectorAll: queryAll,
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
  };

  const window = {
    innerWidth: VIEWPORT.width,
    innerHeight: VIEWPORT.height,
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1' }),
    addEventListener(type, fn) { document.addEventListener(type, fn); },
    document,
  };
  window.window = window;

  const sandbox = {
    window,
    document,
    console,
    setTimeout,
    clearTimeout,
    atob: (b64) => Buffer.from(b64, 'base64').toString('binary'),
    Blob: class { constructor(parts, opts) { this.parts = parts; this.type = opts?.type; } },
    Image: class { set src(v) { this._src = v; } get src() { return this._src; } },
    URL: {
      createObjectURL: () => `blob:lens-${++urlCounter}`,
      revokeObjectURL: (u) => revoked.push(u),
    },
  };
  sandbox.globalThis = sandbox;

  vm.createContext(sandbox);
  vm.runInContext(moduleSource, sandbox);

  const fire = (type, event) => {
    for (const fn of listeners.get(type) || []) fn(event);
  };
  const pointer = (type, x, y, extra = {}) => ({
    type, clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse', button: 0,
    cancelable: true, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this.propagationStopped = true; },
    ...extra,
  });

  return { sandbox, lens: sandbox.window.__frankLens, document, fire, pointer, revoked };
}

const PNG_B64 = 'iVBORw0KGgo=';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const lensEl = (document) => document.body.children.find((c) => c.id === '__frankLens');

test('register binds the translation to the page image', () => {
  const img = makeImage({ left: 100, top: 50, width: 400, height: 600 });
  const { lens } = buildSandbox([img]);

  const result = JSON.parse(lens.register(PNG_B64, { pageId: 'kindle-1', expectedBlobSrc: 'blob:page' }));

  assert.equal(result.ok, true);
  assert.equal(result.matchType, 'kindle-blob');
  assert.match(img.dataset.frankLensSrc, /^blob:lens-/);
  assert.equal(img.dataset.frankLensPageId, 'kindle-1');
  // The reader's own image must keep showing: the original stays untranslated.
  assert.equal(img.src, 'blob:page');
});

test('register reports no_target when the expected page is not on screen', () => {
  const img = makeImage({ left: 100, top: 50, width: 400, height: 600 }, { src: 'blob:other' });
  const { lens } = buildSandbox([img]);

  const result = JSON.parse(lens.register(PNG_B64, { pageId: 'kindle-1', expectedBlobSrc: 'blob:page' }));

  assert.equal(result.ok, false);
  assert.equal(result.error, 'no_target');
});

test('a quick tap never opens the lens, so page turns still work', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, document, fire, pointer } = buildSandbox([img]);
  lens.register(PNG_B64, { pageId: 'kindle-1', expectedBlobSrc: 'blob:page' });

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(60);
  const up = pointer('pointerup', 200, 300);
  fire('pointerup', up);

  assert.equal(lens.isOpen(), false);
  assert.equal(up.defaultPrevented, false, 'a tap must reach the reader');
  assert.equal(lensEl(document), undefined);
});

test('holding opens the lens and magnifies the point under the pointer', async () => {
  const img = makeImage({ left: 100, top: 50, width: 400, height: 600 });
  const { lens, document, fire, pointer } = buildSandbox([img]);
  lens.register(PNG_B64, { pageId: 'kindle-1', expectedBlobSrc: 'blob:page' });

  fire('pointerdown', pointer('pointerdown', 300, 350));
  await wait(260);

  assert.equal(lens.isOpen(), true);
  const el = lensEl(document);
  assert.equal(el.style.display, 'block');

  // Default zoom is 2x, so the translation is drawn at twice the page's box.
  assert.equal(el.style.backgroundSize, '800px 1200px');

  // The pressed point (200, 300 inside the page) must land at the lens centre.
  const d = parseFloat(el.style.width);
  const r = d / 2;
  const [bgX, bgY] = el.style.backgroundPosition.split(' ').map(parseFloat);
  assert.equal(bgX, r - (300 - 100) * 2);
  assert.equal(bgY, r - (350 - 50) * 2);

  // Mouse peeking keeps the lens centred on the cursor.
  assert.equal(parseFloat(el.style.left) + r, 300);
  assert.equal(parseFloat(el.style.top) + r, 350);
});

test('releasing a peek closes the lens and swallows the click', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = buildSandbox([img]);
  lens.register(PNG_B64, { pageId: 'kindle-1', expectedBlobSrc: 'blob:page' });

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);
  fire('pointerup', pointer('pointerup', 200, 300));
  assert.equal(lens.isOpen(), false);

  const click = pointer('click', 200, 300);
  fire('click', click);
  assert.equal(click.defaultPrevented, true, 'the peek must not turn the page');
  assert.equal(click.propagationStopped, true);
});

test('travel before the hold elapses is a scroll, not a peek', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = buildSandbox([img]);
  lens.register(PNG_B64, { pageId: 'wt-0', originalSrc: 'blob:page' });

  fire('pointerdown', pointer('pointerdown', 200, 300));
  fire('pointermove', pointer('pointermove', 200, 340));
  await wait(260);

  assert.equal(lens.isOpen(), false);
});

test('dragging while held tracks the pointer and blocks the page from scrolling', async () => {
  const img = makeImage({ left: 100, top: 50, width: 400, height: 600 });
  const { lens, document, fire, pointer } = buildSandbox([img]);
  lens.register(PNG_B64, { pageId: 'kindle-1', expectedBlobSrc: 'blob:page' });

  fire('pointerdown', pointer('pointerdown', 300, 350));
  await wait(260);
  const move = pointer('pointermove', 320, 360);
  fire('pointermove', move);

  const el = lensEl(document);
  const r = parseFloat(el.style.width) / 2;
  const [bgX] = el.style.backgroundPosition.split(' ').map(parseFloat);
  assert.equal(bgX, r - (320 - 100) * 2);
  assert.equal(move.defaultPrevented, true);
});

test('zoom changes rescale the open lens in place', async () => {
  const img = makeImage({ left: 100, top: 50, width: 400, height: 600 });
  const { lens, document, fire, pointer } = buildSandbox([img]);
  lens.register(PNG_B64, { pageId: 'kindle-1', expectedBlobSrc: 'blob:page' });

  fire('pointerdown', pointer('pointerdown', 300, 350));
  await wait(260);
  lens.setZoom(3);

  const el = lensEl(document);
  const r = parseFloat(el.style.width) / 2;
  assert.equal(el.style.backgroundSize, '1200px 1800px');
  const [bgX, bgY] = el.style.backgroundPosition.split(' ').map(parseFloat);
  assert.equal(bgX, r - (300 - 100) * 3);
  assert.equal(bgY, r - (350 - 50) * 3);
});

test('a page turn releases the old translation so a reused img cannot be peeked', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, revoked, fire, pointer } = buildSandbox([img]);
  lens.register(PNG_B64, { pageId: 'kindle-1', expectedBlobSrc: 'blob:page' });
  const firstUrl = img.dataset.frankLensSrc;

  lens.setActivePage('kindle-2');

  assert.deepEqual(revoked, [firstUrl]);
  assert.equal(img.dataset.frankLensSrc, undefined);
  assert.equal(lens.has('kindle-1'), false);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);
  assert.equal(lens.isOpen(), false, 'the previous page must not be peekable');
});

test('re-registering a page revokes the translation it replaces', () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, revoked } = buildSandbox([img]);
  lens.register(PNG_B64, { pageId: 'kindle-1', expectedBlobSrc: 'blob:page' });
  const firstUrl = img.dataset.frankLensSrc;

  lens.register(PNG_B64, { pageId: 'kindle-1', expectedBlobSrc: 'blob:page' });

  assert.deepEqual(revoked, [firstUrl]);
  assert.notEqual(img.dataset.frankLensSrc, firstUrl);
});

test('disabling the lens disarms the gesture', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = buildSandbox([img]);
  lens.register(PNG_B64, { pageId: 'kindle-1', expectedBlobSrc: 'blob:page' });
  lens.setEnabled(false);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);

  assert.equal(lens.isOpen(), false);
});

test('webtoon pages are matched by their original src', () => {
  const other = makeImage({ left: 0, top: 0, width: 400, height: 600 }, { src: 'https://naver/1.jpg' });
  const target = makeImage({ left: 0, top: 700, width: 400, height: 600 }, { src: 'https://naver/2.jpg' });
  const { lens } = buildSandbox([other, target]);

  const result = JSON.parse(lens.register(PNG_B64, { pageId: 'wt-1', originalSrc: 'https://naver/2.jpg' }));

  assert.equal(result.ok, true);
  assert.equal(result.matchType, 'webtoon-src');
  assert.ok(target.dataset.frankLensSrc);
  assert.equal(other.dataset.frankLensSrc, undefined);
});

test('touch peeks lift the lens clear of the fingertip', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, document, fire, pointer } = buildSandbox([img]);
  lens.register(PNG_B64, { pageId: 'kindle-1', expectedBlobSrc: 'blob:page' });

  fire('pointerdown', pointer('pointerdown', 200, 500, { pointerType: 'touch' }));
  await wait(260);

  const el = lensEl(document);
  const r = parseFloat(el.style.width) / 2;
  assert.equal(parseFloat(el.style.top) + r, 500 - r - 28);
  // Content still tracks the finger, not the lens centre.
  const [, bgY] = el.style.backgroundPosition.split(' ').map(parseFloat);
  assert.equal(bgY, r - 500 * 2);
});
