// The lens is what the reader actually touches: these cover the magnifier
// math, the tap-vs-hold classification that decides whether Kindle turns the
// page, and the retention rules that keep translated pages from piling up.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadContentScripts, makeImage, wait, lensElement, DATA_URL } from './helpers/dom-stub.mjs';

function setup(images) {
  const env = loadContentScripts(['lens.js'], images);
  return { ...env, lens: env.window.FrankLens };
}

test('attach keeps the reader image untouched', async () => {
  const img = makeImage({ left: 100, top: 50, width: 400, height: 600 });
  const { lens } = setup([img]);

  assert.equal(await lens.attach(img, 'kindle-1', DATA_URL), true);

  assert.equal(img.src, 'blob:page', 'the original page must stay on screen');
  assert.match(img.dataset.frankLensSrc, /^blob:frank-/);
  assert.equal(img.dataset.frankLensPageId, 'kindle-1');
});

test('a quick tap never opens the lens, so page turns still work', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, document, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(60);
  const up = pointer('pointerup', 200, 300);
  fire('pointerup', up);

  assert.equal(lens.isOpen(), false);
  assert.equal(up.defaultPrevented, false, 'a tap must reach the reader');
  assert.equal(lensElement(document), undefined);
});

test('holding opens the lens and magnifies the point under the pointer', async () => {
  const img = makeImage({ left: 100, top: 50, width: 400, height: 600 });
  const { lens, document, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 300, 350));
  await wait(260);

  assert.equal(lens.isOpen(), true);
  const el = lensElement(document);
  assert.equal(el.style.display, 'block');
  assert.equal(el.style.backgroundSize, '800px 1200px', 'default 2x of the page box');

  const radius = Number.parseFloat(el.style.width) / 2;
  const [bgX, bgY] = el.style.backgroundPosition.split(' ').map(Number.parseFloat);
  assert.equal(bgX, radius - (300 - 100) * 2);
  assert.equal(bgY, radius - (350 - 50) * 2);
  assert.equal(Number.parseFloat(el.style.left) + radius, 300);
  assert.equal(Number.parseFloat(el.style.top) + radius, 350);
});

test('releasing a peek closes the lens and swallows the click', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);
  fire('pointerup', pointer('pointerup', 200, 300));

  assert.equal(lens.isOpen(), false);
  const click = pointer('click', 200, 300);
  fire('click', click);
  assert.equal(click.defaultPrevented, true, 'the peek must not also turn the page');
});

test('travel before the hold elapses is a scroll, not a peek', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = setup([img]);
  await lens.attach(img, 'wt-0', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  fire('pointermove', pointer('pointermove', 200, 340));
  await wait(260);

  assert.equal(lens.isOpen(), false);
});

test('dragging while held tracks the pointer and blocks scrolling', async () => {
  const img = makeImage({ left: 100, top: 50, width: 400, height: 600 });
  const { lens, document, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 300, 350));
  await wait(260);
  const move = pointer('pointermove', 320, 360);
  fire('pointermove', move);

  const el = lensElement(document);
  const radius = Number.parseFloat(el.style.width) / 2;
  const [bgX] = el.style.backgroundPosition.split(' ').map(Number.parseFloat);
  assert.equal(bgX, radius - (320 - 100) * 2);
  assert.equal(move.defaultPrevented, true);
});

test('zoom changes rescale an open lens in place', async () => {
  const img = makeImage({ left: 100, top: 50, width: 400, height: 600 });
  const { lens, document, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 300, 350));
  await wait(260);
  lens.setZoom(3);

  const el = lensElement(document);
  const radius = Number.parseFloat(el.style.width) / 2;
  assert.equal(el.style.backgroundSize, '1200px 1800px');
  assert.equal(el.style.backgroundPosition.split(' ').map(Number.parseFloat)[0], radius - (300 - 100) * 3);
});

test('an invalid zoom is ignored rather than blanking the lens', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, document, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);
  lens.setZoom('nonsense');

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);

  assert.equal(lensElement(document).style.backgroundSize, '800px 1200px');
});

test('a page turn releases the previous render and blocks peeking it', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, revoked, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);
  const firstUrl = img.dataset.frankLensSrc;

  lens.setActivePage('kindle-2');

  assert.deepEqual(revoked, [firstUrl], 'the old page must not stay in memory');
  assert.equal(lens.has('kindle-1'), false);
  assert.equal(img.dataset.frankLensSrc, undefined);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);
  assert.equal(lens.isOpen(), false, 'the page just left must not be peekable');
});

test('an open lens closes when the reader turns the page', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);
  assert.equal(lens.isOpen(), true);

  lens.setActivePage('kindle-2');
  assert.equal(lens.isOpen(), false);
});

