import {
  ASIN_PATTERN,
  DEFAULT_SETTINGS,
  KINDLE_HOSTS,
  NAVER_WEBTOON_HOSTS,
  STORAGE_KEYS,
  VALID_MANGA_PIPELINES,
  VALID_TARGET_LANGUAGES,
  apiOriginPattern,
  normalizeSettings,
  withPipelineChoice,
} from '../shared/config.js';
import {
  SIZE_LIMITS,
  TIMING,
  cacheKeyFor,
  formatBytes,
  normalizeApiImageUrl,
  safeText,
  sanitizeCapture,
  sanitizeMetadata,
  validateAllowedWebtoonImageUrl,
} from '../shared/policy.js';

const POLL_ALARM_NAME = 'frankPollJobsSoon';
const POLL_FALLBACK_ALARM_NAME = 'frankPollJobsFallback';
const DB_NAME = 'frank-yomik-extension';
const DB_VERSION = 1;
const IMAGE_STORE = 'images';

// WebSocket job-completion delivery. The server's /api/v1/ws endpoint pushes
// `job_complete` notifications as soon as the worker finishes — eliminating
// the 0-3s tail the polling loop adds on top of per-job work. Polling stays
// as a fallback for SW restarts and dropped sockets.
const WS_BACKOFF_INITIAL_MS = 1_000;
const WS_BACKOFF_MAX_MS = 30_000;

let pollTimer = null;
let ws = null;
let wsState = 'closed'; // 'closed' | 'connecting' | 'open'
let wsReconnectTimer = null;
let wsBackoffMs = WS_BACKOFF_INITIAL_MS;
let wsApiBase = '';

restrictStorageAccess();

chrome.runtime.onInstalled.addListener(() => {
  restrictStorageAccess();
  ensurePollingAlarm();
  manageWebSocketLifecycle().catch((error) => console.warn('[Frank] ws lifecycle failed on install:', error));
});

chrome.runtime.onStartup.addListener(() => {
  restrictStorageAccess();
  ensurePollingAlarm();
  manageWebSocketLifecycle().catch((error) => console.warn('[Frank] ws lifecycle failed on startup:', error));
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === POLL_ALARM_NAME || alarm.name === POLL_FALLBACK_ALARM_NAME) {
    pollActiveJobs().catch((error) => console.warn('[Frank] poll alarm failed:', error));
  }
});

chrome.commands?.onCommand?.addListener((command) => {
  if (command !== 'force-reprocess-current') return;
  runActiveTabAction('force-reprocess')
    .then(() => recordEvent({ site: 'extension', level: 'info', message: 'Shortcut forced current page reprocess' }))
    .catch((error) => recordEvent({ site: 'extension', level: 'error', message: `Shortcut force reprocess failed: ${error.message || error}` }));
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  handleMessage(message, sender)
    .then(sendResponse)
    .catch((error) => {
      recordMessageError(message, sender, error).finally(() => {
        sendResponse({ ok: false, error: error.message || String(error) });
      });
    });
  return true;
});

async function handleMessage(message, sender) {
  if (!message || typeof message !== 'object') {
    throw new Error('invalid message');
  }

  switch (message.type) {
    case 'GET_SETTINGS':
      return { ok: true, settings: await getSettingsForSender(sender) };
    case 'SAVE_SETTINGS':
      assertExtensionPage(sender, 'SAVE_SETTINGS');
      return saveSettings(message.settings || {});
    case 'CHECK_HEALTH':
      assertExtensionPage(sender, 'CHECK_HEALTH');
      return checkHealth();
    case 'SUBMIT_CAPTURE':
      return submitCapture(message, sender);
    case 'FETCH_WEBTOON_IMAGE':
      return fetchWebtoonImage(message, sender);
    case 'REPORT_EVENT':
      return reportEvent(message, sender);
    case 'GET_ACTIVE_JOBS':
      assertExtensionPage(sender, 'GET_ACTIVE_JOBS');
      return { ok: true, jobs: await loadActiveJobs() };
    case 'GET_DIAGNOSTICS':
      assertExtensionPage(sender, 'GET_DIAGNOSTICS');
      return { ok: true, diagnostics: await loadDiagnostics(), jobs: await loadActiveJobs() };
    case 'SET_BOOK_PIPELINE':
      // Content scripts may set the pipeline for the book they are reading,
      // and nothing else: the value is checked against the allowlist and the
      // key against the shape of an ASIN. Credentials stay out of reach.
      return setBookPipeline(message.bookId, message.pipeline);
    case 'RUN_ACTIVE_TAB_ACTION':
      assertExtensionPage(sender, 'RUN_ACTIVE_TAB_ACTION');
      return runActiveTabAction(message.action);
    default:
      throw new Error(`unknown message type: ${message.type}`);
  }
}

