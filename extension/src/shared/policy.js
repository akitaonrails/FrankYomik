// Shared extension policy: limits, URL validation, and pure sanitizers.
// Keep Chrome API access out of this file so it can be unit-tested in Node.

export const TIMING = Object.freeze({
  pollDelayMs: 3_000,
  jobTimeoutMs: 5 * 60_000,
  healthTimeoutMs: 10_000,
  submitTimeoutMs: 30_000,
  statusTimeoutMs: 10_000,
  imageDownloadTimeoutMs: 45_000,
  webtoonFetchTimeoutMs: 30_000,
});

export const SIZE_LIMITS = Object.freeze({
  maxCacheEntries: 200,
  maxCaptureDataUrlChars: 28 * 1024 * 1024,
  maxResultImageBytes: 25 * 1024 * 1024,
  maxCacheBytes: 200 * 1024 * 1024,
  maxDiagnostics: 80,
});

export const WEBTOON_BACKGROUND_FETCH_HOSTS = new Set([
  'image-comic.pstatic.net',
  'webtoon-phinf.pstatic.net',
  'swebtoon-phinf.pstatic.net',
]);

export function normalizeApiImageUrl(settingsOrBaseUrl, imageUrl) {
  const apiBaseUrl = typeof settingsOrBaseUrl === 'string'
    ? settingsOrBaseUrl
    : settingsOrBaseUrl?.apiBaseUrl;
  const api = new URL(apiBaseUrl);
  const resolved = String(imageUrl || '').startsWith('http')
    ? new URL(imageUrl)
    : new URL(imageUrl, apiBaseUrl);
  if (resolved.origin !== api.origin) {
    throw new Error('refusing to download cross-origin result image');
  }
  return resolved.toString();
}

export function cacheKeyFor(apiBaseUrl, cachePipeline, sourceHash) {
  const origin = new URL(apiBaseUrl).origin;
  return `${origin}|${cachePipeline}|${sourceHash}`;
}

export function validateAllowedWebtoonImageUrl(value) {
  let url;
  try {
    url = new URL(String(value || ''));
  } catch {
    throw new Error('invalid webtoon image URL');
  }
  if (url.protocol !== 'https:') throw new Error('webtoon image URL must use https');
  if (!WEBTOON_BACKGROUND_FETCH_HOSTS.has(url.hostname.toLowerCase())) {
    throw new Error('webtoon image host is not allowed');
  }
  return url;
}

export function sanitizeMetadata(metadata = {}, fallbackUrl = '') {
  const sourceSite = safeText(metadata.sourceSite || metadata.source_site, 40).toLowerCase();
  const isKindle = sourceSite === 'kindle';
  return {
    title: safeText(metadata.title, 120),
    chapter: safeText(metadata.chapter, 60),
    page_number: safeText(metadata.pageNumber || metadata.page_number, 60),
    source_url: safeUrl(metadata.sourceUrl || metadata.source_url || fallbackUrl),
    source_site: isKindle ? 'kindle' : '',
    latest_group: isKindle ? safeText(metadata.latestGroup || metadata.latest_group, 160) : '',
    latest_token: isKindle ? safeText(metadata.latestToken || metadata.latest_token, 160) : '',
    latest_seq: isKindle ? safePositiveInteger(metadata.latestSeq || metadata.latest_seq) : '',
  };
}

export function sanitizeCapture(capture = {}) {
  if (!capture || typeof capture !== 'object') return {};
  const rect = capture.rect && typeof capture.rect === 'object' ? capture.rect : capture.readerRect;
  return {
    imgSrc: safeText(capture.imgSrc, 2048),
    originalSrc: safeText(capture.originalSrc, 2048),
    groupId: safeText(capture.groupId, 160),
    // Read back by the content script when a result returns: pageId anchors a
    // forced reprocess, and kindlePage is the reader's own page label, which
    // is better metadata than the running index it falls back to.
    pageId: safeText(capture.pageId, 160),
    kindlePage: safeText(capture.kindlePage, 30),
    side: capture.side === 'left' || capture.side === 'right' ? capture.side : undefined,
    index: Number.isFinite(Number(capture.index)) ? Number(capture.index) : undefined,
    pageMode: capture.pageMode === 'spread' ? 'spread' : 'single',
    rect: rect ? {
      x: finiteNumber(rect.x ?? rect.left),
      y: finiteNumber(rect.y ?? rect.top),
      width: finiteNumber(rect.width),
      height: finiteNumber(rect.height),
    } : undefined,
  };
}

export function safeText(value, maxLength) {
  return String(value || '').trim().slice(0, maxLength);
}

export function safeUrl(value) {
  const text = safeText(value, 2048);
  if (!text) return '';
  try {
    const url = new URL(text);
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return '';
    return url.toString();
  } catch {
    return '';
  }
}

export function finiteNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : undefined;
}

export function safePositiveInteger(value) {
  const number = Number(value);
  if (!Number.isInteger(number) || number <= 0) return '';
  return String(number);
}

export function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return 'unknown size';
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}