test('re-attaching a page revokes the render it replaces', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const other = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, revoked } = setup([img, other]);
  await lens.attach(img, 'kindle-1', DATA_URL);
  const firstUrl = img.dataset.frankLensSrc;

  await lens.attach(other, 'kindle-1', DATA_URL);

  assert.deepEqual(revoked, [firstUrl]);
  assert.equal(lens.registeredPages().length, 1);
});

test('re-attaching the same element is a no-op', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, revoked } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);
  const url = img.dataset.frankLensSrc;

  assert.equal(await lens.attach(img, 'kindle-1', DATA_URL), true);

  assert.equal(img.dataset.frankLensSrc, url);
  assert.deepEqual(revoked, []);
});

test('webtoon retention is bounded, oldest page first', async () => {
  const images = Array.from({ length: 10 }, (_, i) =>
    makeImage({ left: 0, top: i * 700, width: 400, height: 600 }));
  const { lens, revoked } = setup(images);

  for (const [index, img] of images.entries()) {
    await lens.attach(img, `wt-${index}`, DATA_URL);
  }

  assert.equal(lens.registeredPages().length, 8);
  assert.deepEqual(Array.from(lens.registeredPages()), ['wt-2', 'wt-3', 'wt-4', 'wt-5', 'wt-6', 'wt-7', 'wt-8', 'wt-9']);
  assert.equal(revoked.length, 2, 'evicted pages release their blob');
  assert.equal(images[0].dataset.frankLensSrc, undefined);
});

test('clear releases every registration', async () => {
  const first = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const second = makeImage({ left: 0, top: 700, width: 400, height: 600 });
  const { lens, revoked } = setup([first, second]);
  await lens.attach(first, 'wt-0', DATA_URL);
  await lens.attach(second, 'wt-1', DATA_URL);

  lens.clear();

  assert.equal(lens.registeredPages().length, 0);
  assert.equal(revoked.length, 2);
});

test('disabling the lens disarms the gesture', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);
  lens.setEnabled(false);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);

  assert.equal(lens.isOpen(), false);
});

test('the smallest page under the pointer wins when they overlap', async () => {
  const spread = makeImage({ left: 0, top: 0, width: 800, height: 600 });
  const inset = makeImage({ left: 100, top: 100, width: 200, height: 200 });
  const { lens, document, fire, pointer } = setup([spread, inset]);
  await lens.attach(spread, 'wt-0', DATA_URL);
  await lens.attach(inset, 'wt-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 150, 150));
  await wait(260);

  assert.equal(lensElement(document).style.backgroundSize, '400px 400px');
});

test('touch peeks lift the lens clear of the fingertip', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, document, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 200, 500, { pointerType: 'touch' }));
  await wait(260);

  const el = lensElement(document);
  const radius = Number.parseFloat(el.style.width) / 2;
  assert.equal(Number.parseFloat(el.style.top) + radius, 500 - radius - 28);
  const [, bgY] = el.style.backgroundPosition.split(' ').map(Number.parseFloat);
  assert.equal(bgY, radius - 500 * 2, 'content still tracks the finger');
});

// --- the reader must not move while the lens is up -------------------------
// preventDefault alone left Kindle's own drag handler receiving the moves, so
// peeking dragged the page sideways under the lens.

test('an open lens takes move events away from the page', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);
  const move = pointer('pointermove', 240, 320);
  fire('pointermove', move);

  assert.equal(lens.isOpen(), true);
  assert.equal(move.defaultPrevented, true);
  assert.equal(move.propagationStopped, true, 'the reader must not see the move');
  assert.equal(move.immediatePropagationStopped, true);
});

test('compatibility mouse and touch moves are taken away too', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);
  const mouseMove = pointer('mousemove', 240, 320);
  const touchMove = pointer('touchmove', 240, 320);
  const drag = pointer('dragstart', 240, 320);
  fire('mousemove', mouseMove);
  fire('touchmove', touchMove);
  fire('dragstart', drag);

  assert.equal(mouseMove.propagationStopped, true);
  assert.equal(touchMove.propagationStopped, true);
  assert.equal(drag.defaultPrevented, true, 'the page image must not be dragged');
});

test('the page is free to move again once the peek ends', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);
  fire('pointerup', pointer('pointerup', 200, 300));
  const move = pointer('pointermove', 240, 320);
  fire('pointermove', move);

  assert.equal(move.propagationStopped, undefined, 'the reader owns the page again');
});

// --- holding before the translation arrives --------------------------------

test('holding on a page that is still translating shows nothing', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, document, fire, pointer } = setup([img]);
  lens.markPending(img);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);

  assert.equal(lens.isOpen(), false);
  assert.equal(lensElement(document), undefined, 'no empty magnifier');
});

test('holding on a page that is still translating does not turn the page', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = setup([img]);
  lens.markPending(img);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);
  fire('pointerup', pointer('pointerup', 200, 300));
  const click = pointer('click', 200, 300);
  fire('click', click);

  assert.equal(click.defaultPrevented, true, 'waiting for a render must not cost the page');
});