async function setBookPipeline(bookId, pipeline) {
  if (!ASIN_PATTERN.test(String(bookId || ''))) throw new Error('invalid book id');
  if (!VALID_MANGA_PIPELINES.has(pipeline)) throw new Error('invalid pipeline');
  const settings = await loadSettings();
  const next = withPipelineChoice(settings, bookId, pipeline);
  await chrome.storage.local.set({ [STORAGE_KEYS.settings]: normalizeSettings(next) });
  await recordEvent({
    site: 'kindle',
    level: 'info',
    message: `Pipeline for ${bookId} set to ${pipeline}`,
  });
  return { ok: true, bookId, pipeline };
}

async function runActiveTabAction(action) {
  const messageType = action === 'force-reprocess'
    ? 'FRANK_FORCE_REPROCESS_CURRENT'
    : (action === 'export-debug-pair' || action === 'upload-debug-pair')
      ? 'FRANK_EXPORT_DEBUG_PAIR'
      : action === 'get-book'
        ? 'FRANK_GET_BOOK'
        : '';
  if (!messageType) throw new Error('unknown active tab action');
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) throw new Error('No active tab found.');
  let response = null;
  try {
    response = await chrome.tabs.sendMessage(tab.id, { type: messageType });
  } catch (error) {
    if (!isMissingContentScriptError(error)) throw error;
    await injectContentScriptsForTab(tab);
    response = await chrome.tabs.sendMessage(tab.id, { type: messageType });
  }
  if (!response?.ok) throw new Error(response?.error || 'Content script could not run the action.');
  if (action === 'upload-debug-pair') {
    response = await uploadDebugPair(response, tab);
  }
  // Reading which volume is open is a query, not an action worth logging.
  if (action !== 'get-book') {
    await recordEvent({ site: response.site || 'extension', level: 'info', message: `Active tab action completed: ${action}` });
  }
  return response;
}

async function injectContentScriptsForTab(tab) {
  const site = siteForTabUrl(tab.url || '');
  if (!site) {
    throw new Error('Open a Kindle or Naver Webtoon reader tab, then use the Frank Yomik popup from that tab.');
  }
  const files = site === 'kindle'
    ? ['src/content/overlay.js', 'src/content/kindle.js', 'src/content/bootstrap.js']
    : ['src/content/overlay.js', 'src/content/webtoon.js', 'src/content/bootstrap.js'];
  try {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: site === 'kindle' },
      files,
    });
    await sleep(250);
  } catch (error) {
    throw new Error(`Could not inject Frank Yomik into this tab. Reload the reader tab and try again. ${error.message || error}`);
  }
}

function siteForTabUrl(rawUrl) {
  try {
    const url = new URL(rawUrl);
    const host = url.hostname.toLowerCase();
    if (KINDLE_HOSTS.has(host)) return 'kindle';
    if (NAVER_WEBTOON_HOSTS.has(host)) return 'webtoon';
  } catch {
    // Ignore invalid/non-web extension page URLs.
  }
  return '';
}

function isMissingContentScriptError(error) {
  const message = String(error?.message || error || '');
  return message.includes('Receiving end does not exist') ||
    message.includes('Could not establish connection');
}

async function getSettings() {
  const stored = await chrome.storage.local.get(STORAGE_KEYS.settings);
  return normalizeSettings(stored[STORAGE_KEYS.settings] || DEFAULT_SETTINGS);
}

async function getSettingsForSender(sender) {
  const settings = await getSettings();
  if (!sender?.tab) return settings;
  // An allowlist rather than a redaction, so a new setting is never leaked to
  // a page by default. Everything a content script actually reads has to be
  // here: a setting missing from this list does not fail, it silently falls
  // back — which is how a book set to the text pipeline kept submitting as
  // manga, and how the reader mode and lens zoom never left the popup.
  return {
    configured: Boolean(settings.apiBaseUrl && settings.authToken),
    kindleEnabled: settings.kindleEnabled,
    webtoonEnabled: settings.webtoonEnabled,
    mangaPipeline: settings.mangaPipeline,
    bookPipelines: settings.bookPipelines,
    webtoonPrefetch: settings.webtoonPrefetch,
    readerMode: settings.readerMode,
    lensZoom: settings.lensZoom,
  };
}

async function saveSettings(rawSettings) {
  const settings = normalizeSettings(rawSettings);
  if (settings.apiBaseUrl) {
    const origin = apiOriginPattern(settings.apiBaseUrl);
    const granted = await chrome.permissions.contains({ origins: [origin] });
    if (!granted) {
      throw new Error(`API host permission has not been granted for ${origin}`);
    }
  }
  await chrome.storage.local.set({ [STORAGE_KEYS.settings]: settings });
  // API base URL / token may have changed — drop any existing socket so the
  // next ensure picks up the new credentials.
  closeWebSocket();
  manageWebSocketLifecycle().catch((error) => console.warn('[Frank] ws lifecycle after save failed:', error));
  return { ok: true, settings };
}

