(function frankOverlayModule() {
  'use strict';

  function runtimeAlive() {
    try {
      return Boolean(chrome.runtime?.id);
    } catch {
      return false;
    }
  }

  if (window.FrankOverlay) {
    if (window.FrankOverlay.alive?.()) return;
    window.FrankOverlay.destroy?.();
  }

  const MIN_PAGE_SIDE_PX = 100;
  const MIN_VISIBLE_OVERLAP_PX2 = 2000;
  const EXACT_SOURCE_SCORE = 2_000_000_000;
  const SAME_TRANSLATED_PAGE_SCORE = 1_000_000_000;
  const TOP_LAYER_HIT_SCORE = 100_000_000;
  const RECT_CENTER_PENALTY = 800;
  const RECT_SIZE_PENALTY = 500_000;
  const COMPOSITOR_NUDGE_OPACITY = '0.999';

  // pageId -> object URL handed to an <img> in full-page mode. Tracked per
  // page so a long session can drop the renders it no longer shows instead of
  // holding every translated page until the tab closes.
  const objectUrls = new Map();
  const MAX_RETAINED_PAGES = 8;

  let readerMode = 'lens';

  window.addEventListener('pagehide', releaseAll);

  window.FrankOverlay = {
    alive: runtimeAlive,
    destroy() {
      releaseAll();
      window.removeEventListener('pagehide', releaseAll);
      delete window.FrankOverlay;
    },
    applyKindleResult,
    applyWebtoonResult,
    applyReaderPreferences,
    restoreOriginals,
    releasePagesExcept,
    isLensMode: () => readerMode === 'lens',
    retainedPages: () => Array.from(objectUrls.keys()),
  };

  /// Reading mode and magnification come from extension settings.
  function applyReaderPreferences(preferences = {}) {
    const nextMode = preferences.readerMode === 'full' ? 'full' : 'lens';
    const changed = nextMode !== readerMode;
    readerMode = nextMode;

    window.FrankLens?.setZoom(preferences.lensZoom ?? 2);
    window.FrankLens?.setEnabled(readerMode === 'lens');
    if (!changed) return;
    if (readerMode === 'lens') {
      // Put the reader's own artwork back before the lens takes over.
      restoreOriginals();
    } else {
      window.FrankLens?.clear();
    }
  }

  /// Undo full-page swaps. Only images that recorded an original can be
  /// restored; anything the reader has since repainted needs nothing from us.
  function restoreOriginals() {
    let restored = 0;
    for (const img of document.querySelectorAll('img[data-frank-translated="true"]')) {
      const original = img.dataset.frankOriginalSrc;
      if (!original) continue;
      img.src = original;
      delete img.dataset.frankTranslated;
      delete img.dataset.frankTranslatedSrc;
      restored += 1;
    }
    releaseAll();
    return restored;
  }

  function releasePage(pageId) {
    const url = objectUrls.get(pageId);
    if (!url) return;
    URL.revokeObjectURL(url);
    objectUrls.delete(pageId);
  }

  /// Drop every retained render except [pageId] — Kindle shows one page at a
  /// time, so the rest are dead weight the moment the reader turns.
  ///
  /// [element] is the page image just detected: handing it over lets a hold
  /// claim the gesture while that page is still being translated.
  function releasePagesExcept(pageId, element) {
    for (const retained of Array.from(objectUrls.keys())) {
      if (retained !== pageId) releasePage(retained);
    }
    window.FrankLens?.setActivePage(pageId, element);
  }

  function releaseAll() {
    for (const url of objectUrls.values()) URL.revokeObjectURL(url);
    objectUrls.clear();
  }

  /// Webtoon keeps several pages on screen at once, so retention is bounded by
  /// count rather than by which page is active.
  function retain(pageId, url) {
    releasePage(pageId);
    objectUrls.set(pageId, url);
    while (objectUrls.size > MAX_RETAINED_PAGES) {
      releasePage(objectUrls.keys().next().value);
    }
  }

  async function applyKindleResult(result) {
    if (!result?.imageDataUrl) return false;
    const capture = result.capture || {};
    const target = findKindleTarget(capture, result.pageId);
    if (!target) return false;

    if (!target.dataset.frankOriginalSrc && target.src) target.dataset.frankOriginalSrc = capture.imgSrc || target.src;

    if (readerMode === 'lens') {
      // The page keeps showing the original; only the magnifier reveals the
      // translation, so nothing is swapped into the DOM here.
      return window.FrankLens
        ? window.FrankLens.attach(target, result.pageId, result.imageDataUrl)
        : false;
    }

    if (target.dataset.frankPageId === result.pageId && target.dataset.frankTranslatedSrc) {
      return true;
    }

    const blobUrl = await objectUrlFromDataUrl(result.imageDataUrl);
    retain(result.pageId, blobUrl);
    target.src = blobUrl;
    target.dataset.frankTranslated = 'true';
    target.dataset.frankPageId = result.pageId || '';
    target.dataset.frankTranslatedSrc = blobUrl;
    if (capture.groupId) target.dataset.frankGroupId = capture.groupId;

    if (typeof target.decode === 'function') {
      target.decode().catch(() => {}).finally(() => nudgeCompositor(target));
    } else {
      nudgeCompositor(target);
    }

    return true;
  }

  function findKindleTarget(capture, pageId) {
    const expected = capture.imgSrc || '';
    const expectedRect = capture.rect;
    const readerRoot = findReaderRoot();
    let imgs = Array.from(readerRoot.querySelectorAll('img'));
    if (!imgs.length) imgs = Array.from(document.querySelectorAll('img'));
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const rootRect = readerRoot.getBoundingClientRect ? readerRoot.getBoundingClientRect() : null;

    let best = null;
    let bestScore = -Infinity;
    for (const img of imgs) {
      if (!img.src || !(img.src.startsWith('blob:') || img.dataset.frankTranslated === 'true')) continue;
      if (img.dataset.frankTranslated === 'true' && img.dataset.frankPageId
          && !samePageIgnoringForce(img.dataset.frankPageId, pageId)) {
        continue;
      }
      const rect = img.getBoundingClientRect();
      if (rect.width < MIN_PAGE_SIDE_PX || rect.height < MIN_PAGE_SIDE_PX) continue;
      let overlap = overlapAreaInViewport(rect, vw, vh);
      if (overlap < MIN_VISIBLE_OVERLAP_PX2) continue;
      if (rootRect && readerRoot !== document.body) {
        const rootOverlap = overlapArea(rect, rootRect);
        if (rootOverlap < MIN_VISIBLE_OVERLAP_PX2) continue;
        overlap = Math.min(overlap, rootOverlap);
      }
      if (!isActuallyVisible(img)) continue;

      if (expected && img.src !== expected && img.dataset.frankTranslated !== 'true') continue;
      const exact = expected && img.src === expected ? EXACT_SOURCE_SCORE : 0;
      const alreadyTranslated = img.dataset.frankTranslated === 'true' ? SAME_TRANSLATED_PAGE_SCORE : 0;
      const score = exact + alreadyTranslated + (topLayerHits(img) * TOP_LAYER_HIT_SCORE) + overlap + rectBiasScore(rect, expectedRect);
      if (score > bestScore) {
        bestScore = score;
        best = img;
      }
    }
    return best;
  }

  async function applyWebtoonResult(result) {
    if (!result?.imageDataUrl) return false;
    const capture = result.capture || {};
    const img = findWebtoonTarget(result.pageId, capture.originalSrc, capture.index);
    if (!img) return false;
    if (!img.dataset.frankOriginalSrc && (capture.originalSrc || img.src)) img.dataset.frankOriginalSrc = capture.originalSrc || img.src;

    if (readerMode === 'lens') {
      return window.FrankLens
        ? window.FrankLens.attach(img, result.pageId, result.imageDataUrl)
        : false;
    }

    const blobUrl = await objectUrlFromDataUrl(result.imageDataUrl);
    retain(result.pageId, blobUrl);
    img.src = blobUrl;
    img.dataset.frankTranslated = 'true';
    img.dataset.frankPageId = result.pageId || '';
    img.dataset.frankTranslatedSrc = blobUrl;
    if (typeof img.decode === 'function') img.decode().catch(() => {}).finally(() => nudgeCompositor(img));
    else nudgeCompositor(img);
    return true;
  }

  function findWebtoonTarget(pageId, originalSrc, index) {
    const imgs = Array.from(document.querySelectorAll('img'));
    if (originalSrc) {
      const exact = imgs.find((img) => img.src === originalSrc || img.dataset.frankOriginalSrc === originalSrc);
      if (exact) return exact;
      return null;
    }
    if (pageId?.startsWith('wt-')) {
      const wtIndex = pageId.replace('wt-', '');
      const byData = imgs.find((img) => img.dataset.frankIndex === wtIndex);
      if (byData) return byData;
    }
    const toonImgs = Array.from(document.querySelectorAll('img.toon_image'));
    const numericIndex = Number(index);
    if (Number.isInteger(numericIndex) && numericIndex >= 0 && numericIndex < toonImgs.length) {
      return toonImgs[numericIndex];
    }
    return null;
  }

  // Force-reprocess appends "-force-<timestamp>" to the captured pageId; a
  // second force on the same page appends another. Two pageIds refer to the
  // same logical page when they match after stripping all such suffixes.
  function samePageIgnoringForce(a, b) {
    const stripped = (s) => String(s || '').replace(/(?:-force-\d+)+$/, '');
    return stripped(a) === stripped(b);
  }

  function findReaderRoot() {
    return document.querySelector(
      '#kr-renderer, #kindle-reader-content, .reader-content, ' +
      '[id*="kindle-reader"], [id*="kr-renderer"], [class*="reader-content"]',
    ) || document.body;
  }

  function isActuallyVisible(el) {
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
    const opacity = Number.parseFloat(style.opacity || '1');
    return !Number.isFinite(opacity) || opacity > 0.05;
  }

  function topLayerHits(el) {
    const rect = el.getBoundingClientRect();
    const points = [
      [rect.left + rect.width / 2, rect.top + rect.height / 2],
      [rect.left + rect.width * 0.25, rect.top + rect.height / 2],
      [rect.left + rect.width * 0.75, rect.top + rect.height / 2],
    ];
    let hits = 0;
    for (const [rawX, rawY] of points) {
      const x = Math.max(0, Math.min(window.innerWidth - 1, rawX));
      const y = Math.max(0, Math.min(window.innerHeight - 1, rawY));
      const top = document.elementFromPoint(x, y);
      if (top && (top === el || el.contains(top) || top.contains(el))) hits += 1;
    }
    return hits;
  }

  function rectBiasScore(rect, expectedRect) {
    if (!expectedRect) return 0;
    const ew = Math.max(1, Number(expectedRect.width || 1));
    const eh = Math.max(1, Number(expectedRect.height || 1));
    const ecx = Number(expectedRect.x || 0) + ew / 2;
    const ecy = Number(expectedRect.y || 0) + eh / 2;
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const centerDist = Math.hypot(cx - ecx, cy - ecy);
    const sizeErr = Math.abs(rect.width - ew) / ew + Math.abs(rect.height - eh) / eh;
    return -((centerDist * RECT_CENTER_PENALTY) + (sizeErr * RECT_SIZE_PENALTY));
  }

  function overlapAreaInViewport(rect, vw, vh) {
    const ox = Math.min(rect.right, vw) - Math.max(rect.left, 0);
    const oy = Math.min(rect.bottom, vh) - Math.max(rect.top, 0);
    return ox <= 0 || oy <= 0 ? 0 : ox * oy;
  }

  function overlapArea(a, b) {
    const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    return ox <= 0 || oy <= 0 ? 0 : ox * oy;
  }

  async function objectUrlFromDataUrl(dataUrl) {
    const response = await fetch(dataUrl);
    const blob = await response.blob();
    return URL.createObjectURL(blob);
  }

  function nudgeCompositor(img) {
    img.style.opacity = COMPOSITOR_NUDGE_OPACITY;
    void img.offsetWidth;
    img.style.opacity = '';
  }
})();