test('a quick tap on a page that is still translating still turns it', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = setup([img]);
  lens.markPending(img);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(60);
  fire('pointerup', pointer('pointerup', 200, 300));
  const click = pointer('click', 200, 300);
  fire('click', click);

  assert.equal(click.defaultPrevented, false);
});

test('a translation that lands mid-hold opens the lens where the pointer is', async () => {
  const img = makeImage({ left: 100, top: 50, width: 400, height: 600 });
  const { lens, document, fire, pointer } = setup([img]);
  lens.markPending(img);

  fire('pointerdown', pointer('pointerdown', 300, 350));
  await wait(260);
  assert.equal(lens.isOpen(), false);

  await lens.attach(img, 'kindle-1', DATA_URL);

  assert.equal(lens.isOpen(), true, 'the reader is still holding — just show it');
  const el = lensElement(document);
  const radius = Number.parseFloat(el.style.width) / 2;
  const [bgX] = el.style.backgroundPosition.split(' ').map(Number.parseFloat);
  assert.equal(bgX, radius - (300 - 100) * 2);
});

test('a hold that found nothing leaves no state behind for the next one', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = setup([img]);
  lens.markPending(img);

  // Hold while it is still translating, then give up.
  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);
  fire('pointerup', pointer('pointerup', 200, 300));
  fire('click', pointer('click', 200, 300));

  await lens.attach(img, 'kindle-1', DATA_URL);
  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);

  assert.equal(lens.isOpen(), true, 'the next hold must zoom normally');
});

test('a page turn clears pages that were waiting on the previous one', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = setup([img]);
  lens.markPending(img);

  lens.setActivePage('kindle-2');
  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);
  fire('pointerup', pointer('pointerup', 200, 300));
  const click = pointer('click', 200, 300);
  fire('click', click);

  assert.equal(click.defaultPrevented, false, 'a stale page must not swallow taps');
});

test('state reports why a hold did or did not open the lens', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens } = setup([img]);

  const idle = lens.state();
  assert.deepEqual({ ...idle, registered: [...idle.registered] }, {
    enabled: true, zoom: 2, activePage: '', registered: [],
    awaitingTranslation: 0, holding: false, open: false,
  });

  lens.markPending(img);
  assert.equal(lens.state().awaitingTranslation, 1);

  await lens.attach(img, 'kindle-1', DATA_URL);
  assert.equal(lens.state().awaitingTranslation, 0, 'no longer waiting once it lands');
  assert.deepEqual([...lens.state().registered], ['kindle-1']);
});

// --- the reader's own long-press must not survive the peek ------------------
// The press is let through so taps still turn pages, which means Kindle has
// begun selecting by the time the lens opens; releasing then pops its
// highlight/copy/note menu over the page.

test('opening the lens cancels the gesture the reader had started', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);

  const cancels = img.dispatched.filter((e) => e.type === 'pointercancel');
  assert.equal(cancels.length, 1);
  assert.equal(cancels[0].bubbles, true);
});

test('a peek leaves no selection behind', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer, selection } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);
  assert.ok(selection.cleared >= 1, 'cleared when the lens takes over');

  selection.isCollapsed = false;
  fire('pointermove', pointer('pointermove', 220, 310));
  assert.ok(selection.cleared >= 2, 'and again as the pointer drags');

  selection.isCollapsed = false;
  fire('pointerup', pointer('pointerup', 220, 310));
  assert.ok(selection.cleared >= 3, 'and once more on release');
});

test('a tap that never becomes a peek is left entirely alone', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer, selection } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(60);
  fire('pointerup', pointer('pointerup', 200, 300));

  assert.equal(selection.cleared, 0, 'the reader keeps its own gestures');
  assert.equal(img.dispatched.length, 0);
});

test('the cancel dispatched at the reader does not end our own peek', async () => {
  // The pointercancel that aborts Kindle's selection is dispatched at the page
  // element, and capture phase starts at the window — so without a guard our
  // own handler treats it as a release, leaving the lens on screen but frozen
  // and no longer swallowing moves.
  const img = makeImage({ left: 100, top: 50, width: 400, height: 600 });
  const { lens, document, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 300, 350));
  await wait(260);
  assert.equal(lens.isOpen(), true);

  const move = pointer('pointermove', 320, 360);
  fire('pointermove', move);

  const el = lensElement(document);
  const radius = Number.parseFloat(el.style.width) / 2;
  const [bgX] = el.style.backgroundPosition.split(' ').map(Number.parseFloat);
  assert.equal(bgX, radius - (320 - 100) * 2, 'the lens must still track the pointer');
  assert.equal(move.propagationStopped, true, 'and still take moves from the page');
});