async function checkHealth() {
  const settings = await getSettings();
  if (!settings.apiBaseUrl) throw new Error('Set the API base URL first.');
  const response = await fetchWithTimeout(`${settings.apiBaseUrl}/api/v1/health`, {
    method: 'GET',
    cache: 'no-store',
  }, TIMING.healthTimeoutMs);
  if (!response.ok) throw new Error(`health check failed: HTTP ${response.status}`);
  return { ok: true, health: await response.json() };
}

async function submitCapture(message, sender) {
  const site = validateSender(sender);
  const settings = await getSettings();
  if (!settings.apiBaseUrl || !settings.authToken) {
    throw new Error('Extension is not configured. Set API URL and auth token first.');
  }
  if (site === 'kindle' && settings.kindleEnabled === false) throw new Error('Kindle support is disabled.');
  if (site === 'webtoon' && settings.webtoonEnabled === false) throw new Error('Webtoon support is disabled.');

  const pageId = safeText(message.pageId, 120) || `${site}-${Date.now()}`;
  const dataUrl = String(message.imageDataUrl || '');
  if (!dataUrl.startsWith('data:image/png;base64,') && !dataUrl.startsWith('data:image/jpeg;base64,')) {
    throw new Error('capture must be a PNG or JPEG data URL');
  }
  if (dataUrl.length > SIZE_LIMITS.maxCaptureDataUrlChars) throw new Error('capture is too large');
  await recordEvent({ site, level: 'info', message: `Received ${site} capture ${pageId} (${formatBytes(dataUrl.length)})` });

  const image = await dataUrlToBlobAndBytes(dataUrl);
  const sourceHash = await sha256Hex(image.bytes);
  const pipeline = pipelineForSite(site, settings, message.pipeline);
  const targetLanguage = VALID_TARGET_LANGUAGES.has(settings.targetLanguage) ? settings.targetLanguage : 'en';
  const cachePipeline = targetLanguage === 'en' ? pipeline : `${pipeline}_${targetLanguage}`;
  const cacheKey = cacheKeyFor(settings.apiBaseUrl, cachePipeline, sourceHash);
  const cachedDataUrl = message.force === true ? null : await cacheGet(cacheKey);
  if (cachedDataUrl) {
    return {
      ok: true,
      status: 'completed',
      cached: true,
      pageId,
      site,
      sourceHash,
      pipeline,
      imageDataUrl: cachedDataUrl,
      capture: sanitizeCapture(message.capture),
    };
  }

  const metadata = sanitizeMetadata(message.metadata || {}, sender.url || sender.tab?.url || '');
  const priority = message.priority === 'low' ? 'low' : 'high';
  await recordEvent({ site, level: 'info', message: `Submitting ${site} capture ${pageId}` });
  const response = await submitJob(settings, image.blob, {
    pipeline,
    priority,
    targetLanguage,
    metadata,
    force: message.force === true,
  });

  const jobId = String(response.job_id || '');
  if (!jobId) throw new Error('server response did not include job_id');

  if (response.cached === true) {
    const imageUrl = response.image_url || `/api/v1/jobs/${encodeURIComponent(jobId)}/image`;
    try {
      const translated = await downloadImageDataUrl(settings, imageUrl);
      await cachePut(cacheKey, translated, { sourceHash, pipeline: cachePipeline, targetLanguage });
      await recordEvent({ site, level: 'info', message: `Cache hit applied for ${pageId}` });
      return {
        ok: true,
        status: 'completed',
        cached: true,
        pageId,
        site,
        jobId,
        sourceHash: response.source_hash || sourceHash,
        pipeline,
        imageUrl,
        imageDataUrl: translated,
        capture: sanitizeCapture(message.capture),
      };
    } catch (error) {
      console.warn('[Frank] cached image download failed; forcing reprocess:', error);
      const forced = await submitJob(settings, image.blob, {
        pipeline,
        priority,
        targetLanguage,
        metadata,
        force: true,
      });
      return queueJobRecord({
        response: forced,
        sender,
        pageId,
        site,
        sourceHash,
        pipeline,
        cachePipeline,
        cacheKey,
        capture: sanitizeCapture(message.capture),
      });
    }
  }

  return queueJobRecord({
    response,
    sender,
    pageId,
    site,
    sourceHash: response.source_hash || sourceHash,
    pipeline,
    cachePipeline,
    cacheKey,
    capture: sanitizeCapture(message.capture),
  });
}

