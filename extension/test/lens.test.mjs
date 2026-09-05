// The lens is what the reader actually touches: these cover the magnifier
// math, the tap-vs-hold classification that decides whether Kindle turns the
// page, and the retention rules that keep translated pages from piling up.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { loadContentScripts, makeImage, wait, lensElement, DATA_URL } from './helpers/dom-stub.mjs';

function readLensSource() {
  return readFileSync(new URL('../src/content/lens.js', import.meta.url), 'utf8');
}

function setup(images) {
  const env = loadContentScripts(['lens.js'], images);
  return { ...env, lens: env.window.FrankLens };
}

test('attach keeps the reader image untouched', async () => {
  const img = makeImage({ left: 100, top: 50, width: 400, height: 600 });
  const { lens } = setup([img]);

  assert.equal(await lens.attach(img, 'kindle-1', DATA_URL), true);
  await wait(2700);   // verification runs asynchronously

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
  await wait(2700);   // verification runs asynchronously

  assert.deepEqual(revoked, [firstUrl]);
  assert.equal(lens.registeredPages().length, 1);
});

test('re-attaching the same element is a no-op', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, revoked } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);
  const url = img.dataset.frankLensSrc;

  assert.equal(await lens.attach(img, 'kindle-1', DATA_URL), true);
  await wait(2700);   // verification runs asynchronously

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
  // Both share the render's 2:3 shape; a page whose shape no longer matches
  // its render is covered separately.
  const spread = makeImage({ left: 0, top: 0, width: 800, height: 1200 });
  const inset = makeImage({ left: 100, top: 100, width: 200, height: 300 });
  const { lens, document, fire, pointer } = setup([spread, inset]);
  await lens.attach(spread, 'wt-0', DATA_URL);
  await lens.attach(inset, 'wt-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 150, 150));
  await wait(260);

  assert.equal(lensElement(document).style.backgroundSize, '400px 600px');
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

test('holding on a page that is still translating shows a waiting ring', async () => {
  // Holding and getting nothing back is indistinguishable from a broken lens,
  // so the ring appears empty and pulses: the answer is "not yet".
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, document, fire, pointer } = setup([img]);
  lens.markPending(img);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);

  const el = lensElement(document);
  assert.equal(el.style.display, 'block');
  assert.equal(el.style.backgroundImage, 'none', 'nothing to magnify yet');
  assert.match(el.style.animation, /frankLensWaiting/);
  assert.equal(lens.isOpen(), false, 'and it is not a peek');
});

test('the waiting ring follows the pointer', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, document, fire, pointer } = setup([img]);
  lens.markPending(img);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);
  const before = lensElement(document).style.left;
  fire('pointermove', pointer('pointermove', 260, 320));

  assert.notEqual(lensElement(document).style.left, before);
});

