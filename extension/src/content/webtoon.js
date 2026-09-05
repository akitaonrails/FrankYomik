(function frankWebtoonModule() {
  'use strict';

  if (window.FrankWebtoon) return;

  const ALLOWED_IMAGE_HOSTS = new Set([
    'comic.naver.com',
    'm.comic.naver.com',
    'image-comic.pstatic.net',
    'webtoon-phinf.pstatic.net',
    'swebtoon-phinf.pstatic.net',
  ]);
  const MAX_CONCURRENT = 3;
  // A webtoon page takes tens of seconds to translate, so the queue has to run
  // further ahead of the scroll than "just off screen" to stay useful.
  const NEARBY_PREFETCH_PX = 2400;
  const RESCAN_MS = 2000;
  const MAX_DEBUG_ENTRIES = 20;
  const MAX_DEBUG_PAYLOADS = 3;

  let started = false;
  let settings = {};
  let observer = null;
  let mutationObserver = null;
  let scanTimer = null;
  let active = 0;
  const queue = [];
  const submitted = new Set();
  const pageById = new Map();
  const debugEntries = new Map();

  window.FrankWebtoon = { start };

  function start(nextSettings) {
    if (started) return;
    started = true;
    settings = nextSettings || {};
    installMessageListener();
    installObservers();
    scanAndQueue();
    scanTimer = window.setInterval(scanAndQueue, RESCAN_MS);
    window.addEventListener('pagehide', cleanup, { once: true });
    console.info('[Frank] Naver Webtoon strategy started');
  }

  function cleanup() {
    observer?.disconnect();
    mutationObserver?.disconnect();
    if (scanTimer) window.clearInterval(scanTimer);
  }

  function installMessageListener() {
    chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
      if (message?.type === 'FRANK_JOB_COMPLETE' && message.site === 'webtoon') handleJobComplete(message);
      if (message?.type === 'FRANK_JOB_FAILED' && message.site === 'webtoon') handleJobFailed(message);
      if (message?.type === 'FRANK_FORCE_REPROCESS_CURRENT') {
        if (!frameHostsWebtoonViewer()) return false;
        forceReprocessCurrent().then(sendResponse).catch((error) => sendResponse({ ok: false, error: error.message || String(error) }));
        return true;
      }
      if (message?.type === 'FRANK_EXPORT_DEBUG_PAIR') {
        if (!frameHostsWebtoonViewer()) return false;
        exportDebugPair().then(sendResponse).catch((error) => sendResponse({ ok: false, error: error.message || String(error) }));
        return true;
      }
      return false;
    });
  }

  // Only the frame that actually contains the Naver Webtoon viewer should reply
  // to popup-driven actions — empty sub-frames race their fast failures against
  // the real frame's work and win.
  function frameHostsWebtoonViewer() {
    return !!document.querySelector(
      '#comic_view_area, .wt_viewer, #sectionContWide, img.toon_image',
    );
  }

  function installObservers() {
    observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const img = entry.target;
        const index = findPageImages().indexOf(img);
        if (index >= 0) queueImage(img, index, 'visible');
      }
      pumpQueue();
    }, {
      rootMargin: rootMarginForPrefetch(),
      threshold: 0.05,
    });

    mutationObserver = new MutationObserver(() => debounceScan());
    if (document.body) mutationObserver.observe(document.body, { childList: true, subtree: true });
  }

  let scanDebounce = null;
  function debounceScan() {
    if (scanDebounce) window.clearTimeout(scanDebounce);
    scanDebounce = window.setTimeout(scanAndQueue, 350);
  }

  function scanAndQueue() {
    if (!settings.configured || settings.webtoonEnabled === false) return;
    const imgs = findPageImages();
    imgs.forEach((img, index) => {
      img.dataset.frankIndex = String(index);
      if (!img.dataset.frankOriginalSrc) {
        const src = imageSrc(img);
        if (src) img.dataset.frankOriginalSrc = src;
      }
      observer?.observe(img);

      if (settings.webtoonPrefetch === 'episode') {
        queueImage(img, index, 'episode');
      } else if (settings.webtoonPrefetch !== 'off' && isNearViewport(img, NEARBY_PREFETCH_PX)) {
        queueImage(img, index, 'nearby');
      } else if (isInViewport(img)) {
        queueImage(img, index, 'visible');
      }

      if ((!img.complete || img.naturalWidth <= 0) && img.dataset.frankLoadListener !== 'true') {
        img.dataset.frankLoadListener = 'true';
        img.addEventListener('load', () => {
          const currentIndex = findPageImages().indexOf(img);
          if (currentIndex >= 0 && (isNearViewport(img, NEARBY_PREFETCH_PX) || settings.webtoonPrefetch === 'episode')) {
            queueImage(img, currentIndex, 'loaded');
            pumpQueue();
          }
        }, { once: true });
      }
    });
    pumpQueue();
  }

  function findPageImages() {
    let imgs = Array.from(document.querySelectorAll('img.toon_image'));
    if (imgs.length) return imgs;

    for (const selector of ['#comic_view_area img', '.wt_viewer img', '#sectionContWide img']) {
      imgs = Array.from(document.querySelectorAll(selector));
      if (imgs.length) return imgs;
    }

    return Array.from(document.querySelectorAll('img')).filter((img) => {
      const w = img.naturalWidth || img.width || 0;
      const h = img.naturalHeight || img.height || 0;
      return w > 600 && h > 400;
    });
  }

  function queueImage(img, index, reason) {
    const src = imageSrc(img);
    if (!src || !isAllowedImageUrl(src)) return;
    if ((img.naturalWidth || img.width || 0) < 200 || (img.naturalHeight || img.height || 0) < 200) return;

    const pageId = `wt-${index}`;
    if (submitted.has(pageId) || queue.some((item) => item.pageId === pageId)) return;
    submitted.add(pageId);
    img.dataset.frankIndex = String(index);
    img.dataset.frankOriginalSrc = src;
    pageById.set(pageId, { img, src, index });
    queue.push({ img, src, index, pageId, reason });
  }

  function pumpQueue() {
    while (active < MAX_CONCURRENT && queue.length) {
      const item = queue.shift();
      active += 1;
      submitImage(item).finally(() => {
        active = Math.max(0, active - 1);
        pumpQueue();
      });
    }
  }

  async function submitImage(item) {
    try {
      const imageDataUrl = await captureImage(item.src);
      rememberDebug(item.pageId, { pageId: item.pageId, site: 'webtoon', index: item.index, originalSrc: item.src, originalDataUrl: imageDataUrl, capture: { originalSrc: item.src, index: item.index, pageMode: 'single' } });
      const response = await chrome.runtime.sendMessage({
        type: 'SUBMIT_CAPTURE',
        site: 'webtoon',
        pageId: item.pageId,
        priority: item.reason === 'visible' ? 'high' : 'low',
        metadata: parseWebtoonMetadata(item.index),
        capture: {
          originalSrc: item.src,
          index: item.index,
          pageMode: 'single',
        },
        imageDataUrl,
      });
      if (!response?.ok) throw new Error(response?.error || 'submit failed');
      if (response.status === 'completed') await applyWebtoon(response);
    } catch (error) {
      submitted.delete(item.pageId);
      console.warn('[Frank] Webtoon submit failed:', error);
    }
  }

  async function captureImage(src) {
    try {
      const response = await fetch(src, {
        method: 'GET',
        credentials: 'include',
        cache: 'force-cache',
      });
      if (!response.ok) throw new Error(`image fetch failed: HTTP ${response.status}`);
      const blob = await response.blob();
      if (!blob.type.startsWith('image/')) throw new Error(`unexpected image type: ${blob.type || 'unknown'}`);
      return blobToDataUrl(blob);
    } catch (error) {
      const fallback = await chrome.runtime.sendMessage({ type: 'FETCH_WEBTOON_IMAGE', src });
      if (!fallback?.ok) throw new Error(fallback?.error || error.message || 'image fetch failed');
      return fallback.imageDataUrl;
    }
  }

  async function handleJobComplete(message) {
    await applyWebtoon(message);
  }

  function handleJobFailed(message) {
    submitted.delete(message.pageId);
    console.warn('[Frank] Webtoon job failed:', message.error);
  }

  async function applyWebtoon(message) {
    const known = pageById.get(message.pageId);
    if (known?.img && known.src && !message.capture?.originalSrc) {
      message.capture = { ...(message.capture || {}), originalSrc: known.src, index: known.index };
    }
    const ok = await window.FrankOverlay?.applyWebtoonResult(message);
    if (ok) {
      rememberDebug(message.pageId, {
        pageId: message.pageId,
        site: 'webtoon',
        index: message.capture?.index,
        originalSrc: message.capture?.originalSrc,
        translatedDataUrl: message.imageDataUrl,
        capture: message.capture,
      });
    }
    return ok;
  }

  async function forceReprocessCurrent() {
    if (!settings.configured || settings.webtoonEnabled === false) throw new Error('Webtoon support is not enabled or configured.');
    scanAndQueue();
    const img = findCurrentVisibleImage();
    if (!img) throw new Error('No visible Naver Webtoon image found.');
    const index = findPageImages().indexOf(img);
    const originalSrc = img.dataset.frankOriginalSrc || imageSrc(img);
    if (!originalSrc || !isAllowedImageUrl(originalSrc)) throw new Error('Current webtoon original image URL is unavailable or not allowed.');
    const pageId = `wt-${index >= 0 ? index : 'current'}-force-${Date.now()}`;
    const imageDataUrl = await captureImage(originalSrc);
    rememberDebug(pageId, { pageId, site: 'webtoon', index, originalSrc, originalDataUrl: imageDataUrl, capture: { originalSrc, index, pageMode: 'single' } });
    const response = await chrome.runtime.sendMessage({
      type: 'SUBMIT_CAPTURE',
      site: 'webtoon',
      pageId,
      priority: 'high',
      metadata: parseWebtoonMetadata(index >= 0 ? index : 0),
      capture: { originalSrc, index, pageMode: 'single' },
      imageDataUrl,
      force: true,
    });
    if (!response?.ok) throw new Error(response?.error || 'submit failed');
    if (response.status === 'completed') await applyWebtoon(response);
    return { ok: true, site: 'webtoon', pageId, message: 'Forced webtoon reprocess submitted.' };
  }

  async function exportDebugPair() {
    scanAndQueue();
    const img = findCurrentVisibleImage();
    if (!img) throw new Error('No visible Naver Webtoon image found.');
    const entry = debugEntryForImage(img);
    const originalSrc = img.dataset.frankOriginalSrc || entry?.originalSrc || imageSrc(img);
    const originalDataUrl = entry?.originalDataUrl || (originalSrc && isAllowedImageUrl(originalSrc) ? await captureImage(originalSrc) : null);
    const translatedDataUrl = entry?.translatedDataUrl || (img.dataset.frankTranslated === 'true' ? await dataUrlFromSrc(img.src) : null);
    if (!originalDataUrl) throw new Error('Original debug image unavailable for the current webtoon image.');
    if (!translatedDataUrl) throw new Error('Translated debug image unavailable for the current webtoon image.');
    return {
      ok: true,
      site: 'webtoon',
      pageId: entry?.pageId || img.dataset.frankPageId || `wt-${img.dataset.frankIndex || 'current'}`,
      sourceUrl: location.href,
      capture: entry?.capture || { originalSrc, index: img.dataset.frankIndex, pageMode: 'single' },
      metadata: parseWebtoonMetadata(Number(img.dataset.frankIndex || 0)),
      originalDataUrl,
      translatedDataUrl,
    };
  }

  function parseWebtoonMetadata(index) {
    const url = new URL(location.href);
    return {
      title: url.searchParams.get('titleId') || 'webtoon',
      chapter: url.searchParams.get('no') || '0',
      pageNumber: String(index),
      sourceUrl: location.href,
    };
  }

  function imageSrc(img) {
    return img.currentSrc || img.src || img.dataset.src || img.getAttribute('data-lazy-src') || '';
  }

  function isAllowedImageUrl(value) {
    try {
      const url = new URL(value, location.href);
      const host = url.hostname.toLowerCase();
      if (url.protocol !== 'https:') return false;
      return ALLOWED_IMAGE_HOSTS.has(host);
    } catch {
      return false;
    }
  }

  function isInViewport(img) {
    const rect = img.getBoundingClientRect();
    return rect.bottom > 0 && rect.top < window.innerHeight && rect.right > 0 && rect.left < window.innerWidth;
  }

  function isNearViewport(img, margin) {
    const rect = img.getBoundingClientRect();
    return rect.bottom > -margin && rect.top < window.innerHeight + margin && rect.right > -200 && rect.left < window.innerWidth + 200;
  }

  function findCurrentVisibleImage() {
    let best = null;
    let bestArea = 0;
    for (const img of findPageImages()) {
      const rect = img.getBoundingClientRect();
      const ox = Math.min(rect.right, window.innerWidth) - Math.max(rect.left, 0);
      const oy = Math.min(rect.bottom, window.innerHeight) - Math.max(rect.top, 0);
      const area = ox <= 0 || oy <= 0 ? 0 : ox * oy;
      if (area > bestArea) {
        bestArea = area;
        best = img;
      }
    }
    return best;
  }

  function rememberDebug(key, value) {
    if (!key) return;
    const previous = debugEntries.get(key) || {};
    const entry = { ...previous, ...value, pageId: value.pageId || previous.pageId || key, updatedAt: Date.now() };
    debugEntries.set(key, entry);
    if (entry.originalSrc) debugEntries.set(entry.originalSrc, entry);
    while (debugEntries.size > MAX_DEBUG_ENTRIES * 2) {
      const oldestKey = debugEntries.keys().next().value;
      debugEntries.delete(oldestKey);
    }
    trimDebugPayloads();
  }

  // Debug payloads are whole-page data URLs — megabytes each. Only the pages
  // the debug export can still act on need them; the rest keep their metadata.
  function trimDebugPayloads() {
    const unique = [];
    const seen = new Set();
    for (const entry of debugEntries.values()) {
      if (seen.has(entry)) continue;
      seen.add(entry);
      unique.push(entry);
    }
    unique.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
    for (const entry of unique.slice(MAX_DEBUG_PAYLOADS)) {
      delete entry.originalDataUrl;
      delete entry.translatedDataUrl;
      delete entry.originalSides;
    }
  }


  function debugEntryForImage(img) {
    const pageId = img.dataset.frankPageId || (img.dataset.frankIndex ? `wt-${img.dataset.frankIndex}` : '');
    return debugEntries.get(pageId)
      || debugEntries.get(img.dataset.frankOriginalSrc)
      || debugEntries.get(imageSrc(img))
      || null;
  }

  async function dataUrlFromSrc(src) {
    if (!src) return null;
    if (src.startsWith('data:image/')) return src;
    const response = await fetch(src);
    if (!response.ok) return null;
    return blobToDataUrl(await response.blob());
  }

  function rootMarginForPrefetch() {
    if (settings.webtoonPrefetch === 'off') return '0px';
    if (settings.webtoonPrefetch === 'episode') return '4000px 0px';
    return `${NEARBY_PREFETCH_PX}px 0px`;
  }

  function blobToDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(reader.error || new Error('failed to read webtoon image'));
      reader.onload = () => resolve(String(reader.result));
      reader.readAsDataURL(blob);
    });
  }
})();