async function fetchWebtoonImage(message, sender) {
  const site = validateSender(sender);
  if (site !== 'webtoon') throw new Error('webtoon image fetch is only allowed from Naver Webtoon pages');
  const url = validateAllowedWebtoonImageUrl(message.src);
  const response = await fetchWithTimeout(url.toString(), {
    method: 'GET',
    cache: 'force-cache',
    credentials: 'omit',
    redirect: 'error',
  }, TIMING.webtoonFetchTimeoutMs);
  if (!response.ok) throw new Error(`image fetch failed: HTTP ${response.status}`);
  const blob = await response.blob();
  if (blob.size > SIZE_LIMITS.maxCaptureDataUrlChars) throw new Error('webtoon source image is too large');
  if (blob.type && !blob.type.startsWith('image/')) throw new Error(`unexpected image type: ${blob.type}`);
  return { ok: true, imageDataUrl: await blobToDataUrl(blob) };
}

async function uploadDebugPair(pair, tab) {
  const settings = await getSettings();
  if (!settings.apiBaseUrl || !settings.authToken) {
    throw new Error('Extension is not configured. Set API URL and auth token first.');
  }
  const site = pair.site === 'kindle' || pair.site === 'webtoon' ? pair.site : siteForTabUrl(tab.url || '');
  if (!site) throw new Error('Debug pair must come from Kindle or Naver Webtoon.');
  const original = await dataUrlToBlobAndBytes(assertImageDataUrl(pair.originalDataUrl, 'original'));
  const translated = await dataUrlToBlobAndBytes(assertImageDataUrl(pair.translatedDataUrl, 'translated'));

  const form = new FormData();
  form.set('site', site);
  form.set('page_id', safeText(pair.pageId || pair.page || 'current', 200));
  form.set('source_url', safeText(pair.sourceUrl || tab.url || '', 2048));
  form.set('original', original.blob, 'original.png');
  form.set('translated', translated.blob, 'translated.png');
  if (pair.capture) form.set('capture_json', JSON.stringify(sanitizeCapture(pair.capture)));
  if (pair.metadata) form.set('metadata_json', JSON.stringify(sanitizeMetadata(pair.metadata, pair.sourceUrl || tab.url || '')));

  const response = await fetchWithTimeout(`${settings.apiBaseUrl}/api/v1/debug/pages`, {
    method: 'POST',
    headers: authHeaders(settings),
    body: form,
  }, TIMING.submitTimeoutMs);
  const text = await response.text();
  if (response.status !== 201) throw new Error(`debug upload failed: HTTP ${response.status} ${text}`);
  const manifest = JSON.parse(text);
  await recordEvent({ site, level: 'info', message: `Uploaded debug pair ${manifest.id || ''}`.trim() });
  return { ok: true, site, pageId: pair.pageId, debugId: manifest.id, manifest, url: manifest.id ? `/api/v1/debug/pages/${manifest.id}` : '' };
}

function assertImageDataUrl(dataUrl, label) {
  const value = String(dataUrl || '');
  if (!value.startsWith('data:image/png;base64,') && !value.startsWith('data:image/jpeg;base64,') && !value.startsWith('data:image/webp;base64,')) {
    throw new Error(`${label} debug image must be a PNG, JPEG, or WebP data URL`);
  }
  if (value.length > SIZE_LIMITS.maxCaptureDataUrlChars) throw new Error(`${label} debug image is too large`);
  return value;
}

async function reportEvent(message, sender) {
  const site = validateSender(sender);
  await recordEvent({
    site,
    level: safeText(message.level, 20) || 'info',
    message: safeText(message.message, 300),
  });
  return { ok: true };
}

async function recordMessageError(message, sender, error) {
  if (!sender?.tab) return;
  try {
    const site = validateSender(sender);
    const type = safeText(message?.type, 80) || 'message';
    await recordEvent({ site, level: 'error', message: `${type} failed: ${error.message || error}` });
  } catch {
    // Ignore diagnostics failures.
  }
}

async function queueJobRecord({ response, sender, pageId, site, sourceHash, pipeline, cachePipeline, cacheKey, capture }) {
  const jobId = String(response.job_id || '');
  if (!jobId) throw new Error('server response did not include job_id');

  const job = {
    recordId: '',
    jobId,
    pageId,
    site,
    tabId: sender.tab?.id,
    frameId: sender.frameId,
    sourceHash,
    pipeline,
    cachePipeline,
    cacheKey,
    capture,
    submittedAt: Date.now(),
    lastPollAt: 0,
    status: response.status || 'queued',
  };
  job.recordId = `${jobId}|${pageId}|${Date.now()}|${Math.random().toString(36).slice(2, 8)}`;
  const jobs = await loadActiveJobs();
  jobs[job.recordId] = job;
  await saveActiveJobs(jobs);
  await recordEvent({ site, level: 'info', message: `Queued ${site} job ${jobId} for ${pageId}` });
  schedulePollSoon();
  // Wake the socket if needed and subscribe immediately so we don't miss the
  // notification for fast-completing jobs.
  ensureWebSocket().then(() => subscribeJob(jobId)).catch((error) => console.warn('[Frank] ws ensure failed:', error));
  return { ok: true, status: 'queued', pageId, site, jobId, sourceHash, pipeline, capture };
}

