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
import { chromiumPath, withRealInput } from './chromium.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const lensSource = readFileSync(join(here, '../../src/content/lens.js'), 'utf8');
const available = Boolean(chromiumPath());

/// A page holding one image, with the lens installed and a reader-like
/// listener counting what reaches the page.
function setup({ source }) {
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
      window.__reader = { move: 0, up: 0, click: 0 };
      document.addEventListener('pointermove', () => { window.__reader.move += 1; });
      document.addEventListener('pointerup', () => { window.__reader.up += 1; });
      document.addEventListener('click', () => { window.__reader.click += 1; });
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
    setup,
    actions: [
      at('mousePressed', 200, 300, { buttons: 1, pause: 550 }),   // held past 200ms
      at('mouseMoved', 220, 320, { buttons: 1 }),
      at('mouseMoved', 245, 345, { buttons: 1 }),
      at('mouseReleased', 245, 345),
    ],
    read: () => ({ ...window.__reader, lens: document.getElementById('__frankLens')?.style.display }),
  }, { source: lensSource });

  assert.equal(result.move, 0,
    `the reader saw ${result.move} moves during a peek; the page would drag under the lens`);
  assert.equal(result.click, 0, 'the reader saw a click on release; the page would turn');
});

test('a quick tap still reaches the reader', { skip: !available }, async () => {
  const result = await withRealInput({
    setup,
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
      setup,
      actions: [at('mousePressed', 200, 300, { buttons: 1, pause: 550 })],
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