test('the ring becomes the lens when the render lands', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, document, fire, pointer } = setup([img]);
  lens.markPending(img);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);
  assert.equal(lensElement(document).style.backgroundImage, 'none');

  await lens.attach(img, 'kindle-1', DATA_URL);

  const el = lensElement(document);
  assert.match(el.style.backgroundImage, /^url\("blob:/);
  assert.equal(el.style.animation, 'none', 'no longer waiting');
  assert.equal(lens.isOpen(), true);
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
  await wait(2700);   // verification runs asynchronously

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
  await wait(2700);   // verification runs asynchronously
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

test('the module loads before the document has a head or body', () => {
  // Content scripts run at document_start so their capture listeners are in
  // place before the reader registers its own; at that point the document is
  // little more than an <html> element.
  const env = loadContentScripts(['lens.js'], [], { bareDocument: true });

  assert.equal(typeof env.window.FrankLens?.attach, 'function');
  assert.equal(env.window.FrankLens.state().enabled, true);
});

// --- taking the press, and giving taps back --------------------------------
// Kindle starts selecting from the pointerdown itself, so nothing swallowed
// afterwards stops the highlight menu. The press has to be taken before the
// reader sees it, which means page turns have to be handed back by hand.

function pressCapturing(images) {
  const env = setup(images);
  env.lens.setPressCapture(true);
  return env;
}

test('a mouse press over a peekable page is taken from the reader', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = pressCapturing([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  const down = pointer('pointerdown', 200, 300);
  fire('pointerdown', down);

  assert.equal(down.propagationStopped, true);
  assert.equal(down.defaultPrevented, true);
});

test('the compatibility mousedown is taken too', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = pressCapturing([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  const mouseDown = pointer('mousedown', 200, 300);
  fire('mousedown', mouseDown);

  assert.equal(mouseDown.propagationStopped, true,
    'otherwise the reader starts the same gesture again');
});

test('a tap is handed back so the page still turns', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = pressCapturing([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(60);
  fire('pointerup', pointer('pointerup', 200, 300));

  const replayed = img.dispatched.filter((e) => e.frankSynthetic).map((e) => e.type);
  assert.deepEqual(replayed, ['mousedown', 'mouseup', 'click']);
  assert.equal(img.dispatched.at(-1).clientX, 200);
});

test('a peek is not handed back — that would turn the page', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = pressCapturing([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);
  assert.equal(lens.isOpen(), true);
  fire('pointerup', pointer('pointerup', 200, 300));

  assert.equal(img.dispatched.filter((e) => e.type === 'click').length, 0);
});

test('a replayed tap does not come back to us as a new press', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = pressCapturing([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(60);
  fire('pointerup', pointer('pointerup', 200, 300));
  await wait(260);

  assert.equal(lens.isOpen(), false, 'the replay must not arm another hold');
});

test('touch presses are left alone, because scrolling cannot be handed back', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = pressCapturing([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  const down = pointer('pointerdown', 200, 300, { pointerType: 'touch' });
  fire('pointerdown', down);

  assert.equal(down.propagationStopped, undefined);
  assert.equal(down.defaultPrevented, false);
});

test('without the opt-in the press is untouched', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = setup([img]);   // webtoon never opts in
  await lens.attach(img, 'wt-0', DATA_URL);

  const down = pointer('pointerdown', 200, 300);
  fire('pointerdown', down);

  assert.equal(down.propagationStopped, undefined);
});

// --- a render belongs to one page ------------------------------------------
// Kindle reuses a single <img> across books. A registration left over from the
// last book magnified that book's page over this one — white slab and all.

test('a render whose page has been replaced is dropped, not shown', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);
  await wait(2700);   // verification runs asynchronously
  assert.equal(lens.has('kindle-1'), true);

  // The reader loads a different book into the same element.
  img._rect = { left: 0, top: 0, width: 900, height: 600 };
  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);

  assert.equal(lens.isOpen(), false, 'another book must not be peekable here');
  assert.equal(lens.has('kindle-1'), false, 'and the render is released');
});

test('a page that merely resized keeps its render', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  img._rect = { left: 0, top: 0, width: 360, height: 540 };  // same shape
  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);

  assert.equal(lens.isOpen(), true);
});

test('the magnifier stops at the edge of the page instead of showing nothing', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, document, fire, pointer } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  // Press in the top-left corner: without clamping the render would be pulled
  // clear of the lens and leave a blank slab beside it.
  fire('pointerdown', pointer('pointerdown', 5, 5));
  await wait(260);

  const el = lensElement(document);
  const [bgX, bgY] = el.style.backgroundPosition.split(' ').map(Number.parseFloat);
  assert.equal(bgX, 0, 'never past the left edge');
  assert.equal(bgY, 0, 'never past the top edge');
});

test('the lens does not paint its own background over the page', () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens } = setup([img]);
  assert.equal(typeof lens.attach, 'function');
  // A backing colour is what turned a misaligned render into a white slab.
  const source = readLensSource();
  assert.ok(source.includes('background-color:transparent'));
  assert.ok(!source.includes('background-color:#fff'));
});

// --- a reinstalled extension must be able to take the page back -------------
// Reloading an extension leaves its content scripts running in open tabs with
// a dead runtime. Chrome injects the new copy, but the old modules still own
// the page's listeners — which is how a stale pipeline kept being submitted
// after a reinstall.

test('a live instance keeps the page', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, sandbox } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  loadContentScripts(['lens.js'], [img], { sandbox });   // a second injection

  assert.equal(sandbox.window.FrankLens, lens, 'the running instance stays');
  assert.equal(lens.has('kindle-1'), true, 'and keeps what it registered');
});

