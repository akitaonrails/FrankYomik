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
  };
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