function validateSender(sender) {
  const rawUrl = sender?.url || sender?.tab?.url || '';
  let url;
  try {
    url = new URL(rawUrl);
  } catch {
    throw new Error('message sender has no valid URL');
  }
  if (url.protocol !== 'https:') throw new Error('unsupported sender scheme');
  const host = url.hostname.toLowerCase();
  if (KINDLE_HOSTS.has(host)) return 'kindle';
  if (NAVER_WEBTOON_HOSTS.has(host)) return 'webtoon';
  throw new Error(`unsupported sender host: ${host}`);
}

function assertExtensionPage(sender, messageType) {
  if (sender?.tab) throw new Error(`${messageType} is not allowed from content scripts`);
}

function pipelineForSite(site, settings, requestedPipeline) {
  if (site === 'webtoon') return 'webtoon';
  const preferred = requestedPipeline || settings.mangaPipeline;
  return VALID_MANGA_PIPELINES.has(preferred) ? preferred : 'manga_translate';
}

async function submitJob(settings, imageBlob, options) {
  const form = new FormData();
  form.set('pipeline', options.pipeline);
  form.set('priority', options.priority);
  form.set('target_lang', options.targetLanguage);
  form.set('image', imageBlob, 'page.png');
  if (options.force) form.set('force', 'true');
  for (const [key, value] of Object.entries(options.metadata || {})) {
    if (value) form.set(key, value);
  }

  return withRetry(async () => {
    const response = await fetchWithTimeout(`${settings.apiBaseUrl}/api/v1/jobs`, {
      method: 'POST',
      headers: authHeaders(settings),
      body: form,
    }, TIMING.submitTimeoutMs);
    const text = await response.text();
    if (response.status !== 201) {
      throw retryableError(`submit failed: HTTP ${response.status} ${text}`, response.status >= 500);
    }
    return JSON.parse(text);
  });
}

async function getJobStatus(settings, jobId) {
  return withRetry(async () => {
    const response = await fetchWithTimeout(`${settings.apiBaseUrl}/api/v1/jobs/${encodeURIComponent(jobId)}`, {
      method: 'GET',
      headers: authHeaders(settings),
      cache: 'no-store',
    }, TIMING.statusTimeoutMs);
    if (!response.ok) throw retryableError(`status failed: HTTP ${response.status}`, response.status >= 500);
    return response.json();
  });
}

async function downloadImageDataUrl(settings, imageUrl) {
  const url = normalizeApiImageUrl(settings, imageUrl);
  return withRetry(async () => {
    const response = await fetchWithTimeout(url, {
      method: 'GET',
      headers: authHeaders(settings),
      cache: 'no-store',
      redirect: 'error',
    }, TIMING.imageDownloadTimeoutMs);
    if (!response.ok) throw retryableError(`image download failed: HTTP ${response.status}`, response.status >= 500);
    const blob = await response.blob();
    if (blob.size > SIZE_LIMITS.maxResultImageBytes) throw new Error('translated image is too large');
    if (blob.type && !blob.type.startsWith('image/')) throw new Error(`translated result is not an image: ${blob.type}`);
    return blobToDataUrl(blob);
  });
}

