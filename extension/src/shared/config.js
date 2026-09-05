export const DEFAULT_SETTINGS = Object.freeze({
  apiBaseUrl: '',
  authToken: '',
  kindleEnabled: true,
  webtoonEnabled: true,
  mangaPipeline: 'manga_translate',
  targetLanguage: 'en',
  webtoonPrefetch: 'nearby',
  readerMode: 'lens',
  lensZoom: 2,
  // Per-volume pipeline overrides, keyed by Kindle ASIN. A manga volume and a
  // novel need different pipelines, and the reader moves between them.
  bookPipelines: {},
});

export const STORAGE_KEYS = Object.freeze({
  settings: 'frankSettings',
  activeJobs: 'frankActiveJobs',
  diagnostics: 'frankDiagnostics',
});

export const KINDLE_HOSTS = new Set(['read.amazon.co.jp', 'read.kindle.co.jp']);
export const NAVER_WEBTOON_HOSTS = new Set(['comic.naver.com', 'm.comic.naver.com']);
export const VALID_TARGET_LANGUAGES = new Set(['en', 'pt-br']);
// Kindle delivers manga and prose alike as page images, so the pipeline is the
// reader's choice rather than something detectable from the page.
export const VALID_MANGA_PIPELINES = new Set([
  'manga_translate',
  'manga_furigana',
  'book_furigana',
]);
// Kindle ASINs. Anything else in the map is not a book we put there.
export const ASIN_PATTERN = /^B[A-Z0-9]{9}$/;
// Bounded so a long library cannot grow extension storage without limit.
export const MAX_BOOK_PIPELINES = 50;

export const VALID_READER_MODES = new Set(['lens', 'full']);
export const VALID_LENS_ZOOMS = Object.freeze([1.5, 2, 3]);

export function normalizeSettings(raw = {}) {
  return {
    ...DEFAULT_SETTINGS,
    ...raw,
    apiBaseUrl: normalizeApiBaseUrl(raw.apiBaseUrl ?? DEFAULT_SETTINGS.apiBaseUrl),
    mangaPipeline: VALID_MANGA_PIPELINES.has(raw.mangaPipeline)
      ? raw.mangaPipeline
      : DEFAULT_SETTINGS.mangaPipeline,
    targetLanguage: raw.targetLanguage === 'pt-br' ? 'pt-br' : 'en',
    webtoonPrefetch: raw.webtoonPrefetch === 'off' || raw.webtoonPrefetch === 'episode'
      ? raw.webtoonPrefetch
      : 'nearby',
    kindleEnabled: raw.kindleEnabled !== false,
    webtoonEnabled: raw.webtoonEnabled !== false,
    readerMode: raw.readerMode === 'full' ? 'full' : 'lens',
    lensZoom: normalizeLensZoom(raw.lensZoom),
    bookPipelines: normalizeBookPipelines(raw.bookPipelines),
  };
}

export function normalizeBookPipelines(raw) {
  if (!raw || typeof raw !== 'object') return {};
  const entries = Object.entries(raw)
    .filter(([asin, pipeline]) => ASIN_PATTERN.test(asin)
      && VALID_MANGA_PIPELINES.has(pipeline))
    .slice(-MAX_BOOK_PIPELINES);
  return Object.fromEntries(entries);
}

/// The pipeline a volume should use: its own choice, else the default.
export function pipelineForBook(settings, bookId) {
  const chosen = bookId ? settings?.bookPipelines?.[bookId] : null;
  return VALID_MANGA_PIPELINES.has(chosen)
    ? chosen
    : (settings?.mangaPipeline || DEFAULT_SETTINGS.mangaPipeline);
}

export function normalizeLensZoom(value) {
  const zoom = Number(value);
  return VALID_LENS_ZOOMS.includes(zoom) ? zoom : 2;
}

export function normalizeApiBaseUrl(value) {
  const trimmed = String(value || '').trim().replace(/\/+$/, '');
  if (!trimmed) return '';
  try {
    const url = new URL(trimmed);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return '';
    url.pathname = url.pathname.replace(/\/+$/, '');
    url.search = '';
    url.hash = '';
    return url.toString().replace(/\/+$/, '');
  } catch {
    return '';
  }
}

export function apiOriginPattern(apiBaseUrl) {
  const url = new URL(apiBaseUrl);
  return `${url.protocol}//${url.host}/*`;
}
