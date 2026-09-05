import { apiOriginPattern, normalizeApiBaseUrl, withPipelineChoice } from '../shared/config.js';

const form = document.querySelector('#settings-form');
const statusEl = document.querySelector('#status');

const fields = {
  apiBaseUrl: document.querySelector('#api-base-url'),
  authToken: document.querySelector('#auth-token'),
  kindleEnabled: document.querySelector('#kindle-enabled'),
  webtoonEnabled: document.querySelector('#webtoon-enabled'),
  mangaPipeline: document.querySelector('#manga-pipeline'),
  targetLanguage: document.querySelector('#target-language'),
  webtoonPrefetch: document.querySelector('#webtoon-prefetch'),
  readerMode: document.querySelector('#reader-mode'),
  lensZoom: document.querySelector('#lens-zoom'),
};
const pipelineScopeHint = document.querySelector('#pipeline-scope');
const activeJobsEl = document.querySelector('#active-jobs');
const diagnosticsListEl = document.querySelector('#diagnostics-list');
// Per-volume pipeline choices are not form state: they are keyed by book and
// edited one at a time, so they are carried across saves rather than read back
// out of the form.
let bookPipelines = {};
let defaultPipeline = 'manga_translate';
let currentBook = null;
let lastSavedSignature = '';
let saveInFlight = null;
let autoSaveTimer = null;

loadSettings().then(refreshCurrentBook);
refreshDiagnostics();
window.setInterval(refreshDiagnostics, 2000);

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  await saveSettings({ force: true });
});

for (const [name, field] of Object.entries(fields)) {
  // The pipeline select follows the book being read, so it writes a keyed map
  // rather than a plain form value.
  if (name === 'mangaPipeline') continue;
  field.addEventListener('blur', scheduleAutosave);
  if (field.type === 'checkbox' || field.tagName === 'SELECT') {
    field.addEventListener('change', scheduleAutosave);
  }
}

fields.mangaPipeline.addEventListener('change', async (event) => {
  try {
    await setPipeline(event.target.value);
  } catch (error) {
    setStatus(error.message || String(error), 'error');
  }
});

document.querySelector('#health-check').addEventListener('click', async () => {
  setStatus('Checking server…');
  const response = await sendMessage({ type: 'CHECK_HEALTH' });
  if (!response.ok) {
    setStatus(response.error || 'Health check failed.', 'error');
    return;
  }
  const health = response.health || {};
  setStatus(`Server OK. Redis: ${health.redis || 'unknown'}, workers: ${health.active_workers ?? 0}.`, 'ok');
});

document.querySelector('#refresh-diagnostics').addEventListener('click', refreshDiagnostics);
document.querySelector('#export-settings').addEventListener('click', exportSettings);
document.querySelector('#force-reprocess-current').addEventListener('click', forceReprocessCurrent);
document.querySelector('#export-debug-images').addEventListener('click', exportDebugImages);
document.querySelector('#import-settings').addEventListener('click', () => {
  document.querySelector('#import-settings-file').click();
});
document.querySelector('#import-settings-file').addEventListener('change', importSettingsFile);

async function loadSettings() {
  const response = await sendMessage({ type: 'GET_SETTINGS' });
  if (!response.ok) {
    setStatus(response.error || 'Could not load settings.', 'error');
    return;
  }
  applySettings(response.settings || {});
  markSettingsSaved();
}

async function saveSettings({ force = false } = {}) {
  if (saveInFlight) await saveInFlight;
  if (!force && settingsSignature() === lastSavedSignature) return true;

  saveInFlight = saveSettingsNow().finally(() => {
    saveInFlight = null;
  });
  return saveInFlight;
}

async function saveSettingsNow() {
  setStatus('Saving…');
  const settings = readSettings();
  const permissionGranted = await requestApiPermission(settings.apiBaseUrl);
  if (!permissionGranted) return false;
  const response = await sendMessage({ type: 'SAVE_SETTINGS', settings });
  if (!response.ok) {
    setStatus(response.error || 'Could not save settings.', 'error');
    return false;
  }
  applySettings(response.settings || {});
  markSettingsSaved();
  setStatus('Saved.', 'ok');
  await refreshDiagnostics();
  return true;
}