async function pollActiveJobs() {
  const settings = await getSettings();
  if (!settings.apiBaseUrl || !settings.authToken) return;
  const jobs = await loadActiveJobs();
  const entries = Object.values(jobs);
  if (!entries.length) {
    await manageWebSocketLifecycle();
    return;
  }

  // Keep WS subscriptions in sync with whatever the SW believes is active —
  // a no-op when the socket is already open and current.
  await ensureWebSocket(settings).catch(() => {});

  let changed = false;
  await recordEvent({ site: 'extension', level: 'info', message: `Polling ${entries.length} active job(s)` });
  for (const job of entries) {
    const recordId = job.recordId || job.jobId;
    if (Date.now() - job.submittedAt > TIMING.jobTimeoutMs) {
      delete jobs[recordId];
      changed = true;
      await notifyTab(job, { type: 'FRANK_JOB_FAILED', pageId: job.pageId, jobId: job.jobId, error: 'Job timed out' });
      continue;
    }

    try {
      job.lastPollAt = Date.now();
      const status = await getJobStatus(settings, job.jobId);
      job.status = status.status || job.status;
      changed = true;
      await recordEvent({ site: job.site, level: 'info', message: `Job ${job.jobId} status: ${status.status || 'unknown'}` });
      if (status.status === 'completed') {
        const ok = await finalizeJobCompletion(settings, job, jobs, recordId, {
          imageUrl: status.image_url,
          pageKind: status.page_kind,
          sourceHash: status.source_hash,
          source: 'poll',
        });
        if (ok) changed = true;
      } else if (status.status === 'failed') {
        delete jobs[recordId];
        changed = true;
        await recordEvent({ site: job.site, level: 'error', message: `Failed ${job.site} job ${job.jobId}: ${status.error || 'Job failed'}` });
        await notifyTab(job, {
          type: 'FRANK_JOB_FAILED',
          pageId: job.pageId,
          site: job.site,
          jobId: job.jobId,
          error: status.error || 'Job failed',
        });
      }
    } catch (error) {
      console.warn(`[Frank] poll failed for ${job.jobId}:`, error);
      await recordEvent({ site: job.site, level: 'error', message: `Poll failed for ${job.jobId}: ${error.message || error}` });
    }
  }

  if (changed) await saveActiveJobs(jobs);
  if (Object.keys(jobs).length) schedulePollSoon();
  await manageWebSocketLifecycle();
}

// Download the result, cache it, drop the job record, and notify the tab.
// Shared between the poll loop and WebSocket notifications so both paths are
// idempotent — first writer wins and the loser becomes a cheap no-op.
async function finalizeJobCompletion(settings, job, jobsMap, recordId, { imageUrl, sourceHash, source, pageKind }) {
  if (!jobsMap[recordId]) return false;
  const url = imageUrl || `/api/v1/jobs/${encodeURIComponent(job.jobId)}/image`;
  try {
    const imageDataUrl = await downloadImageDataUrl(settings, url);
    await cachePut(job.cacheKey, imageDataUrl, {
      sourceHash: sourceHash || job.sourceHash,
      pipeline: job.cachePipeline,
      targetLanguage: settings.targetLanguage,
    });
    delete jobsMap[recordId];
    await recordEvent({ site: job.site, level: 'info', message: `Completed ${job.site} job ${job.jobId} (${source})` });
    await notifyTab(job, {
      type: 'FRANK_JOB_COMPLETE',
      pageId: job.pageId,
      site: job.site,
      jobId: job.jobId,
      sourceHash: sourceHash || job.sourceHash,
      imageUrl: url,
      imageDataUrl,
      capture: job.capture,
      // What the worker made of the page, so a book on the wrong pipeline can
      // be corrected from evidence rather than from a render that looks odd.
      pageKind: safeText(pageKind, 16),
    });
    return true;
  } catch (error) {
    console.warn(`[Frank] finalize failed for ${job.jobId}:`, error);
    await recordEvent({ site: job.site, level: 'error', message: `Finalize failed for ${job.jobId}: ${error.message || error}` });
    return false;
  }
}

// --- WebSocket job-completion delivery ---

async function ensureWebSocket(settingsHint) {
  const jobs = await loadActiveJobs();
  if (!Object.keys(jobs).length) {
    closeWebSocket();
    return;
  }
  if (wsState !== 'closed') return;

  const settings = settingsHint || (await getSettings());
  if (!settings.apiBaseUrl || !settings.authToken) return;

  let url;
  try {
    url = buildWsUrl(settings);
  } catch (error) {
    console.warn('[Frank] could not build ws url:', error);
    return;
  }

  wsApiBase = settings.apiBaseUrl;
  wsState = 'connecting';
  try {
    ws = new WebSocket(url);
  } catch (error) {
    console.warn('[Frank] ws construct failed:', error);
    ws = null;
    wsState = 'closed';
    scheduleWsReconnect();
    return;
  }

  ws.addEventListener('open', () => {
    wsState = 'open';
    wsBackoffMs = WS_BACKOFF_INITIAL_MS;
    subscribeAllActiveJobs().catch((error) => console.warn('[Frank] ws subscribe failed:', error));
  });
  ws.addEventListener('message', (event) => {
    handleWsMessage(event).catch((error) => console.warn('[Frank] ws handler failed:', error));
  });
  ws.addEventListener('close', () => onWsClose());
  ws.addEventListener('error', () => {
    // The close event will follow and trigger reconnect.
  });
}

function buildWsUrl(settings) {
  const base = new URL(settings.apiBaseUrl);
  base.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:';
  const path = base.pathname.replace(/\/+$/, '');
  base.pathname = `${path}/api/v1/ws`;
  base.searchParams.set('token', settings.authToken);
  return base.toString();
}