test('a dead instance stands down and releases its renders', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, revoked, sandbox } = setup([img]);
  await lens.attach(img, 'kindle-1', DATA_URL);

  sandbox.chrome.runtime.id = undefined;      // the extension was reinstalled
  assert.equal(lens.alive(), false);
  lens.destroy();

  assert.deepEqual(revoked.length, 1, 'its renders are freed');
  assert.equal(sandbox.window.FrankLens, undefined, 'and the page is handed back');
});

test('an orphaned instance stops swallowing presses', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, fire, pointer, sandbox } = setup([img]);
  lens.setPressCapture(true);
  await lens.attach(img, 'kindle-1', DATA_URL);

  sandbox.chrome.runtime.id = undefined;
  lens.destroy();

  const down = pointer('pointerdown', 200, 300);
  fire('pointerdown', down);
  assert.equal(down.propagationStopped, undefined,
    'a dead instance must not keep taking the reader\'s presses');
});

// --- the render has to depict the page it is bound to -----------------------
// Every other link in the chain — blob URL, page id, element identity — has at
// some point pointed at the wrong page, and a real render of the wrong page
// reads as a translation of what is on screen. So compare them directly.

test('a render of this page is bound', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const env = loadContentScripts(['lens.js'], [img], {
    pixels: { 'blob:page': 7, 'blob:frank-1': 7 },   // a page and its render
  });
  const lens = env.window.FrankLens;

  await lens.attach(img, 'kindle-1', DATA_URL);
  await wait(2700);   // verification runs asynchronously

  assert.equal(lens.has('kindle-1'), true);
});

test('a render of another page is discarded, not shown', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const env = loadContentScripts(['lens.js'], [img], {
    pixels: { 'blob:page': 7, 'blob:frank-1': 31 },  // two different pages
  });
  const lens = env.window.FrankLens;

  await lens.attach(img, 'kindle-1', DATA_URL);
  await wait(2700);   // verification runs asynchronously

  assert.equal(lens.has('kindle-1'), false, 'better nothing than the wrong page');
  assert.equal(env.revoked.length, 1, 'and it is freed');
});

test('unreadable pixels leave the binding alone', async () => {
  // A tainted canvas must not cost a good render.
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const env = loadContentScripts(['lens.js'], [img], {});   // no canvas pixels
  const lens = env.window.FrankLens;

  await lens.attach(img, 'kindle-1', DATA_URL);
  await wait(2700);   // verification runs asynchronously

  assert.equal(lens.has('kindle-1'), true);
});

test('a page that reads as blank is not called a mismatch', async () => {
  // An element that has not decoded yet draws as a flat canvas, which carries
  // no structure to compare. That is unreadable, not "a different page", and
  // must not cost a good render.
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const env = loadContentScripts(['lens.js'], [img], {
    pixels: { 'blob:page': 0, 'blob:frank-1': 7 },   // 0 renders flat
  });

  await env.window.FrankLens.attach(img, 'kindle-1', DATA_URL);
  await wait(2700);   // verification runs asynchronously

  assert.equal(env.window.FrankLens.has('kindle-1'), true);
});

test('an inverted render is named as a pipeline mismatch', async () => {
  // A manga pipeline on a prose page clears "balloons" and redraws the text,
  // which comes back as the page inverted. That is a setting to change, not a
  // failure to retry, so the message has to say which.
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const warned = [];
  const env = loadContentScripts(['lens.js'], [img], {
    pixels: { 'blob:page': 7, 'blob:frank-1': -7 },   // the same page, inverted
    onWarn: (message) => warned.push(message),
  });

  await env.window.FrankLens.attach(img, 'kindle-1', DATA_URL);
  await wait(2700);   // verification runs asynchronously

  assert.equal(env.window.FrankLens.has('kindle-1'), false);
  assert.match(warned.join(' '), /text book run through a manga pipeline/);
});

