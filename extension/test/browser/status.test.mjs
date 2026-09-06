// The status indicator, measured where it actually lives.
//
// The first version was correct and invisible: a 9px dot at a third opacity in
// a corner, which the reader never once noticed and had to read the log
// instead. Whether something can be seen is a question only a browser can
// answer, so it is asked here rather than asserted in a stub.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { browserUsable, inBrowser } from './chromium.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '../../src/content/status.js'), 'utf8');
const available = await browserUsable();

/// Drive the indicator through states and report what is on screen after each.
function observe(states) {
  return inBrowser(async ({ src, sequence }) => {
    window.chrome = { runtime: { id: 'test' } };
    // eslint-disable-next-line no-eval
    eval(src);
    const settle = () => new Promise((resolve) => setTimeout(resolve, 420));
    const seen = {};
    for (const state of sequence) {
      window.FrankStatus.set(state);
      await settle();
      const dot = document.getElementById('__frankStatusDot');
      const label = document.getElementById('__frankStatusLabel');
      const style = dot ? getComputedStyle(dot) : null;
      const rect = dot ? dot.getBoundingClientRect() : null;
      seen[state] = {
        opacity: style ? Number(style.opacity) : 0,
        colour: style?.backgroundColor ?? '',
        side: rect ? Math.round(rect.width) : 0,
        onScreen: Boolean(rect && rect.right <= innerWidth + 1 && rect.bottom <= innerHeight + 1),
        label: label?.textContent ?? '',
        labelOpacity: label ? Number(getComputedStyle(label).opacity) : 0,
      };
    }
    return seen;
  }, { src: source, sequence: states });
}

test('each stage is visible and says what it is', { skip: !available }, async () => {
  const seen = await observe(['capturing', 'queued', 'ready']);

  for (const [state, shown] of Object.entries(seen)) {
    assert.ok(shown.opacity > 0.5, `${state} is barely visible at opacity ${shown.opacity}`);
    assert.ok(shown.side >= 10, `${state} is only ${shown.side}px across`);
    assert.ok(shown.onScreen, `${state} is off screen`);
    assert.ok(shown.label.length > 0, `${state} says nothing`);
    assert.ok(shown.labelOpacity > 0.5, `${state}'s words are invisible`);
  }
});

test('the stages are told apart by colour', { skip: !available }, async () => {
  const seen = await observe(['capturing', 'queued', 'ready', 'failed']);

  assert.notEqual(seen.queued.colour, seen.ready.colour,
    'waiting and ready must not look the same');
  assert.notEqual(seen.ready.colour, seen.failed.colour);
  assert.match(seen.ready.colour, /129, 199, 132/, 'ready is green');
  assert.match(seen.failed.colour, /229, 115, 115/, 'failed is red');
});

test('nothing is left on the page once there is nothing to say',
  { skip: !available }, async () => {
    const seen = await observe(['queued', 'idle']);
    assert.equal(seen.idle.opacity, 0);
    assert.equal(seen.idle.labelOpacity, 0);
  });
