(function frankKindleModule() {
  'use strict';

  function runtimeAlive() {
    try {
      return Boolean(chrome.runtime?.id);
    } catch {
      return false;
    }
  }

  if (window.FrankKindle) {
    if (window.FrankKindle.alive?.()) return;
    window.FrankKindle.destroy?.();
  }

  const SPREAD_THRESHOLD = 1.3;
  const DETECT_INTERVAL_MS = 450;
  const MAX_CAPTURE_SIDE = 2200;
  const RECENT_USER_NAV_MS = 4000;
  const REPAINT_GEOMETRY_TOLERANCE = 0.02;
  const REPAINT_SUPPRESS_MS = 600;
  const QUEUED_DETECTION_DELAY_MS = 250;
  const SUBMIT_DEBOUNCE_MS = 550;
  const REAPPLY_DELAYS_MS = [800, 1800, 3500];
  const MIN_PAGE_SIDE_PX = 100;
  const MIN_VISIBLE_OVERLAP_PX2 = 2000;
  const LOADER_VISIBLE_OVERLAP_PX2 = 1600;
  const NO_TARGET_REPORT_INTERVAL_MS = 15000;
  const MAX_CONSECUTIVE_FAILURES = 3;
  // A re-paginating book settles in a few seconds; a page that never settles
  // is not worth chasing round a loop.
  const MAX_RECAPTURES = 2;
  const RECAPTURE_DELAY_MS = 1200;
  // A reflowable book is laid out progressively: Kindle paints a provisional
  // page, then replaces it once it has finished paginating. Capturing during
  // that produces a page the reader never sees — deterministically, so every
  // such capture is byte-identical and cache-hits the same stale render.
  // Fixed-layout manga settles at once and is unaffected by the wait.
  const SETTLE_MS = 1500;
  const PROSE_VERDICTS_BEFORE_SWITCH = 2;
  // A loader that never goes away is not a loader. The selector below is loose
  // enough to match unrelated furniture, and a false match used to disable
  // detection for the life of the page without saying so.
  const LOADER_PATIENCE_MS = 5000;
  const MAX_DEBUG_ENTRIES = 20;
  const MAX_DEBUG_PAYLOADS = 3;

  let started = false;
  let settings = {};
  let sessionId = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  let pageCounter = 0;
  let lastBlob = '';
  let lastRect = null;
  let lastEmitAt = 0;
  let userNavAt = Date.now();
  let navIntent = 'forward';
  let activeGroups = 0;
  let queuedDetection = null;
  let submitDebounceTimer = null;
  let lastNoTargetReportAt = 0;
  // A page that fails for a reason the page cannot change — the wrong pipeline
  // for this book, say — fails identically every time. Kindle regenerates blob
  // URLs on its own, so each churn would resubmit the same pixels forever.
  let consecutiveFailures = 0;
  let lastFailureError = '';
  let autoSubmitPaused = false;
  // Renders that came back describing something other than the page they were
  // bound to. Two in a row on a manga pipeline means the book is prose.
  let recaptures = 0;
  let proseVerdicts = 0;
  // A book is corrected at most once. A volume with pages of both kinds would
  // otherwise be switched back and forth for as long as it is open.
  const correctedBooks = new Set();
  let detectTimer = null;
  let lastHref = location.href;
  let loaderSince = 0;
  let loaderOverriddenAt = 0;
  const processedBlobs = new Set();
  const MAX_PROCESSED_BLOBS = 200;
  const spreadGroups = new Map();
  const debugEntries = new Map();

  window.FrankKindle = { start, updateSettings, state, alive: runtimeAlive, destroy };

  /// Stand down so a freshly injected copy can take the page over.
  function destroy() {
    started = false;
    if (detectTimer) window.clearInterval(detectTimer);
    if (submitDebounceTimer) window.clearTimeout(submitDebounceTimer);
    detectTimer = null;
    delete window.FrankKindle;
  }

  /// Settings changed in the popup while this page was open.
  ///
  /// Without this the strategy keeps the settings it started with, so
  /// switching pipeline needed a reload to take effect.
  function updateSettings(nextSettings) {
    const previous = effectivePipeline();
    settings = nextSettings || {};
    if (effectivePipeline() === previous) return;
    resumeAutoSubmit();
    recaptures = 0;
    // Re-detect the page in front of the reader under the new pipeline, and
    // drop what the old one produced: it answers a different question.
    lastBlob = '';
    processedBlobs.clear();
    window.FrankLens?.clear();
    window.FrankStatus?.set('idle');
    window.setTimeout(detectPageChange, 100);
  }

  function state() {
    const target = findVisibleBlob();
    return {
      // Which build is actually running: a released zip and a working copy
      // have looked identical before, which made every other answer suspect.
      version: chrome.runtime?.getManifest?.().version ?? 'unknown',
      started,
      configured: Boolean(settings.configured),
      kindleEnabled: settings.kindleEnabled !== false,
      book: bookId(),
      pipeline: effectivePipeline(),
      defaultPipeline: settings.mangaPipeline,
      autoSubmitPaused,
      lastFailureError,
      consecutiveFailures,
      // Why detection is or is not producing pages.
      frameHostsReader: frameHostsKindleReader(),
      loaderPresent: loaderPresent(),
      loaderBlocking: loaderVisible(),
      pageImageFound: Boolean(target),
      pageImageSize: target
        ? `${Math.round(target.getBoundingClientRect().width)}x${Math.round(target.getBoundingClientRect().height)}`
        : null,
      // More than one means a scored match could pick the wrong page.
      pageImageCandidates: document.querySelectorAll('img[src^="blob:"]').length,
      pageMode: target ? spreadMode(target.getBoundingClientRect()) : null,
      pagesDetected: pageCounter,
      pagesSubmitted: processedBlobs.size,
    };
  }

  /// Opening a different book leaves nothing of the last one behind.
  function noteNavigation() {
    if (location.href === lastHref) return;
    lastHref = location.href;
    recaptures = 0;
    lastBlob = '';
    lastRect = null;
    processedBlobs.clear();
    window.FrankLens?.clear();
    resumeAutoSubmit();
    report('info', 'Navigated; cleared translations from the previous book');
  }

  /// The worker's verdict on what kind of page it just processed.
  ///
  /// A manga pipeline reporting prose means this book is a novel, which is a
  /// far better signal than a render that came back looking wrong: it comes
  /// from the page itself rather than from what we made of it. Two in a row,
  /// so one text page in a manga volume does not move the whole book.
  function notePageKind(kind) {
    const pipeline = effectivePipeline();
    if (kind !== 'prose' || !pipeline.startsWith('manga_')) {
      proseVerdicts = 0;
      return;
    }
    proseVerdicts += 1;
    if (proseVerdicts < PROSE_VERDICTS_BEFORE_SWITCH) return;
    proseVerdicts = 0;
    switchBookPipeline('book_furigana',
      `${bookId()} reads as typeset prose; switching it to the text-book pipeline.`);
  }

  function switchBookPipeline(pipeline, reason) {
    const book = bookId();
    if (!book || correctedBooks.has(book)) return;
    correctedBooks.add(book);
    report('info', reason);
    chrome.runtime.sendMessage({ type: 'SET_BOOK_PIPELINE', bookId: book, pipeline })
      ?.catch?.(() => {});
  }

  /// A render arrived that does not depict the page it was bound to.
  ///
  /// A reflowable book re-paginates while a job is in flight — Kindle is still
  /// settling for seconds after a load — so the page that comes back is a real
  /// render of a page the reader has already left. Discarding it is right;
  /// leaving the reader with nothing is not. Capture what is on screen now.
  /// Which side of the mismatch is wrong.
  ///
  /// The render is compared against the image that was actually submitted. If
  /// they match, the render is good and the page moved underneath it — a
  /// reflowable book re-paginating while the job ran. If they do not, the
  /// result belongs to some other capture entirely, which is a different bug
  /// and worth saying so.
  async function explainMismatch(detail) {
    const captured = debugEntries.get(detail.pageId)?.originalDataUrl;
    if (!captured || !detail.renderUrl || !window.FrankLens?.compare) return;
    const difference = await window.FrankLens.compare(detail.renderUrl, captured);
    if (difference === null) return;
    report('info', difference <= 0.5
      ? `The render matches the page that was submitted (${difference.toFixed(2)}), `
        + 'so the reader moved on while it was being made.'
      : `The render does not match the page that was submitted either `
        + `(${difference.toFixed(2)}); it belongs to a different capture.`);
  }

  function noteRenderMismatch(detail = {}) {
    // Keep the refused render alongside the page it was refused for, so
    // "Send debug pages to server" can hand over both for comparison.
    if (detail.pageId && detail.renderUrl) {
      rememberDebug(detail.pageId, { pageId: detail.pageId, site: 'kindle',
                                     translatedDataUrl: detail.renderUrl });
    }
    explainMismatch(detail).catch(() => {});
    recaptures += 1;
    if (recaptures > MAX_RECAPTURES) {
      report('error',
        'The page keeps changing under each render; stopping until you turn a page.');
      autoSubmitPaused = true;
      // Nothing is coming, so the reader should not be left holding on a ring
      // that promises otherwise.
      window.FrankLens?.clearPending?.();
      window.FrankStatus?.set('failed');
      return;
    }
    report('info', 'The page changed while it was being translated; capturing it again.');
    lastBlob = '';
    processedBlobs.clear();
    window.setTimeout(detectPageChange, RECAPTURE_DELAY_MS);
  }

  function resumeAutoSubmit() {
    consecutiveFailures = 0;
    lastFailureError = '';
    autoSubmitPaused = false;
  }

  function start(nextSettings) {
    if (started) return;
    started = true;
    settings = nextSettings || {};
    // Kindle begins selecting from the press itself, so the lens has to take
    // mouse presses before the reader sees them and hand back the taps.
    window.FrankLens?.setPressCapture?.(true);
    window.FrankLens?.onRenderMismatch?.(noteRenderMismatch);
    installListeners();
    detectTimer = window.setInterval(detectPageChange, DETECT_INTERVAL_MS);
    window.setTimeout(detectPageChange, 400);
    console.info('[Frank] Kindle strategy started');
    report('info', 'Kindle strategy started');
  }

  function installListeners() {
    document.addEventListener('click', (event) => {
      if (typeof event.clientX === 'number') {
        navIntent = event.clientX <= window.innerWidth / 2 ? 'forward' : 'backward';
      }
      userNavAt = Date.now();
      window.setTimeout(detectPageChange, 500);
    });
    for (const eventName of ['pointerdown', 'mousedown', 'touchstart']) {
      document.addEventListener(eventName, () => {
        userNavAt = Date.now();
        window.setTimeout(detectPageChange, 420);
      }, true);
    }
    document.addEventListener('wheel', () => { userNavAt = Date.now(); }, { passive: true });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowLeft') navIntent = 'forward';
      if (event.key === 'ArrowRight') navIntent = 'backward';
      userNavAt = Date.now();
    });
    document.addEventListener('keyup', () => {
      userNavAt = Date.now();
      window.setTimeout(detectPageChange, 500);
    });
    window.addEventListener('resize', () => {
      lastBlob = '';
      userNavAt = Date.now();
      window.setTimeout(detectPageChange, 1000);
    });
    chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
      if (message?.type === 'FRANK_JOB_COMPLETE' && message.site === 'kindle') {
        notePageKind(message.pageKind);
        handleJobComplete(message);
      }
      if (message?.type === 'FRANK_JOB_FAILED' && message.site === 'kindle') handleJobFailed(message);
      if (message?.type === 'FRANK_FORCE_REPROCESS_CURRENT') {
        if (!frameHostsKindleReader()) return false;
        forceReprocessCurrent().then(sendResponse).catch((error) => sendResponse({ ok: false, error: error.message || String(error) }));
        return true;
      }
      if (message?.type === 'FRANK_GET_BOOK') {
        if (!frameHostsKindleReader()) return false;
        sendResponse({
          ok: true,
          site: 'kindle',
          bookId: bookId(),
          pipeline: effectivePipeline(),
          usingDefault: !settings.bookPipelines?.[bookId()],
        });
        return true;
      }
      if (message?.type === 'FRANK_EXPORT_DEBUG_PAIR') {
        if (!frameHostsKindleReader()) return false;
        exportDebugPair().then(sendResponse).catch((error) => sendResponse({ ok: false, error: error.message || String(error) }));
        return true;
      }
      return false;
    });
  }

  // Kindle injects content scripts into every frame (allFrames: true). Sub-frames
  // like the javascript:void(0) telemetry shim don't host the reader and would
  // race to reply "No current Kindle page image found" before the real frame
  // finishes its work. Only the frame that actually contains a Kindle reader DOM
  // should respond to popup-driven actions.
  function frameHostsKindleReader() {
    return !!document.querySelector(
      '#kr-renderer, #kindle-reader-content, .reader-content, ' +
      '[id*="kindle-reader"], [id*="kr-renderer"], [class*="reader-content"]',
    );
  }

  function detectPageChange() {
    if (!settings.configured || settings.kindleEnabled === false) return;
    noteNavigation();
    if (loaderVisible()) return;
    const target = findVisibleBlob();
    if (!target) {
      reportNoTarget();
      return;
    }
    const blobSrc = target.src;
    if (!blobSrc || blobSrc === lastBlob || processedBlobs.has(blobSrc)) return;

    const rect = target.getBoundingClientRect();
    const now = Date.now();
    const userNavRecent = now - userNavAt < RECENT_USER_NAV_MS;
    if (!userNavRecent && lastRect) {
      const dw = Math.abs(rect.width - lastRect.width) / Math.max(1, lastRect.width);
      const dh = Math.abs(rect.height - lastRect.height) / Math.max(1, lastRect.height);
      if (dw < REPAINT_GEOMETRY_TOLERANCE && dh < REPAINT_GEOMETRY_TOLERANCE && now - lastEmitAt < REPAINT_SUPPRESS_MS) return;
    }

    lastBlob = blobSrc;
    lastRect = { width: rect.width, height: rect.height };
    lastEmitAt = now;
    pageCounter += 1;

    const pageMode = spreadMode(rect);
    const pageId = `kindle-${sessionId}-${pageCounter}${pageMode === 'spread' ? '-spread' : ''}`;
    const detection = {
      pageId,
      index: pageCounter,
      pageMode,
      navIntent,
      imgSrc: blobSrc,
      naturalWidth: target.naturalWidth,
      naturalHeight: target.naturalHeight,
      rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      devicePixelRatio: window.devicePixelRatio || 1,
      kindlePage: findKindlePage(),
    };
    report('info', `Detected Kindle ${pageMode} page ${pageCounter}`);
    // Detection still runs while submission is paused, because this is also
    // what releases the page the reader has left.
    // Kindle reuses the same <img> across turns: retarget before the new
    // page's translation lands, so a peek cannot magnify the page just left,
    // and the previous render is released instead of accumulating.
    window.FrankOverlay?.releasePagesExcept(pageId, target);
    if (autoSubmitPaused) return;
    window.FrankStatus?.set('capturing');
    scheduleSubmit(detection);
  }

  function scheduleSubmit(detection) {
    queuedDetection = detection;
    if (activeGroups > 0) return;
    scheduleQueuedFlush(SUBMIT_DEBOUNCE_MS);
  }

  function scheduleQueuedFlush(delayMs) {
    if (submitDebounceTimer) window.clearTimeout(submitDebounceTimer);
    submitDebounceTimer = window.setTimeout(flushQueuedDetection, delayMs);
  }

  function scheduleQueuedFlushAfterActive() {
    if (activeGroups === 0 && queuedDetection) {
      scheduleQueuedFlush(QUEUED_DETECTION_DELAY_MS);
    }
  }

  function flushQueuedDetection() {
    submitDebounceTimer = null;
    if (activeGroups > 0 || !queuedDetection) return;
    const detection = queuedDetection;
    activeGroups += 1;
    queuedDetection = null;
    submitDetection(detection).finally(() => {
      // Release submission gating once capture + server enqueue are done. Spread
      // completion is tracked separately for stitching and must not block newer
      // page detections from updating the server-side latest marker.
      activeGroups = Math.max(0, activeGroups - 1);
      scheduleQueuedFlushAfterActive();
    });
  }

  /// Wait until the element has actually decoded the page we mean to capture.
  ///
  /// An <img> keeps painting its previous frame until a new src decodes, and
  /// drawImage copies what is painted. Kindle regenerates blob URLs constantly
  /// and we notice within half a second, so capturing straight away yields the
  /// page before this one — every time, byte for byte, which is why those
  /// captures cache-hit and why their renders never matched the page.
  /// What the element is currently showing, as a value that changes whenever
  /// the page does.
  function frameId(target) {
    return `${target.src}|${target.naturalWidth}x${target.naturalHeight}`;
  }

  /// Wait for the page to stop changing before capturing it.
  ///
  /// Returns false if it is still moving, in which case the next detection
  /// picks it up — better than submitting a layout the reader never sees.
  async function settled(target) {
    const before = frameId(target);
    await new Promise((resolve) => window.setTimeout(resolve, SETTLE_MS));
    return frameId(target) === before;
  }

  async function decoded(target, expectedSrc) {
    if (expectedSrc && target.src !== expectedSrc) return false;
    if (typeof target.decode !== 'function') return target.complete !== false;
    try {
      await target.decode();
    } catch {
      return false;   // the src changed under us; the next detection handles it
    }
    return !expectedSrc || target.src === expectedSrc;
  }

  async function submitDetection(detection, force = false) {
    const target = findImageBySrc(detection.imgSrc) || findVisibleBlob();
    if (!target) return;
    if (!await decoded(target, target.src) || !await settled(target)) {
      report('info', 'The page was still being laid out; leaving it for the next detection.');
      window.FrankStatus?.set('idle');
      return;
    }
    if (!await decoded(target, target.src)) return;
    // Remember which element this capture came from. Scoring cannot tell one
    // page image from its neighbour: they are the same size, in the same
    // place, and both visible during a turn.
    target.dataset.frankCapturedPage = detection.pageId;
    processedBlobs.add(detection.imgSrc);
    while (processedBlobs.size > MAX_PROCESSED_BLOBS) {
      processedBlobs.delete(processedBlobs.values().next().value);
    }

    try {
      if (detection.pageMode === 'spread') {
        await submitSpread(target, detection, force);
      } else {
        const imageDataUrl = captureImage(target, 'full');
        if (!imageDataUrl) throw new Error('Kindle capture failed');
        rememberDebug(detection.pageId, { pageId: detection.pageId, site: 'kindle', pageMode: 'single', originalSrc: detection.imgSrc, originalDataUrl: imageDataUrl, capture: detection });
        report('info', `Captured Kindle page ${detection.index} (${formatBytes(imageDataUrl.length)})`);
        const response = await submitCapture({ ...detection, pageMode: 'single' }, imageDataUrl, detection.pageId, force);
        if (response.status === 'completed') await applyKindle(response);
      }
    } catch (error) {
      if (spreadGroups.has(detection.pageId)) spreadGroups.delete(detection.pageId);
      processedBlobs.delete(detection.imgSrc);
      console.warn('[Frank] Kindle submit failed:', error);
      report('error', `Kindle submit failed: ${error.message || error}`);
    }
  }

  async function submitSpread(target, detection, force = false) {
    const left = captureImage(target, 'left');
    const right = captureImage(target, 'right');
    const full = captureImage(target, 'full');
    if (!left || !right) throw new Error('Kindle spread capture failed');
    await submitSpreadCaptures(detection, { left, right, full }, force);
  }

  async function submitSpreadCaptures(detection, captures, force = false) {
    const { left, right, full } = captures;
    if (!left || !right) throw new Error('Kindle spread capture failed');
    if (full) {
      rememberDebug(detection.pageId, {
        pageId: detection.pageId,
        site: 'kindle',
        pageMode: 'spread',
        originalSrc: detection.imgSrc,
        originalDataUrl: full,
        originalSides: { left, right },
        capture: detection,
      });
    }
    report('info', `Captured Kindle spread halves (${formatBytes(left.length)} + ${formatBytes(right.length)})`);

    const group = {
      pageId: detection.pageId,
      detection,
      sides: {},
      pending: 2,
    };
    spreadGroups.set(detection.pageId, group);

    const leftId = `${detection.pageId}-left`;
    const rightId = `${detection.pageId}-right`;
    const leftResponse = await submitCapture({ ...detection, groupId: detection.pageId, side: 'left' }, left, leftId, force);
    const rightResponse = await submitCapture({ ...detection, groupId: detection.pageId, side: 'right' }, right, rightId, force);
    if (leftResponse.status === 'completed') await handleSpreadSide(leftResponse);
    if (rightResponse.status === 'completed') await handleSpreadSide(rightResponse);
  }

  async function submitCapture(capture, imageDataUrl, pageId, force = false) {
    const metadata = parseKindleMetadata(capture, pageId);
    const response = await chrome.runtime.sendMessage({
      type: 'SUBMIT_CAPTURE',
      site: 'kindle',
      pageId,
      pipeline: effectivePipeline(),
      priority: 'high',
      metadata,
      capture,
      imageDataUrl,
      force,
    });
    if (!response?.ok) throw new Error(response?.error || 'submit failed');
    // Handed to the server: from here the wait is theirs, not ours.
    if (response.status !== 'completed') window.FrankStatus?.set('queued');
    report('info', `Kindle capture submitted: ${pageId} (${response.status || 'unknown'})`);
    return response;
  }

  async function handleJobComplete(message) {
    if (message.capture?.groupId) {
      await handleSpreadSide(message);
      return;
    }
    await applyKindle(message);
    finishGroup();
  }

  function handleJobFailed(message) {
    const error = message.error || 'unknown error';
    console.warn('[Frank] Kindle job failed:', error);
    report('error', `Kindle job failed: ${error}`);
    if (message.capture?.groupId) spreadGroups.delete(message.capture.groupId);
    finishGroup();
    window.FrankStatus?.set('failed');
    noteFailure(error);
  }

  /// Give up on auto-submitting once the same error repeats.
  ///
  /// The page cannot fix a mismatched pipeline by being sent again, and
  /// Kindle's blob churn would keep sending it. Changing a setting or forcing
  /// a reprocess resumes.
  function noteFailure(error) {
    if (/use a manga pipeline/i.test(error) && bookId()) {
      switchBookPipeline('manga_furigana', `${bookId()} is not a text book; switching it back.`);
      return;
    }
    consecutiveFailures = error === lastFailureError ? consecutiveFailures + 1 : 1;
    lastFailureError = error;
    if (autoSubmitPaused || consecutiveFailures < MAX_CONSECUTIVE_FAILURES) return;
    autoSubmitPaused = true;
    report('error',
      `Auto-translate paused after ${consecutiveFailures} failures: ${error}. ` +
      'Change the pipeline in the popup, or force reprocess, to resume.');
  }

  async function handleSpreadSide(message) {
    const groupId = message.capture?.groupId;
    const side = message.capture?.side;
    const group = spreadGroups.get(groupId);
    if (!group || (side !== 'left' && side !== 'right')) return;
    if (!group.sides[side]) group.pending -= 1;
    group.sides[side] = message.imageDataUrl;
    if (group.pending > 0) return;

    try {
      const stitched = await stitchSpread(group.sides.left, group.sides.right);
      await applyKindle({
        type: 'FRANK_JOB_COMPLETE',
        site: 'kindle',
        pageId: group.pageId,
        imageDataUrl: stitched,
        capture: group.detection,
      });
    } finally {
      spreadGroups.delete(groupId);
      finishGroup();
    }
  }

  async function applyKindle(message) {
    const ok = await window.FrankOverlay?.applyKindleResult(message);
    if (ok) {
      resumeAutoSubmit();
      window.FrankStatus?.set('ready');
      rememberDebug(message.pageId, { pageId: message.pageId, site: 'kindle', translatedDataUrl: message.imageDataUrl, capture: message.capture });
      report('info', `Kindle translated image applied: ${message.pageId || 'unknown page'}`);
      // Nothing is swapped into the DOM in lens mode, so a Kindle repaint has
      // nothing to clobber and there is nothing to re-apply.
      if (!window.FrankOverlay?.isLensMode()) {
        for (const delay of REAPPLY_DELAYS_MS) {
          window.setTimeout(() => window.FrankOverlay?.applyKindleResult(message), delay);
        }
      }
    }
    if (!ok) report('error', `Kindle translated image was ready but could not be applied: ${message.pageId || 'unknown page'}`);
    return ok;
  }

  async function forceReprocessCurrent() {
    if (!settings.configured || settings.kindleEnabled === false) throw new Error('Kindle support is not enabled or configured.');
    const target = findVisibleKindleImage();
    if (!target) throw new Error('No current Kindle page image found. Reload or turn the page and try again.');
    const entry = debugEntryForImage(target);
    let capture = entry?.capture || captureForTarget(target, entry?.pageId || `kindle-${sessionId}-manual-${Date.now()}`);
    const pageId = `${capture.pageId || entry?.pageId || `kindle-${sessionId}-manual`}-force-${Date.now()}`;
    capture = { ...capture, pageId };
    if (capture.pageMode === 'spread') {
      if (target.dataset.frankTranslated === 'true') {
        const originalSides = entry?.originalSides || (entry?.originalDataUrl ? await splitSpreadDataUrl(entry.originalDataUrl) : null);
        if (!originalSides?.left || !originalSides?.right) {
          throw new Error('Original spread capture is unavailable for the translated page. Reload or turn the page to let Frank recapture the original, then try again.');
        }
        await submitSpreadCaptures(capture, { ...originalSides, full: entry?.originalDataUrl }, true);
      } else {
        await submitSpread(target, capture, true);
      }
      return { ok: true, site: 'kindle', pageId, message: 'Forced Kindle spread reprocess submitted.' };
    }

    let imageDataUrl = null;
    if (target.dataset.frankTranslated === 'true') {
      imageDataUrl = entry?.originalDataUrl || null;
      if (!imageDataUrl) throw new Error('Original capture is unavailable for the translated page. Reload or turn the page to let Frank recapture the original, then try again.');
    } else {
      imageDataUrl = captureImage(target, 'full');
    }
    if (!imageDataUrl) throw new Error('Current Kindle page could not be captured.');
    rememberDebug(pageId, { pageId, site: 'kindle', pageMode: capture.pageMode, originalSrc: capture.imgSrc, originalDataUrl: imageDataUrl, capture });
    const response = await submitCapture(capture, imageDataUrl, pageId, true);
    if (response.status === 'completed') await applyKindle(response);
    return { ok: true, site: 'kindle', pageId, message: 'Forced Kindle reprocess submitted.' };
  }

  async function exportDebugPair() {
    const target = findVisibleKindleImage();
    if (!target) throw new Error('No current Kindle page image found.');
    const entry = debugEntryForImage(target);
    // A fresh snapshot of what the reader is looking at right now, rather than
    // the copy kept from capture time. When a render is refused as not
    // depicting its page, the difference between these two is the evidence: it
    // says whether the wrong page was captured, or the right one measured
    // wrongly.
    const originalDataUrl = captureImage(target, 'full') || entry?.originalDataUrl;
    const translatedDataUrl = entry?.translatedDataUrl || (target.dataset.frankTranslated === 'true' ? await dataUrlFromSrc(target.src) : null);
    if (!originalDataUrl) throw new Error('Original debug image unavailable. Reload or turn the page to let Frank recapture the original.');
    if (!translatedDataUrl) throw new Error('Translated debug image unavailable for the current page.');
    return {
      ok: true,
      site: 'kindle',
      pageId: entry?.pageId || target.dataset.frankPageId || 'current',
      sourceUrl: location.href,
      capture: entry?.capture || null,
      originalDataUrl,
      translatedDataUrl,
    };
  }

  function finishGroup() {
    activeGroups = Math.max(0, activeGroups - 1);
    scheduleQueuedFlushAfterActive();
  }

  function captureImage(target, part) {
    const rect = target.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const renderW = Math.max(1, Math.round(rect.width * dpr));
    const renderH = Math.max(1, Math.round(rect.height * dpr));
    const side = Math.max(renderW, renderH);
    const scale = side > MAX_CAPTURE_SIDE ? MAX_CAPTURE_SIDE / side : 1;
    const fullW = Math.max(1, Math.round(renderW * scale));
    const fullH = Math.max(1, Math.round(renderH * scale));
    const sourceW = target.naturalWidth || target.width;
    const sourceH = target.naturalHeight || target.height;
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    if (!ctx) return null;

    if (part === 'left' || part === 'right') {
      const halfSourceW = Math.floor(sourceW / 2);
      const sx = part === 'left' ? 0 : halfSourceW;
      const sw = part === 'left' ? halfSourceW : sourceW - halfSourceW;
      canvas.width = Math.max(1, Math.round(fullW * (sw / sourceW)));
      canvas.height = fullH;
      ctx.drawImage(target, sx, 0, sw, sourceH, 0, 0, canvas.width, canvas.height);
    } else {
      canvas.width = fullW;
      canvas.height = fullH;
      ctx.drawImage(target, 0, 0, fullW, fullH);
    }

    try {
      return canvas.toDataURL('image/png');
    } catch (error) {
      console.warn('[Frank] Kindle canvas capture blocked:', error);
      return null;
    }
  }

  async function stitchSpread(leftDataUrl, rightDataUrl) {
    const [left, right] = await Promise.all([loadImage(leftDataUrl), loadImage(rightDataUrl)]);
    const canvas = document.createElement('canvas');
    canvas.width = left.naturalWidth + right.naturalWidth;
    canvas.height = Math.max(left.naturalHeight, right.naturalHeight);
    const ctx = canvas.getContext('2d');
    ctx.drawImage(left, 0, 0);
    ctx.drawImage(right, left.naturalWidth, 0);
    return canvas.toDataURL('image/png');
  }

  function loadImage(src) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error('failed to load translated spread half'));
      img.src = src;
    });
  }

  function findVisibleBlob() {
    const root = findReaderRoot();
    let imgs = Array.from(root.querySelectorAll('img'));
    if (!imgs.length) imgs = Array.from(document.querySelectorAll('img'));
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const rootRect = root.getBoundingClientRect ? root.getBoundingClientRect() : null;
    let best = null;
    let bestArea = 0;
    for (const img of imgs) {
      if (img.dataset.frankTranslated === 'true') continue;
      if (!img.src?.startsWith('blob:')) continue;
      // Kindle parks the pages either side of the current one in the DOM, laid
      // out inside the viewport but hidden from the reader. They are the same
      // size and in the same place, so overlap alone cannot tell them apart —
      // and capturing one produces a page nobody is looking at.
      if (!isActuallyVisible(img)) continue;
      const rect = img.getBoundingClientRect();
      if (rect.width < MIN_PAGE_SIDE_PX || rect.height < MIN_PAGE_SIDE_PX) continue;
      let overlap = overlapAreaInViewport(rect, vw, vh);
      if (overlap < MIN_VISIBLE_OVERLAP_PX2) continue;
      if (rootRect && root !== document.body) {
        const rootOverlap = overlapArea(rect, rootRect);
        if (rootOverlap < MIN_VISIBLE_OVERLAP_PX2) continue;
        overlap = Math.min(overlap, rootOverlap);
      }
      // The page the reader can actually point at beats one merely laid out
      // under the same coordinates.
      const score = (topLayerHits(img) * 1e9) + overlap;
      if (score > bestArea) {
        bestArea = score;
        best = img;
      }
    }
    return best;
  }

  /// Whether the reader can actually see this element.
  function isActuallyVisible(el) {
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
    const opacity = Number.parseFloat(style.opacity || '1');
    return !Number.isFinite(opacity) || opacity > 0.05;
  }

  /// How many probe points land on this element rather than something above it.
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
      const top = document.elementFromPoint?.(x, y);
      if (top && (top === el || el.contains?.(top) || top.contains?.(el))) hits += 1;
    }
    return hits;
  }

  function findVisibleKindleImage() {
    const root = findReaderRoot();
    let imgs = Array.from(root.querySelectorAll('img'));
    if (!imgs.length) imgs = Array.from(document.querySelectorAll('img'));
    let best = null;
    let bestArea = 0;
    for (const img of imgs) {
      if (!img.src?.startsWith('blob:') && img.dataset.frankTranslated !== 'true') continue;
      const rect = img.getBoundingClientRect();
      if (rect.width < MIN_PAGE_SIDE_PX || rect.height < MIN_PAGE_SIDE_PX) continue;
      const overlap = overlapAreaInViewport(rect, window.innerWidth, window.innerHeight);
      if (overlap > bestArea) {
        bestArea = overlap;
        best = img;
      }
    }
    return best;
  }

  function captureForTarget(target, pageId) {
    const rect = target.getBoundingClientRect();
    return {
      pageId,
      index: pageCounter || 0,
      pageMode: spreadMode(rect),
      navIntent,
      imgSrc: target.dataset.frankOriginalSrc || target.src,
      naturalWidth: target.naturalWidth,
      naturalHeight: target.naturalHeight,
      rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      devicePixelRatio: window.devicePixelRatio || 1,
      kindlePage: findKindlePage(),
    };
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
    return debugEntries.get(img.dataset.frankPageId)
      || debugEntries.get(img.dataset.frankOriginalSrc)
      || debugEntries.get(img.src)
      || null;
  }

  async function dataUrlFromSrc(src) {
    if (!src) return null;
    if (src.startsWith('data:image/')) return src;
    const response = await fetch(src);
    if (!response.ok) return null;
    return blobToDataUrl(await response.blob());
  }

  async function splitSpreadDataUrl(dataUrl) {
    const img = await loadImage(dataUrl);
    const halfW = Math.floor(img.naturalWidth / 2);
    if (halfW <= 0 || img.naturalHeight <= 0) return null;
    return {
      left: cropLoadedImage(img, 0, 0, halfW, img.naturalHeight),
      right: cropLoadedImage(img, halfW, 0, img.naturalWidth - halfW, img.naturalHeight),
    };
  }

  function cropLoadedImage(img, sx, sy, sw, sh) {
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, sw);
    canvas.height = Math.max(1, sh);
    const ctx = canvas.getContext('2d');
    if (!ctx) return null;
    ctx.drawImage(img, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL('image/png');
  }

  function blobToDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(reader.error || new Error('failed to read image'));
      reader.onload = () => resolve(String(reader.result));
      reader.readAsDataURL(blob);
    });
  }

  function findImageBySrc(src) {
    if (!src) return null;
    return Array.from(document.querySelectorAll('img')).find((img) => img.src === src) || null;
  }

  function findReaderRoot() {
    return document.querySelector(
      '#kr-renderer, #kindle-reader-content, .reader-content, ' +
      '[id*="kindle-reader"], [id*="kr-renderer"], [class*="reader-content"]',
    ) || document.body;
  }

  /// Whether the reader is still painting, with a limit on how long we will
  /// believe it.
  function loaderVisible() {
    if (!loaderPresent()) {
      loaderSince = 0;
      return false;
    }
    const now = Date.now();
    if (!loaderSince) loaderSince = now;
    if (now - loaderSince < LOADER_PATIENCE_MS) return true;
    if (!loaderOverriddenAt) {
      loaderOverriddenAt = now;
      report('info', 'A loader has been on screen for 5s; reading the page anyway');
    }
    return false;
  }

  function loaderPresent() {
    const nodes = document.querySelectorAll('.kg-loader-wrapper, .kg-loader-container, [class*="loader"]');
    for (const el of nodes) {
      const style = window.getComputedStyle(el);
      if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
      const opacity = Number.parseFloat(style.opacity || '1');
      if (Number.isFinite(opacity) && opacity <= 0.05) continue;
      const rect = el.getBoundingClientRect();
      const overlap = overlapAreaInViewport(rect, window.innerWidth, window.innerHeight);
      if (overlap > LOADER_VISIBLE_OVERLAP_PX2) return true;
    }
    return false;
  }

  function findKindlePage() {
    const indicator = document.querySelector(
      '#kr-page-indicator, .page-number, [class*="pageNum"], [class*="page-count"], ' +
      '[class*="location"], [data-cfi], .cfi-marker',
    );
    const text = indicator?.textContent?.trim().slice(0, 30);
    if (text) return text;
    const slider = document.querySelector('input[type="range"], [role="slider"]');
    return slider ? `pos:${slider.value || slider.getAttribute('aria-valuenow') || ''}` : '';
  }

  /// The volume being read, as Kindle's ASIN. Also the key a per-book
  /// pipeline choice is stored under.
  /// Whether a page image holds two facing pages.
  ///
  /// A wide image is two manga pages side by side — but a novel is typeset to
  /// the window, so on a landscape screen a single prose page is wide too.
  /// Splitting one in half cuts its columns down the middle, annotates each
  /// half as if it were a page, and stitches something that matches nothing.
  function spreadMode(rect) {
    if (effectivePipeline() === 'book_furigana') return 'single';
    return rect.width > rect.height * SPREAD_THRESHOLD ? 'spread' : 'single';
  }

  function bookId() {
    return /[/=](B[A-Z0-9]{9})/.exec(location.href)?.[1] || '';
  }

  /// The pipeline this volume should use: its own choice, else the default.
  /// A manga volume and a novel need different ones, and the reader moves
  /// between them without wanting to change a setting each time.
  function effectivePipeline() {
    const chosen = settings.bookPipelines?.[bookId()];
    return chosen || settings.mangaPipeline;
  }

  function parseKindleMetadata(capture, pageId) {
    const title = bookId() || 'kindle';
    const latestToken = capture.groupId || capture.pageId || pageId;
    return {
      title,
      chapter: '1',
      pageNumber: capture.kindlePage || String(capture.index || pageId),
      sourceUrl: location.href,
      sourceSite: 'kindle',
      latestGroup: `kindle:${title}:${sessionId}`,
      latestToken,
      latestSeq: capture.index,
    };
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

  function reportNoTarget() {
    // Kindle injects into every frame; the ones without a reader have no page
    // image by definition and their reports are pure noise.
    if (!frameHostsKindleReader()) return;
    const now = Date.now();
    if (now - lastNoTargetReportAt < NO_TARGET_REPORT_INTERVAL_MS) return;
    lastNoTargetReportAt = now;
    report('info', 'Kindle detector is running, but no visible blob page image was found yet');
  }

  function report(level, message) {
    chrome.runtime.sendMessage({ type: 'REPORT_EVENT', site: 'kindle', level, message }).catch(() => {});
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes)) return 'unknown size';
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KiB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  }
})();
