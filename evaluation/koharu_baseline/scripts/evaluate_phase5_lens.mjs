#!/usr/bin/env node
// Exercise the production extension lens in real Chromium at every supported zoom.

import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repository = resolve(here, '../../..');
const { browserUsable, withRealInput } = await import(
  join(repository, 'extension/test/browser/chromium.mjs')
);

if (!(await browserUsable())) {
  console.error('SKIP: a driveable Chromium and Node >=21 are required');
  process.exitCode = 77;
} else {
  const source = readFileSync(
    join(repository, 'extension/src/content/lens.js'), 'utf8',
  );
  const pageBytes = readFileSync(
    join(repository, 'extension/test/browser/fixtures/page.png'),
  );
  const pageData = `data:image/png;base64,${pageBytes.toString('base64')}`;
  const renderBytes = readFileSync(
    join(repository, 'extension/test/browser/fixtures/render-of-page.png'),
  );
  const renderData = `data:image/png;base64,${renderBytes.toString('base64')}`;

  function setup({ lensSource, image, render, zoom }) {
    window.chrome = {
      runtime: { id: 'phase5', sendMessage: () => Promise.resolve() },
    };
    document.body.style.margin = '0';
    const page = document.createElement('img');
    page.id = 'page';
    // Preserve the fixture aspect ratio. The production lens deliberately
    // releases a registration when its element is reused for another shape.
    page.style.cssText = 'position:absolute;left:0;top:0;width:700px;height:400px';
    return new Promise((resolveSetup, rejectSetup) => {
      page.addEventListener('load', async () => {
        document.body.appendChild(page);
        // This is the production module under evaluation.
        // eslint-disable-next-line no-eval
        eval(lensSource);
        window.FrankLens.setZoom(zoom);
        await window.FrankLens.attach(page, 'phase5-page', render);
        const deadline = Date.now() + 5_000;
        while (!window.FrankLens.has('phase5-page')) {
          if (Date.now() >= deadline) {
            rejectSetup(new Error('production lens did not verify the fixture'));
            return;
          }
          await new Promise((resolveWait) => setTimeout(resolveWait, 25));
        }
        resolveSetup(true);
      });
      page.src = image;
    });
  }

  const at = (type, x, y, extra = {}) => ({
    type, x, y, button: 'left', clickCount: 1,
    buttons: type === 'mouseMoved' ? 1 : 0, ...extra,
  });

  const observations = [];
  for (const zoom of [1.5, 2, 3]) {
    const observed = await withRealInput({
      setup,
      actions: [
        at('mouseMoved', 350, 200, { buttons: 0 }),
        at('mousePressed', 350, 200, { buttons: 1, pause: 350 }),
      ],
      read: () => {
        const lens = document.getElementById('__frankLens');
        const style = getComputedStyle(lens);
        const rect = lens.getBoundingClientRect();
        return {
          open: window.FrankLens.isOpen(),
          zoom: window.FrankLens.state().zoom,
          viewport_px: [window.innerWidth, window.innerHeight],
          diameter_px: Math.round(rect.width),
          left_px: Math.round(rect.left),
          top_px: Math.round(rect.top),
          background_size: style.backgroundSize,
          background_position: style.backgroundPosition,
          pointer_events: style.pointerEvents,
          z_index: Number(style.zIndex),
        };
      },
    }, { lensSource: source, image: pageData, render: renderData, zoom });
    if (!observed.open || observed.zoom !== zoom) {
      throw new Error(`lens failed at ${zoom}x: ${JSON.stringify(observed)}`);
    }
    observations.push(observed);
  }

  process.stdout.write(`${JSON.stringify({
    page_fixture: 'extension/test/browser/fixtures/page.png',
    render_fixture: 'extension/test/browser/fixtures/render-of-page.png',
    requested_browser_window_px: [1000, 800],
    page_css_px: [700, 400],
    pointer_css_px: [350, 200],
    observations,
  }, null, 2)}\n`);
}
