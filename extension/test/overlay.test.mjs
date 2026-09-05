// Overlay decides which presentation a finished translation gets, and owns the
// object URLs behind it. Both matter to the reader: the wrong branch either
// replaces the page the user wanted to read in Japanese, or leaks a few MB per
// page for the life of the tab.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadContentScripts, makeImage, DATA_URL } from './helpers/dom-stub.mjs';

function setup(images) {
  const env = loadContentScripts(['lens.js', 'overlay.js'], images);
  return { ...env, lens: env.window.FrankLens, overlay: env.window.FrankOverlay };
}

const kindleResult = (pageId, imgSrc = 'blob:page') => ({
  pageId,
  imageDataUrl: DATA_URL,
  capture: { imgSrc, rect: { x: 0, y: 0, width: 400, height: 600 } },
});

test('lens is the default presentation and leaves the page alone', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { overlay, lens } = setup([img]);

  assert.equal(await overlay.applyKindleResult(kindleResult('kindle-1')), true);

  assert.equal(overlay.isLensMode(), true);
  assert.equal(img.src, 'blob:page', 'the reader keeps showing the original');
  assert.equal(img.dataset.frankTranslated, undefined);
  assert.equal(lens.has('kindle-1'), true);
});

test('full-page mode swaps the image and remembers the original', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { overlay } = setup([img]);
  overlay.applyReaderPreferences({ readerMode: 'full' });

  assert.equal(await overlay.applyKindleResult(kindleResult('kindle-1')), true);

  assert.match(img.src, /^blob:frank-/);
  assert.equal(img.dataset.frankTranslated, 'true');
  assert.equal(img.dataset.frankOriginalSrc, 'blob:page');
});

test('switching back to lens restores the page and frees the render', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { overlay, revoked } = setup([img]);
  overlay.applyReaderPreferences({ readerMode: 'full' });
  await overlay.applyKindleResult(kindleResult('kindle-1'));
  const swapped = img.src;

  overlay.applyReaderPreferences({ readerMode: 'lens' });

  assert.equal(img.src, 'blob:page');
  assert.equal(img.dataset.frankTranslated, undefined);
  assert.deepEqual(revoked, [swapped]);
  assert.deepEqual(Array.from(overlay.retainedPages()), []);
});

test('switching to full-page mode drops lens registrations', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { overlay, lens, revoked } = setup([img]);
  await overlay.applyKindleResult(kindleResult('kindle-1'));

  overlay.applyReaderPreferences({ readerMode: 'full' });

  assert.equal(lens.has('kindle-1'), false);
  assert.equal(revoked.length, 1);
});

test('preferences drive lens zoom and arming', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { overlay, lens, fire, pointer, document } = setup([img]);
  await overlay.applyKindleResult(kindleResult('kindle-1'));

  overlay.applyReaderPreferences({ readerMode: 'lens', lensZoom: 3 });
  fire('pointerdown', pointer('pointerdown', 200, 300));
  await new Promise((r) => setTimeout(r, 260));

  assert.equal(lens.isOpen(), true);
  const el = document.body.children.find((c) => c.id === '__frankLens');
  assert.equal(el.style.backgroundSize, '1200px 1800px');
});

test('turning the page frees the previous render in full-page mode too', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 });
  const { overlay, revoked } = setup([img]);
  overlay.applyReaderPreferences({ readerMode: 'full' });
  await overlay.applyKindleResult(kindleResult('kindle-1'));
  const first = img.src;

  overlay.releasePagesExcept('kindle-2');

  assert.deepEqual(revoked, [first]);
  assert.deepEqual(Array.from(overlay.retainedPages()), []);
});

test('webtoon retention in full-page mode is bounded', async () => {
  const images = Array.from({ length: 10 }, (_, i) =>
    makeImage({ left: 0, top: i * 700, width: 400, height: 600 }, { src: `https://naver/${i}.jpg` }));
  const { overlay, revoked } = setup(images);
  overlay.applyReaderPreferences({ readerMode: 'full' });

  for (const [index] of images.entries()) {
    await overlay.applyWebtoonResult({
      pageId: `wt-${index}`,
      imageDataUrl: DATA_URL,
      capture: { originalSrc: `https://naver/${index}.jpg`, index },
    });
  }

  assert.equal(overlay.retainedPages().length, 8);
  assert.equal(revoked.length, 2);
});

test('webtoon results land on the image that was captured', async () => {
  const first = makeImage({ left: 0, top: 0, width: 400, height: 600 }, { src: 'https://naver/1.jpg' });
  const second = makeImage({ left: 0, top: 700, width: 400, height: 600 }, { src: 'https://naver/2.jpg' });
  const { overlay, lens } = setup([first, second]);

  await overlay.applyWebtoonResult({
    pageId: 'wt-1',
    imageDataUrl: DATA_URL,
    capture: { originalSrc: 'https://naver/2.jpg', index: 1 },
  });

  assert.equal(second.dataset.frankLensPageId, 'wt-1');
  assert.equal(first.dataset.frankLensPageId, undefined);
  assert.equal(lens.has('wt-1'), true);
});

test('a result with no matching page on screen is reported as unapplied', async () => {
  const img = makeImage({ left: 0, top: 0, width: 400, height: 600 }, { src: 'blob:other' });
  const { overlay } = setup([img]);

  assert.equal(await overlay.applyKindleResult(kindleResult('kindle-1', 'blob:missing')), false);
});