function exportSettings() {
  const settings = readSettings();
  const blob = new Blob([`${JSON.stringify(settings, null, 2)}\n`], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'frank-yomik-extension-settings.json';
  link.click();
  URL.revokeObjectURL(url);
  setStatus('Settings exported. Keep the file private because it contains the auth token.', 'ok');
}

async function importSettingsFile(event) {
  const file = event.target.files?.[0];
  event.target.value = '';
  if (!file) return;
  try {
    const imported = JSON.parse(await file.text());
    applySettings(imported);
    await saveSettings({ force: true });
  } catch (error) {
    setStatus(`Import failed: ${error.message || error}`, 'error');
  }
}

async function forceReprocessCurrent() {
  try {
    const saved = await saveSettings({ force: false });
    if (!saved) return;
    setStatus('Requesting forced reprocess on active tab…');
    const response = await sendMessage({ type: 'RUN_ACTIVE_TAB_ACTION', action: 'force-reprocess' });
    if (!response.ok) {
      setStatus(response.error || 'Force reprocess failed.', 'error');
      return;
    }
    setStatus(response.message || 'Forced reprocess submitted.', 'ok');
    await refreshDiagnostics();
  } catch (error) {
    setStatus(error.message || String(error), 'error');
  }
}

async function exportDebugImages() {
  try {
    const saved = await saveSettings({ force: false });
    if (!saved) return;
    setStatus('Uploading debug pages from active tab…');
    const response = await sendMessage({ type: 'RUN_ACTIVE_TAB_ACTION', action: 'upload-debug-pair' });
    if (!response.ok) {
      setStatus(response.error || 'Debug upload failed.', 'error');
      return;
    }
    const latest = response.manifest?.id ? `/api/v1/debug/pages/${response.manifest.id}` : (response.url || '');
    setStatus(`Debug pages uploaded: ${response.debugId || response.manifest?.id || 'ok'}${latest ? ` (${latest})` : ''}`, 'ok');
  } catch (error) {
    setStatus(error.message || String(error), 'error');
  }
}

function applySettings(settings) {
  fields.apiBaseUrl.value = settings.apiBaseUrl || '';
  fields.authToken.value = settings.authToken || '';
  fields.kindleEnabled.checked = settings.kindleEnabled !== false;
  fields.webtoonEnabled.checked = settings.webtoonEnabled !== false;
  fields.targetLanguage.value = settings.targetLanguage || 'en';
  fields.webtoonPrefetch.value = settings.webtoonPrefetch || 'nearby';
  fields.readerMode.value = settings.readerMode || 'lens';
  fields.lensZoom.value = String(settings.lensZoom ?? 2);
  bookPipelines = { ...(settings.bookPipelines || {}) };
  defaultPipeline = settings.mangaPipeline || 'manga_translate';
  applyPipelineScope(settings);
}

/// Point the pipeline select at whatever the reader is actually reading.
///
/// One control, two meanings: with a book open it is that book's pipeline,
/// otherwise it is the default new books start from. A manga and a novel need
/// different pipelines, and a single global setting cannot follow the reader
/// between them.
function applyPipelineScope(settings = {}) {
  const fallback = settings.mangaPipeline || defaultPipeline;
  if (currentBook) {
    fields.mangaPipeline.value = bookPipelines[currentBook] || fallback;
    pipelineScopeHint.textContent =
      `Applies to this book (${currentBook}). Others keep their own.`;
  } else {
    fields.mangaPipeline.value = fallback;
    pipelineScopeHint.textContent = 'Default for books you have not set.';
  }
}

/// Save the pipeline against the book being read, or as the default.
async function setPipeline(value) {
  const next = withPipelineChoice(
    { mangaPipeline: defaultPipeline, bookPipelines }, currentBook, value,
  );
  defaultPipeline = next.mangaPipeline;
  bookPipelines = next.bookPipelines || bookPipelines;
  const saved = await saveSettings({ force: true });
  if (saved !== false) {
    setStatus(currentBook ? 'Saved for this book.' : 'Saved as the default.', 'ok');
  }
}

/// Ask the open reader which volume it is showing.
async function refreshCurrentBook() {
  try {
    const response = await sendMessage({ type: 'RUN_ACTIVE_TAB_ACTION', action: 'get-book' });
    currentBook = response.ok ? (response.bookId || null) : null;
  } catch {
    currentBook = null;  // not a Kindle tab, or no reader in it
  }
  applyPipelineScope();
}



function readSettings() {
  return {
    apiBaseUrl: fields.apiBaseUrl.value,
    authToken: fields.authToken.value,
    kindleEnabled: fields.kindleEnabled.checked,
    webtoonEnabled: fields.webtoonEnabled.checked,
    // The select edits the current book; the default only changes when no book
    // is open, so it is carried across saves rather than read from the form.
    mangaPipeline: currentBook ? defaultPipeline : fields.mangaPipeline.value,
    targetLanguage: fields.targetLanguage.value,
    webtoonPrefetch: fields.webtoonPrefetch.value,
    readerMode: fields.readerMode.value,
    lensZoom: Number(fields.lensZoom.value),
    bookPipelines,
  };
}

function scheduleAutosave() {
  window.clearTimeout(autoSaveTimer);
  autoSaveTimer = window.setTimeout(() => {
    autoSaveTimer = null;
    saveSettings().catch((error) => {
      setStatus(error.message || String(error), 'error');
    });
  }, 150);
}

function settingsSignature(settings = readSettings()) {
  return JSON.stringify(settings);
}

function markSettingsSaved() {
  lastSavedSignature = settingsSignature();
}

async function requestApiPermission(apiBaseUrl) {
  const normalized = normalizeApiBaseUrl(apiBaseUrl);
  if (!normalized) return true;
  const origin = apiOriginPattern(normalized);
  const alreadyGranted = await chrome.permissions.contains({ origins: [origin] });
  if (alreadyGranted) return true;
  let granted = false;
  try {
    granted = await chrome.permissions.request({ origins: [origin] });
  } catch (error) {
    setStatus(`Could not request API permission for ${origin}. Click Save now to retry.`, 'error');
    return false;
  }
  if (!granted) {
    setStatus(`Permission denied for ${origin}.`, 'error');
    return false;
  }
  return true;
}

function setStatus(message, kind = '') {
  statusEl.textContent = message;
  if (kind) statusEl.dataset.kind = kind;
  else delete statusEl.dataset.kind;
}

async function refreshDiagnostics() {
  const response = await sendMessage({ type: 'GET_DIAGNOSTICS' });
  if (!response.ok) {
    activeJobsEl.textContent = 'Active jobs: unavailable';
    diagnosticsListEl.replaceChildren();
    return;
  }

  const jobs = Object.values(response.jobs || {});
  activeJobsEl.textContent = jobs.length
    ? `Active jobs: ${jobs.length} (${jobs.map((job) => `${job.site || 'job'}:${job.status || 'queued'}`).join(', ')})`
    : 'Active jobs: 0';
  const events = Array.isArray(response.diagnostics) ? response.diagnostics.slice(0, 12) : [];
  if (!events.length) {
    const item = document.createElement('li');
    item.textContent = 'No extension activity recorded yet. Reload Kindle/Naver after saving settings.';
    diagnosticsListEl.replaceChildren(item);
    return;
  }

  diagnosticsListEl.replaceChildren(...events.map((event) => {
    const item = document.createElement('li');
    item.dataset.level = event.level || 'info';
    const time = event.ts ? new Date(event.ts).toLocaleTimeString() : '';
    item.textContent = `${time} ${event.site || 'extension'}: ${event.message || ''}`.trim();
    return item;
  }));
}

function sendMessage(message) {
  return chrome.runtime.sendMessage(message).catch((error) => ({
    ok: false,
    error: error.message || String(error),
  }));
}