async function subscribeAllActiveJobs() {
  if (wsState !== 'open' || !ws) return;
  const jobs = await loadActiveJobs();
  const jobIds = Array.from(new Set(Object.values(jobs).map((j) => j.jobId).filter(Boolean)));
  if (!jobIds.length) return;
  try {
    ws.send(JSON.stringify({ type: 'subscribe', job_ids: jobIds }));
  } catch (error) {
    console.warn('[Frank] ws subscribe send failed:', error);
  }
}

function subscribeJob(jobId) {
  if (!jobId || wsState !== 'open' || !ws) return;
  try {
    ws.send(JSON.stringify({ type: 'subscribe', job_ids: [jobId] }));
  } catch (error) {
    console.warn('[Frank] ws subscribe send failed:', error);
  }
}

function closeWebSocket() {
  if (wsReconnectTimer) {
    clearTimeout(wsReconnectTimer);
    wsReconnectTimer = null;
  }
  if (ws) {
    try { ws.close(1000); } catch { /* ignore */ }
    ws = null;
  }
  wsState = 'closed';
  wsBackoffMs = WS_BACKOFF_INITIAL_MS;
}

function onWsClose() {
  ws = null;
  wsState = 'closed';
  scheduleWsReconnect();
}

function scheduleWsReconnect() {
  if (wsReconnectTimer) return;
  const delay = wsBackoffMs;
  wsBackoffMs = Math.min(wsBackoffMs * 2, WS_BACKOFF_MAX_MS);
  wsReconnectTimer = setTimeout(() => {
    wsReconnectTimer = null;
    ensureWebSocket().catch((error) => console.warn('[Frank] ws reconnect failed:', error));
  }, delay);
}

async function handleWsMessage(event) {
  let payload;
  try {
    payload = JSON.parse(typeof event.data === 'string' ? event.data : '');
  } catch {
    return;
  }
  if (!payload || payload.type !== 'job_complete') return;
  const jobId = String(payload.job_id || '');
  if (!jobId) return;

  const settings = await getSettings();
  if (!settings.apiBaseUrl || !settings.authToken) return;

  const jobs = await loadActiveJobs();
  const recordId = Object.keys(jobs).find((k) => jobs[k].jobId === jobId);
  if (!recordId) return;
  const job = jobs[recordId];

  if (payload.status === 'completed') {
    const ok = await finalizeJobCompletion(settings, job, jobs, recordId, {
      imageUrl: payload.image_url,
      pageKind: payload.page_kind,
      sourceHash: payload.source_hash,
      source: 'ws',
    });
    if (ok) await saveActiveJobs(jobs);
  } else if (payload.status === 'failed') {
    delete jobs[recordId];
    await saveActiveJobs(jobs);
    await recordEvent({ site: job.site, level: 'error', message: `Failed ${job.site} job ${job.jobId} (ws): ${payload.error || 'Job failed'}` });
    await notifyTab(job, {
      type: 'FRANK_JOB_FAILED',
      pageId: job.pageId,
      site: job.site,
      jobId: job.jobId,
      error: payload.error || 'Job failed',
    });
  }

  await manageWebSocketLifecycle();
}

async function manageWebSocketLifecycle() {
  const settings = await getSettings();
  // API URL changed (user reconfigured) → tear down so the next ensure picks
  // the new base.
  if (wsApiBase && settings.apiBaseUrl && wsApiBase !== settings.apiBaseUrl) {
    closeWebSocket();
  }
  await ensureWebSocket(settings);
}

async function notifyTab(job, message) {
  if (typeof job.tabId !== 'number') return;
  try {
    const options = typeof job.frameId === 'number' ? { frameId: job.frameId } : undefined;
    await chrome.tabs.sendMessage(job.tabId, message, options);
  } catch (error) {
    console.warn('[Frank] could not notify tab:', error);
  }
}

function ensurePollingAlarm() {
  chrome.alarms.create(POLL_FALLBACK_ALARM_NAME, { periodInMinutes: 0.5 });
}

function schedulePollSoon() {
  if (pollTimer) clearTimeout(pollTimer);
  chrome.alarms.create(POLL_ALARM_NAME, { when: Date.now() + TIMING.pollDelayMs });
  pollTimer = setTimeout(() => {
    pollTimer = null;
    pollActiveJobs().catch((error) => console.warn('[Frank] scheduled poll failed:', error));
  }, TIMING.pollDelayMs);
  ensurePollingAlarm();
}

async function loadActiveJobs() {
  const stored = await chrome.storage.local.get(STORAGE_KEYS.activeJobs);
  const jobs = stored[STORAGE_KEYS.activeJobs];
  return jobs && typeof jobs === 'object' ? jobs : {};
}

async function saveActiveJobs(jobs) {
  await chrome.storage.local.set({ [STORAGE_KEYS.activeJobs]: jobs });
}

