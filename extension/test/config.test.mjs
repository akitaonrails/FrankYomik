import test from 'node:test';
import assert from 'node:assert/strict';

import {
  MAX_BOOK_PIPELINES,
  apiOriginPattern,
  normalizeApiBaseUrl,
  normalizeSettings,
  pipelineForBook,
  withPipelineChoice,
} from '../src/shared/config.js';

test('normalizeApiBaseUrl strips trailing slashes and URL noise', () => {
  assert.equal(normalizeApiBaseUrl(' https://frank.example.net/api///?x=1#token '), 'https://frank.example.net/api');
});

test('normalizeApiBaseUrl accepts LAN HTTP but rejects non-HTTP protocols', () => {
  assert.equal(normalizeApiBaseUrl('http://192.168.0.90:8080/'), 'http://192.168.0.90:8080');
  assert.equal(normalizeApiBaseUrl('file:///tmp/nope'), '');
});

test('apiOriginPattern grants only the configured origin', () => {
  assert.equal(apiOriginPattern('https://frank.example.net/api'), 'https://frank.example.net/*');
  assert.equal(apiOriginPattern('http://192.168.0.90:8080'), 'http://192.168.0.90:8080/*');
});

test('normalizeSettings clamps enum-like settings to supported values', () => {
  const settings = normalizeSettings({
    apiBaseUrl: 'https://frank.example.net/',
    mangaPipeline: 'bad',
    targetLanguage: 'es',
    webtoonPrefetch: 'too-much',
    kindleEnabled: false,
  });
  assert.equal(settings.apiBaseUrl, 'https://frank.example.net');
  assert.equal(settings.mangaPipeline, 'manga_translate');
  assert.equal(settings.targetLanguage, 'en');
  assert.equal(settings.webtoonPrefetch, 'nearby');
  assert.equal(settings.kindleEnabled, false);
  assert.equal(settings.webtoonEnabled, true);
});

test('normalizeSettings defaults to lens reading at 2x', () => {
  const settings = normalizeSettings({});
  assert.equal(settings.readerMode, 'lens');
  assert.equal(settings.lensZoom, 2);
});

test('normalizeSettings clamps reader mode and magnification', () => {
  assert.equal(normalizeSettings({ readerMode: 'full' }).readerMode, 'full');
  assert.equal(normalizeSettings({ readerMode: 'sideways' }).readerMode, 'lens');
  assert.equal(normalizeSettings({ lensZoom: 1.5 }).lensZoom, 1.5);
  assert.equal(normalizeSettings({ lensZoom: 3 }).lensZoom, 3);
  assert.equal(normalizeSettings({ lensZoom: 12 }).lensZoom, 2);
  assert.equal(normalizeSettings({ lensZoom: 'huge' }).lensZoom, 2);
});

test('normalizeSettings accepts the book pipeline and rejects invented ones', () => {
  // Kindle prose pages go through their own pipeline; manga must be unaffected.
  assert.equal(normalizeSettings({ mangaPipeline: 'book_furigana' }).mangaPipeline, 'book_furigana');
  assert.equal(normalizeSettings({ mangaPipeline: 'manga_furigana' }).mangaPipeline, 'manga_furigana');
  assert.equal(normalizeSettings({ mangaPipeline: 'book_translate' }).mangaPipeline, 'manga_translate');
  assert.equal(normalizeSettings({}).mangaPipeline, 'manga_translate');
});

test('per-book pipelines keep only real books and real pipelines', () => {
  const settings = normalizeSettings({
    bookPipelines: {
      B0ABCDEFGH: 'book_furigana',
      B0IJKLMNOP: 'not_a_pipeline',
      'javascript:alert(1)': 'manga_furigana',
      '../../etc': 'manga_furigana',
    },
  });
  assert.deepEqual(settings.bookPipelines, { B0ABCDEFGH: 'book_furigana' });
});

test('per-book pipelines are bounded so storage cannot grow forever', () => {
  const many = {};
  for (let i = 0; i < MAX_BOOK_PIPELINES + 10; i++) {
    many[`B0${String(i).padStart(8, '0')}`] = 'manga_furigana';
  }
  const kept = normalizeSettings({ bookPipelines: many }).bookPipelines;
  assert.equal(Object.keys(kept).length, MAX_BOOK_PIPELINES);
});

test('pipelineForBook prefers the book over the default', () => {
  const settings = normalizeSettings({
    mangaPipeline: 'manga_furigana',
    bookPipelines: { B0ABCDEFGH: 'book_furigana' },
  });
  assert.equal(pipelineForBook(settings, 'B0ABCDEFGH'), 'book_furigana');
  assert.equal(pipelineForBook(settings, 'B0ZZZZZZZZ'), 'manga_furigana');
  assert.equal(pipelineForBook(settings, ''), 'manga_furigana');
  assert.equal(pipelineForBook(settings, null), 'manga_furigana');
});

test('a pipeline choice lands on the book being read', () => {
  // One control, two meanings: with a book open it is that book's pipeline.
  // A single global setting cannot follow a reader between a manga and a novel.
  const settings = normalizeSettings({ mangaPipeline: 'manga_furigana' });

  const next = withPipelineChoice(settings, 'B0GMLQXJ4S', 'book_furigana');

  assert.equal(next.bookPipelines.B0GMLQXJ4S, 'book_furigana');
  assert.equal(next.mangaPipeline, 'manga_furigana', 'the default is untouched');
});

test('a pipeline choice with no book open sets the default', () => {
  const settings = normalizeSettings({ mangaPipeline: 'manga_furigana' });

  const next = withPipelineChoice(settings, null, 'book_furigana');

  assert.equal(next.mangaPipeline, 'book_furigana');
  assert.deepEqual(next.bookPipelines, {});
});

test('choosing for one book leaves the others alone', () => {
  const settings = normalizeSettings({
    mangaPipeline: 'manga_translate',
    bookPipelines: { B0MANGAAAA: 'manga_furigana' },
  });

  const next = withPipelineChoice(settings, 'B0GMLQXJ4S', 'book_furigana');

  assert.equal(next.bookPipelines.B0MANGAAAA, 'manga_furigana');
  assert.equal(next.bookPipelines.B0GMLQXJ4S, 'book_furigana');
});

test('an invented pipeline changes nothing', () => {
  const settings = normalizeSettings({ mangaPipeline: 'manga_furigana' });
  assert.equal(withPipelineChoice(settings, 'B0GMLQXJ4S', 'nonsense'), settings);
});