test('compare answers what a render actually matches', async () => {
  // The measure that separates "the page moved" from "this is someone else's
  // render": the same one the binding check uses, exposed for the caller.
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const env = loadContentScripts(['lens.js'], [img], {
    pixels: { 'blob:a': 7, 'blob:b': 7, 'blob:c': 31 },
  });
  const lens = env.window.FrankLens;

  const same = await lens.compare('blob:a', 'blob:b');
  const different = await lens.compare('blob:a', 'blob:c');

  assert.ok(same <= 0.5, `the same page should compare low, got ${same}`);
  assert.ok(different > 0.5, `different pages should compare high, got ${different}`);
});

test('a mismatch hands back a render the caller can still load', async () => {
  // release() revokes the object URL, so reporting one meant the caller's
  // follow-up comparison silently loaded nothing and said nothing.
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const env = loadContentScripts(['lens.js'], [img], {
    pixels: { 'blob:page': 7, 'blob:frank-1': 31 },
  });
  const seen = [];
  env.window.FrankLens.onRenderMismatch((detail) => seen.push(detail));

  await env.window.FrankLens.attach(img, 'kindle-1', DATA_URL);
  await wait(2700);   // verification runs asynchronously

  assert.equal(seen.length, 1);
  assert.equal(seen[0].renderUrl, DATA_URL, 'a data URL survives the release');
  assert.ok(!env.revoked.includes(seen[0].renderUrl));
  assert.match(seen[0].natural, /^\d+x\d+$/, 'and says what the element holds');
});

test('a ring that nothing will fill stops being shown', async () => {
  // A page whose render never arrives must stop promising one: a ring that
  // waits forever is a worse answer than nothing, and it was left on screen
  // permanently once submission gave up.
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, document, fire, pointer } = setup([img]);
  lens.markPending(img);
  lens.clearPending();

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);

  assert.equal(lensElement(document)?.style.display ?? 'none', 'none',
    'no ring once nothing is in flight');
});

test('a hold still in progress is ended when the wait is abandoned', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, document, fire, pointer } = setup([img]);
  lens.markPending(img);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);
  assert.equal(lensElement(document).style.display, 'block', 'the ring is up');

  lens.clearPending();

  assert.equal(lensElement(document).style.display, 'none');
});

test('releasing removes the waiting ring, not just the lens', async () => {
  // closeLens used to return early when the lens was not "open" — which the
  // ring never sets — so the circle stayed on screen after the reader let go.
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { lens, document, fire, pointer } = setup([img]);
  lens.markPending(img);

  fire('pointerdown', pointer('pointerdown', 200, 300));
  await wait(260);
  assert.equal(lensElement(document).style.display, 'block', 'the ring is up');

  fire('pointerup', pointer('pointerup', 200, 300));

  assert.equal(lensElement(document).style.display, 'none',
    'letting go must take the ring with it');
});

test('the ring becomes the lens only once the render is checked', async () => {
  // Opening as soon as the render arrives means opening before it is known to
  // depict the page — and then closing again when it turns out not to, which
  // reads as the lens flickering out from under the reader.
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const env = loadContentScripts(['lens.js'], [img], {
    pixels: { 'blob:page': 7, 'blob:frank-1': 31 },   // the render will be refused
  });
  const lens = env.window.FrankLens;
  lens.markPending(img);

  env.fire('pointerdown', env.pointer('pointerdown', 200, 300));
  await wait(260);
  await lens.attach(img, 'kindle-1', DATA_URL);
  await wait(100);

  const el = lensElement(env.document);
  assert.equal(el.style.backgroundImage, 'none',
    'a render that has not been checked must not be shown');
  assert.match(el.style.animation, /frankLensWaiting/, 'the ring stays until it is');
});

// The stub's pages are either identical or maximally unlike, so the band
// between them cannot be expressed here. Real pages, real renders and real
// browser downscaling are measured in test/browser/signature.test.mjs, which
// is where the thresholds come from.

test('a render from another book is still refused', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const env = loadContentScripts(['lens.js'], [img], {
    pixels: { 'blob:page': 3, 'blob:frank-1': 211 },   // nothing alike
  });

  await env.window.FrankLens.attach(img, 'kindle-1', DATA_URL);
  await wait(2700);

  assert.equal(env.window.FrankLens.has('kindle-1'), false,
    'a page from another book is never a translation of this one');
});
