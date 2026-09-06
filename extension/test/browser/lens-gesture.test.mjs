// The gesture assumptions, driven by the browser's own input pipeline.
//
// These are the claims that mattered most and were checked least: that a held
// peek keeps its events away from the reader, that a quick tap still reaches
// it, and that the lens cannot be pointed at. A stub can only show that our
// handler calls preventDefault; real input shows whether the reader's own
// handler consequently runs.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { browserUsable, withRealInput } from './chromium.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const lensSource = readFileSync(join(here, '../../src/content/lens.js'), 'utf8');
const available = await browserUsable();

/// A page holding one image, with the lens installed and a reader-like
/// listener counting what reaches the page.
function setupPage({ source, pressCapture }) {
  window.chrome = { runtime: { id: 'test' } };
  document.body.style.margin = '0';
  const canvas = document.createElement('canvas');
  canvas.width = 800;
  canvas.height = 1200;
  const context = canvas.getContext('2d');
  context.fillStyle = '#111';
  context.fillRect(0, 0, 800, 1200);
  context.fillStyle = '#eee';
  for (let i = 0; i < 40; i++) context.fillRect(40, 20 + i * 30, 720, 14);

  return new Promise((resolve) => {
    const page = document.createElement('img');
    page.id = 'page';
    page.style.cssText = 'position:absolute;left:0;top:0;width:400px;height:600px';
    page.addEventListener('load', () => {
      document.body.appendChild(page);
      // eslint-disable-next-line no-eval
      eval(source);
      window.FrankLens.markPending(page);
      if (pressCapture) window.FrankLens.setPressCapture(true);
      window.__reader = { move: 0, up: 0, click: 0 };
      window.__synthetic = 0;
      // A drag is a move with the button down that actually moves. The browser
      // emits one at the press point as part of the press itself, before any
      // hold can have begun; counting that failed this test for a reason that
      // had nothing to do with the lens.
      // Every move the reader sees, so a test can say which of them would
      // actually have dragged the page.
      window.__moves = [];
      document.addEventListener('pointermove', (event) => {
        window.__moves.push({ x: event.clientX, y: event.clientY, buttons: event.buttons });
        window.__reader.move += 1;
      });
      document.addEventListener('pointerup', () => { window.__reader.up += 1; });
      document.addEventListener('click', (event) => {
        window.__reader.click += 1;
        // A replayed tap is dispatched by us, so it is not trusted input.
        if (!event.isTrusted) window.__synthetic += 1;
      });
      resolve(true);
    });
    page.src = canvas.toDataURL('image/png');
  });
}

const at = (type, x, y, extra = {}) => ({
  type, x, y, button: 'left', clickCount: 1, buttons: type === 'mouseMoved' ? 1 : 0, ...extra,
});

test('a held peek keeps its events away from the reader', { skip: !available }, async () => {
  const result = await withRealInput({
    setup: setupPage,
    actions: [
      at('mouseMoved', 200, 300, { buttons: 0 }),                 // settle the cursor first
      at('mousePressed', 200, 300, { buttons: 1, pause: 900 }),   // held past 200ms
      at('mouseMoved', 220, 320, { buttons: 1 }),
      at('mouseMoved', 245, 345, { buttons: 1 }),
      at('mouseReleased', 245, 345),
    ],
    read: () => ({
      ...window.__reader,
      // A press emits a move at its own coordinates before any hold can have
      // begun. That one moves nothing; a drag is displacement.
      drags: window.__moves.filter((m) => m.buttons && (m.x !== 200 || m.y !== 300)),
    }),
  }, { source: lensSource });

  assert.deepEqual(result.drags, [],
    `the reader was dragged to ${JSON.stringify(result.drags)} during a peek`);
  assert.equal(result.click, 0, 'the reader saw a click on release; the page would turn');
});

test('a quick tap still reaches the reader', { skip: !available }, async () => {
  const result = await withRealInput({
    setup: setupPage,
    actions: [
      at('mousePressed', 200, 300, { buttons: 1, pause: 60 }),    // shorter than the hold
      at('mouseReleased', 200, 300),
    ],
    read: () => window.__reader,
  }, { source: lensSource });

  assert.equal(result.click, 1, 'a tap must still turn the page');
});

test('the lens is shown, above the page, and cannot be pointed at',
  { skip: !available }, async () => {
    const result = await withRealInput({
      setup: setupPage,
      actions: [
        at('mouseMoved', 200, 300, { buttons: 0 }),
        at('mousePressed', 200, 300, { buttons: 1, pause: 900 }),
      ],
      read: () => {
        const el = document.getElementById('__frankLens');
        if (!el) return { missing: true };
        const style = getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return {
          display: style.display,
          pointerEvents: style.pointerEvents,
          zIndex: Number(style.zIndex),
          diameter: Math.round(rect.width),
          hit: document.elementFromPoint(rect.left + rect.width / 2,
                                         rect.top + rect.height / 2)?.id ?? null,
        };
      },
    }, { source: lensSource });

    assert.ok(!result.missing, 'a hold should show something');
    assert.equal(result.display, 'block');
    assert.equal(result.pointerEvents, 'none', 'it must not intercept the reader');
    assert.ok(result.zIndex > 1_000_000, `z-index ${result.zIndex} may sit under the reader`);
    assert.ok(result.diameter >= 180, `only ${result.diameter}px across`);
    assert.equal(result.hit, 'page', 'the pointer must still reach the page beneath');
  });

test('with press capture on, a tap is handed back to the reader',
  { skip: !available }, async () => {
    // The Kindle strategy takes the press itself, because the reader begins
    // selecting from it. That makes handing the tap back the only thing
    // turning pages — for manga as much as for books — so it is checked with
    // real input rather than assumed.
    const result = await withRealInput({
      setup: setupPage,
      actions: [
        at('mouseMoved', 200, 300, { buttons: 0 }),
        at('mousePressed', 200, 300, { buttons: 1, pause: 60 }),   // a tap
        at('mouseReleased', 200, 300),
      ],
      read: () => ({ ...window.__reader, synthetic: window.__synthetic }),
    }, { source: lensSource, pressCapture: true });

    assert.equal(result.click, 1,
      'a captured tap must still reach the reader, or pages stop turning');
    assert.equal(result.synthetic, 1, 'and it is the one we handed back');
  });

test('with press capture on, a peek is not handed back',
  { skip: !available }, async () => {
    const result = await withRealInput({
      setup: setupPage,
      actions: [
        at('mouseMoved', 200, 300, { buttons: 0 }),
        at('mousePressed', 200, 300, { buttons: 1, pause: 900 }),  // a peek
        at('mouseReleased', 200, 300),
      ],
      read: () => window.__reader,
    }, { source: lensSource, pressCapture: true });

    assert.equal(result.click, 0, 'releasing a peek must not turn the page');
  });