async function loadDiagnostics() {
  const stored = await chrome.storage.local.get(STORAGE_KEYS.diagnostics);
  const diagnostics = stored[STORAGE_KEYS.diagnostics];
  return Array.isArray(diagnostics) ? diagnostics : [];
}

async function recordEvent(event) {
  const diagnostics = await loadDiagnostics();
  diagnostics.unshift({
    ts: Date.now(),
    site: safeText(event.site, 40),
    level: event.level === 'error' ? 'error' : 'info',
    message: safeText(event.message, 300),
  });
  await chrome.storage.local.set({ [STORAGE_KEYS.diagnostics]: diagnostics.slice(0, SIZE_LIMITS.maxDiagnostics) });
}

function authHeaders(settings) {
  return { Authorization: `Bearer ${settings.authToken}` };
}

function fetchWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  return fetch(url, { ...options, signal: controller.signal })
    .finally(() => clearTimeout(timer));
}

async function withRetry(action, maxAttempts = 3) {
  let lastError;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      return await action();
    } catch (error) {
      lastError = error;
      if (!error.retryable && error.name !== 'AbortError' && !(error instanceof TypeError)) break;
      if (attempt === maxAttempts) break;
      await sleep(Math.min(2 ** attempt, 4) * 1000);
    }
  }
  throw lastError;
}

function retryableError(message, retryable) {
  const error = new Error(message);
  error.retryable = retryable;
  return error;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function dataUrlToBlobAndBytes(dataUrl) {
  const response = await fetch(dataUrl);
  const blob = await response.blob();
  const bytes = await blob.arrayBuffer();
  return { blob, bytes };
}

function blobToDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error('failed to read image blob'));
    reader.onload = () => resolve(String(reader.result));
    reader.readAsDataURL(blob);
  });
}

async function sha256Hex(arrayBuffer) {
  const digest = await crypto.subtle.digest('SHA-256', arrayBuffer);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function openDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onerror = () => reject(request.error);
    request.onsuccess = () => resolve(request.result);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(IMAGE_STORE)) {
        const store = db.createObjectStore(IMAGE_STORE, { keyPath: 'key' });
        store.createIndex('lastAccessed', 'lastAccessed');
      }
    };
  });
}

async function cacheGet(key) {
  const db = await openDb();
  try {
    const entry = await idbRequest(db.transaction(IMAGE_STORE, 'readonly').objectStore(IMAGE_STORE).get(key));
    if (!entry?.dataUrl) return null;
    entry.lastAccessed = Date.now();
    await cachePut(key, entry.dataUrl, entry.meta || {});
    return entry.dataUrl;
  } finally {
    db.close();
  }
}

async function cachePut(key, dataUrl, meta = {}) {
  const db = await openDb();
  try {
    const now = Date.now();
    const store = db.transaction(IMAGE_STORE, 'readwrite').objectStore(IMAGE_STORE);
    await idbRequest(store.put({ key, dataUrl, meta, bytes: dataUrl.length, createdAt: now, lastAccessed: now }));
  } finally {
    db.close();
  }
  await evictCacheIfNeeded();
}

async function evictCacheIfNeeded() {
  const db = await openDb();
  try {
    const transaction = db.transaction(IMAGE_STORE, 'readwrite');
    const store = transaction.objectStore(IMAGE_STORE);
    const entries = await idbRequest(store.getAll());
    let totalBytes = entries.reduce((sum, entry) => sum + Number(entry.bytes || entry.dataUrl?.length || 0), 0);
    if (entries.length <= SIZE_LIMITS.maxCacheEntries && totalBytes <= SIZE_LIMITS.maxCacheBytes) return;
    entries.sort((a, b) => (a.lastAccessed || 0) - (b.lastAccessed || 0));
    const toDelete = [];
    while ((entries.length - toDelete.length > SIZE_LIMITS.maxCacheEntries || totalBytes > SIZE_LIMITS.maxCacheBytes) && entries.length > toDelete.length) {
      const entry = entries[toDelete.length];
      toDelete.push(entry);
      totalBytes -= Number(entry.bytes || entry.dataUrl?.length || 0);
    }
    await Promise.all(toDelete.map((entry) => idbRequest(store.delete(entry.key))));
  } finally {
    db.close();
  }
}

function idbRequest(request) {
  return new Promise((resolve, reject) => {
    request.onerror = () => reject(request.error);
    request.onsuccess = () => resolve(request.result);
  });
}

function restrictStorageAccess() {
  try {
    const result = chrome.storage.local.setAccessLevel?.({ accessLevel: 'TRUSTED_CONTEXTS' });
    if (result && typeof result.catch === 'function') result.catch(() => {});
  } catch {
    // Older Chromium builds may not support setAccessLevel. The token still
    // stays out of content-script messages; this is defense-in-depth.
  }
}
