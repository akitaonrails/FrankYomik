(function frankLensModule() {
  'use strict';

  /// Whether the extension this code belongs to is still installed.
  ///
  /// Reloading or reinstalling an extension leaves its content scripts running
  /// in open tabs with a dead runtime. Chrome injects the new copy, but the old
  /// one still owns the page's listeners, so it has to stand down.
  function runtimeAlive() {
    try {
      return Boolean(chrome.runtime?.id);
    } catch {
      return false;
    }
  }

  if (window.FrankLens) {
    if (window.FrankLens.alive?.()) return;
    window.FrankLens.destroy?.();
  }

  const HOLD_MS = 200;              // press duration that separates a peek from a tap
  const MOVE_CANCEL_PX = 12;        // travel that reclassifies a press as a scroll
  const LENS_MIN_D = 180;
  const LENS_MAX_D = 360;
  const LENS_VIEWPORT_RATIO = 0.42;
  const TOUCH_LIFT_PX = 28;         // keep the lens clear of the fingertip
  const CLICK_SUPPRESS_MS = 400;
  const LENS_EDGE_MARGIN_PX = 4;
  const DEFAULT_ZOOM = 2;
  const MIN_TARGET_SIDE_PX = 40;
  // Webtoon keeps many pages on screen, so nothing calls setActivePage there;
  // retention is bounded by count instead.
  const MAX_REGISTRATIONS = 8;
  // A render and the page it belongs to have the same shape. Beyond this the
  // registration belongs to something else the reader has since replaced.
  const ASPECT_TOLERANCE = 0.08;
  // A render is its page plus annotations, so the two look alike at a glance.
  // Measured on real pages: a page against its own render differs by 0.05, and
  // against a different page by 0.90. Half way between leaves room for a
  // re-captured page (0.33) without admitting another one.
  const SIGNATURE_SIZE = 16;
  const SIGNATURE_TOLERANCE = 0.5;
  const FLAT_IMAGE_DEVIATION = 0.02;
  const INVERTED_CORRELATION = -0.3;
  // A render arrives while the reader may be mid-repaint, and comparing then
  // measures the page against a frame it is halfway through drawing. Uploading
  // both a moment later showed them matching at 0.096, so the answer is to
  // look again rather than to refuse on the first glance.
  const MATCH_RETRY_MS = [250, 750, 1500];

  // pageId -> { el, url }. Registrations own their object URL and revoke it,
  // so a long reading session cannot accumulate translated pages.
  const entries = new Map();

  // Elements the reader knows are pages but that have no translation yet. A
  // hold on one is still our gesture: it shows nothing, but it must not fall
  // through and turn the page while the reader waits for the render.
  const pending = new Set();

  const state = {
    enabled: true,
    zoom: DEFAULT_ZOOM,
    activePage: '',   // '' means every registration may be peeked
    el: null,
    open: false,      // the magnifier is on screen
    holding: false,   // the press became a peek; the reader is locked
    pendingEl: null,
    holdTimer: null,
    pointerId: null,
    pointerType: '',
    startX: 0,
    startY: 0,
    lastX: 0,
    lastY: 0,
    target: null,
    suppressClick: false,
    suppressTimer: null,
    // Set while we dispatch events of our own. Capture phase starts at the
    // window, so our own listeners see them before the reader does.
    cancelling: false,
    // Whether this press was taken from the reader before it saw it.
    pressCaptured: false,
    // Opt-in: only the Kindle strategy asks for this, and only mice get it.
    pressCapture: false,
  };

  let onMismatch = null;

  const api = {
    alive: runtimeAlive,
    destroy,
    /// Called when a render turns out not to depict the page it was bound to,
    /// so a strategy can decide what that means for the book being read.
    onRenderMismatch(handler) { onMismatch = handler; },
    compare,
    /// Test seam: the same call the render check makes when it discards one.
    __mismatch(detail = {}) { onMismatch?.({ pageId: 'test', ...detail }); },
    attach,
    release,
    markPending,
    setPressCapture,
    setActivePage,
    setZoom,
    setEnabled,
    clear,
    has,
    isOpen: () => state.open,
    registeredPages: () => Array.from(entries.keys()),
    /// Why a hold did or did not open the lens. Read from the extension's
    /// console context, not the page's.
    state: () => ({
      enabled: state.enabled,
      zoom: state.zoom,
      activePage: state.activePage,
      registered: Array.from(entries.keys()),
      awaitingTranslation: pending.size,
      holding: state.holding,
      open: state.open,
    }),
  };

  window.FrankLens = api;

  // Every listener this module installs, so it can hand the page back intact.
  const installed = [];
  function listen(target, type, handler, options) {
    target.addEventListener(type, handler, options);
    installed.push({ target, type, handler, options });
  }

  function destroy() {
    clear();
    for (const { target, type, handler, options } of installed.splice(0)) {
      target.removeEventListener(type, handler, options);
    }
    state.el?.remove?.();
    state.el = null;
    style?.remove?.();
    if (window.FrankLens === api) delete window.FrankLens;
  }

  listen(window, 'pagehide', clear);

  /* ---------- registration ---------- */

  // The page image keeps showing the original; the translation only ever
  // appears inside the magnifier.
  async function attach(target, pageId, imageDataUrl) {
    if (!target || !pageId || !imageDataUrl) return false;
    const existing = entries.get(pageId);
    if (existing && existing.el === target) return true;

    let url;
    try {
      const response = await fetch(imageDataUrl);
      url = URL.createObjectURL(await response.blob());
    } catch {
      return false;
    }

    release(pageId);
    pending.delete(target);
    // Unverified until the render is shown to depict this page: a peek before
    // then shows the waiting ring rather than a render that may be refused.
    const entry = { el: target, url, aspect: null, verified: false };
    entries.set(pageId, entry);
    while (entries.size > MAX_REGISTRATIONS) {
      release(entries.keys().next().value);
    }
    target.dataset.frankLensPageId = pageId;
    target.dataset.frankLensSrc = url;

    // Warm the decode so the first peek does not stutter, and record the
    // render's shape while we are at it.
    const warm = new Image();
    warm.addEventListener('load', async () => {
      if (warm.naturalHeight > 0) entry.aspect = warm.naturalWidth / warm.naturalHeight;
      const match = await depictsSettled(warm, target);
      if (match.ok) {
        entry.verified = true;
        return;
      }
      // The render is of some other page. Showing it would be worse than
      // showing nothing: it reads as a translation of what is on screen.
      const rect = target.getBoundingClientRect();
      onMismatch?.({
        pageId,
        difference: match.difference,
        // The data URL, not the object URL: release() revokes the latter.
        renderUrl: imageDataUrl,
        natural: `${target.naturalWidth || '?'}x${target.naturalHeight || '?'}`,
      });
      release(pageId);
      // An inverted render means the page's own text was redrawn — a manga
      // pipeline clearing balloons on a page that has none. Say so, because
      // the fix is a setting rather than anything the reader can retry.
      const diagnosis = match.inverted
        ? 'This page looks like a text book run through a manga pipeline. '
          + 'Set the pipeline for this book to "Furigana — text book".'
        : 'It is a render of a different page.';
      report(
        `Discarded a render that does not match its page (${pageId}). ${diagnosis} `
        + `[difference ${match.difference.toFixed(2)} of max ${SIGNATURE_TOLERANCE}, `
        + `render ${warm.naturalWidth}x${warm.naturalHeight}, `
        + `page ${Math.round(rect.width)}x${Math.round(rect.height)} `
        + `natural ${target.naturalWidth || '?'}x${target.naturalHeight || '?'}]`,
      );
    });
    warm.src = url;

    // The reader may already be holding on this page, waiting for it.
    if (state.holding && !state.open && state.pendingEl === target) {
      openLens(state.lastX, state.lastY, target, state.pointerType);
    }
    return true;
  }

  /// Compare two images by URL, using the same measure the binding check uses.
  ///
  /// Lets a caller ask the question that separates the two causes of a
  /// mismatch: does the render match what was actually sent for translation?
  function compare(urlA, urlB) {
    const load = (url) => new Promise((resolve) => {
      const image = new Image();
      image.addEventListener('load', () => resolve(image));
      image.addEventListener('error', () => resolve(null));
      image.src = url;
    });
    return Promise.all([load(urlA), load(urlB)]).then(([a, b]) => {
      if (!a || !b) return null;
      const result = depicts(a, b);
      return typeof result.difference === 'number' ? result.difference : null;
    });
  }

  /// A coarse, brightness-independent fingerprint of what something looks like.
  ///
  /// Returns null when the pixels cannot be read — a tainted canvas, an image
  /// that has not decoded — in which case the caller trusts the binding rather
  /// than dropping a good render.
  function signature(source) {
    try {
      const canvas = document.createElement('canvas');
      canvas.width = SIGNATURE_SIZE;
      canvas.height = SIGNATURE_SIZE;
      const context = canvas.getContext('2d', { willReadFrequently: true });
      if (!context) return null;
      context.drawImage(source, 0, 0, SIGNATURE_SIZE, SIGNATURE_SIZE);
      const { data } = context.getImageData(0, 0, SIGNATURE_SIZE, SIGNATURE_SIZE);
      const luma = [];
      for (let i = 0; i < data.length; i += 4) {
        luma.push((0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2]) / 255);
      }
      const mean = luma.reduce((a, b) => a + b, 0) / luma.length;
      const variance = luma.reduce((a, b) => a + (b - mean) ** 2, 0) / luma.length;
      const deviation = Math.sqrt(variance);
      // A flat image carries no structure to compare — an undecoded element,
      // a blank canvas. Treat it as unreadable rather than as "different".
      if (deviation < FLAT_IMAGE_DEVIATION) return null;
      return luma.map((value) => (value - mean) / deviation);
    } catch {
      return null;   // reading the pixels is a courtesy, not a requirement
    }
  }

  /// Whether a render depicts its page, allowing the page time to settle.
  ///
  /// A single look can catch the reader mid-repaint; a page that is really a
  /// different one stays different however long you wait.
  async function depictsSettled(render, target) {
    let match = depicts(render, target);
    for (const delay of MATCH_RETRY_MS) {
      if (match.ok) return match;
      await new Promise((resolve) => window.setTimeout(resolve, delay));
      if (!target.isConnected) return match;
      match = depicts(render, target);
    }
    return match;
  }

  /// Whether a render depicts the page it is about to be bound to.
  ///
  /// Every other link in the chain — blob URL, page id, element identity — has
  /// at some point pointed at the wrong page. This compares the two directly.
  function depicts(render, target) {
    const a = signature(render);
    const b = signature(target);
    if (!a || !b) return { ok: true, reason: 'unreadable' };
    let total = 0;
    let correlation = 0;
    for (let i = 0; i < a.length; i++) {
      total += Math.abs(a[i] - b[i]);
      correlation += a[i] * b[i];
    }
    const difference = total / a.length;
    // Both signatures are zero-mean and unit-variance, so this is their
    // correlation: strongly negative means the render is the page inverted,
    // which is what redrawing dark text as light boxes looks like.
    const inverted = (correlation / a.length) < INVERTED_CORRELATION;
    return { ok: difference <= SIGNATURE_TOLERANCE, difference, inverted };
  }

  /// Note an element as a page whose translation has not arrived yet.
  function markPending(el) {
    if (!el) return;
    pending.add(el);
    while (pending.size > MAX_REGISTRATIONS) {
      pending.delete(pending.values().next().value);
    }
  }

  function release(pageId) {
    const entry = entries.get(pageId);
    if (!entry) return;
    if (state.open && state.target === entry.el) closeLens();
    try {
      URL.revokeObjectURL(entry.url);
    } catch {
      // already revoked
    }
    if (entry.el?.dataset?.frankLensPageId === pageId) {
      delete entry.el.dataset.frankLensPageId;
      delete entry.el.dataset.frankLensSrc;
    }
    entries.delete(pageId);
  }

  function clear() {
    closeLens();
    for (const pageId of Array.from(entries.keys())) release(pageId);
  }

  /// Whether a page has a render that has been checked against it.
  function has(pageId) {
    return entries.get(String(pageId))?.verified === true;
  }

  // Kindle shows one page at a time and reuses the same <img> across turns, so
  // every other registration is both dead weight and a chance to magnify the
  // page the reader already left.
  function setActivePage(pageId, element) {
    const id = pageId == null ? '' : String(pageId);
    state.activePage = id;
    pending.clear();
    if (element) markPending(element);
    if (!id) return;
    for (const registered of Array.from(entries.keys())) {
      if (registered !== id) release(registered);
    }
    if (state.open && state.target && state.target.dataset.frankLensPageId !== id) closeLens();
  }

  /// Take the press itself, not just what follows it.
  ///
  /// Kindle starts its long-press selection from the pointerdown, so nothing
  /// swallowed afterwards can stop the highlight menu — the press has to not
  /// reach it at all. A tap is then handed back as a synthetic click so pages
  /// still turn. Mouse only: a touch press is also how the reader scrolls and
  /// swipes, and those cannot be handed back.
  function setPressCapture(enabled) {
    state.pressCapture = !!enabled;
  }

  /// Give the reader back the tap we took.
  function replayTap(x, y, target) {
    const element = document.elementFromPoint?.(x, y) || target;
    if (!element?.dispatchEvent || typeof MouseEvent !== 'function') return;
    state.cancelling = true;
    try {
      for (const type of ['mousedown', 'mouseup', 'click']) {
        const event = new MouseEvent(type, {
          bubbles: true,
          cancelable: true,
          view: window,
          detail: 1,
          button: 0,
          clientX: x,
          clientY: y,
        });
        event.frankSynthetic = true;
        element.dispatchEvent(event);
      }
    } catch {
      // Replaying a tap is best effort; never break the peek over it.
    } finally {
      state.cancelling = false;
    }
  }

  function setZoom(zoom) {
    const value = Number(zoom);
    if (!Number.isFinite(value) || value <= 0) return;
    state.zoom = value;
    if (state.open) updateLens(state.lastX, state.lastY);
  }

  function setEnabled(enabled) {
    state.enabled = !!enabled;
    if (!state.enabled) closeLens();
  }

  /* ---------- lens element ---------- */

  function lensDiameter() {
    const base = Math.min(window.innerWidth, window.innerHeight) * LENS_VIEWPORT_RATIO;
    return Math.round(Math.max(LENS_MIN_D, Math.min(LENS_MAX_D, base)));
  }

  function ensureLensEl() {
    if (state.el?.isConnected) return state.el;
    const el = document.createElement('div');
    el.id = '__frankLens';
    el.style.cssText = [
      'position:fixed',
      'z-index:2147483646',
      'display:none',
      'pointer-events:none',
      'border-radius:50%',
      'background-repeat:no-repeat',
      'background-color:transparent',
      'box-shadow:0 6px 24px rgba(0,0,0,0.45), 0 0 0 3px rgba(255,255,255,0.9), 0 0 0 5px rgba(0,0,0,0.35)',
      'will-change:left,top,background-position',
    ].join(';');
    (document.body || document.documentElement).appendChild(el);
    state.el = el;
    return el;
  }

  function isVisible(el) {
    if (!el?.isConnected) return false;
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
    const opacity = Number.parseFloat(style.opacity || '1');
    return !Number.isFinite(opacity) || opacity > 0.05;
  }

  function covers(el, x, y) {
    if (!isVisible(el)) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < MIN_TARGET_SIDE_PX || rect.height < MIN_TARGET_SIDE_PX) return false;
    return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
  }

  function smallestCovering(elements, x, y) {
    let best = null;
    let bestArea = Infinity;
    for (const el of elements) {
      if (!covers(el, x, y)) continue;
      const rect = el.getBoundingClientRect();
      const area = rect.width * rect.height;
      if (area < bestArea) {
        bestArea = area;
        best = el;
      }
    }
    return best;
  }

  /// Whether a registration still describes the page it is bound to.
  function stillMatches(entry) {
    if (!entry.aspect || !entry.el) return true;   // unknown shape: trust it
    const rect = entry.el.getBoundingClientRect();
    if (rect.height <= 0) return true;
    return Math.abs((rect.width / rect.height) - entry.aspect) / entry.aspect
      <= ASPECT_TOLERANCE;
  }

  /// The page under the pointer, and whether its translation is ready.
  function candidateAt(x, y) {
    const registered = [];
    for (const [pageId, entry] of entries) {
      if (state.activePage && pageId !== state.activePage) continue;
      if (!stillMatches(entry)) {
        // The reader put a different page in this element.
        release(pageId);
        continue;
      }
      registered.push(entry.el);
    }
    const ready = smallestCovering(registered, x, y);
    if (ready) return { el: ready, ready: true };
    const waiting = smallestCovering(pending, x, y);
    return waiting ? { el: waiting, ready: false } : null;
  }

  /// Tell the reader its gesture is void.
  ///
  /// The press that becomes a peek is deliberately let through, so Kindle has
  /// already started its own long-press selection by the time the lens opens.
  /// Swallowing the release stops our click but not its selection, which is
  /// what pops the highlight/copy/note menu. A pointercancel is exactly the
  /// signal for "this gesture is not happening", and the selection is dropped
  /// alongside it for readers that track it through the DOM.
  function abortReaderGesture(target, pointerType, x, y) {
    clearSelection();
    if (typeof PointerEvent !== 'function' || !target?.dispatchEvent) return;
    const cancel = new PointerEvent('pointercancel', {
      bubbles: true,
      cancelable: false,
      pointerId: state.pointerId ?? 1,
      pointerType: pointerType || 'mouse',
      clientX: x,
      clientY: y,
    });
    cancel.frankSynthetic = true;
    state.cancelling = true;
    try {
      target.dispatchEvent(cancel);
    } catch {
      // Synthetic pointer events are a courtesy; never break the peek over one.
    } finally {
      state.cancelling = false;
    }
  }

  /// Our own cancel, coming back to us through the capture phase.
  function isOurs(event) {
    return state.cancelling || event?.frankSynthetic === true;
  }

  function report(message) {
    try {
      chrome.runtime.sendMessage({
        type: 'REPORT_EVENT', site: 'kindle', level: 'error', message,
      })?.catch?.(() => {});
    } catch {
      // The extension may have been reloaded; the console still has it.
    }
    console.warn(`[Frank] ${message}`);
  }

  function clearSelection() {
    const selection = window.getSelection?.();
    if (selection && !selection.isCollapsed) selection.removeAllRanges();
  }

  /// The lens with nothing in it yet: this page is still being translated.
  function openWaitingRing(x, y) {
    const el = ensureLensEl();
    const diameter = lensDiameter();
    el.style.width = `${diameter}px`;
    el.style.height = `${diameter}px`;
    el.style.backgroundImage = 'none';
    el.style.animation = 'frankLensWaiting 1.4s ease-in-out infinite';
    el.style.display = 'block';
    place(el, x, y, diameter / 2);
  }

  function openLens(x, y, target, pointerType) {
    const url = target.dataset.frankLensSrc;
    if (!url) return;
    abortReaderGesture(target, pointerType, x, y);
    const el = ensureLensEl();
    const diameter = lensDiameter();
    el.style.width = `${diameter}px`;
    el.style.height = `${diameter}px`;
    el.style.backgroundImage = `url("${url}")`;
    el.style.animation = 'none';
    el.style.display = 'block';
    state.open = true;
    state.target = target;
    state.pointerType = pointerType || '';
    document.documentElement.classList.add('__frank-lens-open');
    updateLens(x, y);
  }

  function updateLens(x, y) {
    if (!state.open || !state.target) return;
    state.lastX = x;
    state.lastY = y;
    const el = state.el;
    const rect = state.target.getBoundingClientRect();
    const radius = lensDiameter() / 2;
    const zoom = state.zoom;

    // The translated render is scaled to the original's on-screen box, so the
    // point under the pointer maps to the same point in the translation.
    const width = rect.width * zoom;
    const height = rect.height * zoom;
    el.style.backgroundSize = `${width}px ${height}px`;
    // Clamp to the render: near an edge the lens stops panning instead of
    // showing empty space beside the page.
    const diameter = radius * 2;
    const offsetX = Math.min(0, Math.max(radius - (x - rect.left) * zoom, diameter - width));
    const offsetY = Math.min(0, Math.max(radius - (y - rect.top) * zoom, diameter - height));
    el.style.backgroundPosition = `${offsetX}px ${offsetY}px`;

    place(el, x, y, radius);
  }

  function place(el, x, y, radius) {
    const lift = state.pointerType === 'touch' ? radius + TOUCH_LIFT_PX : 0;
    const min = radius + LENS_EDGE_MARGIN_PX;
    const cx = Math.max(min, Math.min(window.innerWidth - min, x));
    const cy = Math.max(min, Math.min(window.innerHeight - min, y - lift));
    el.style.left = `${cx - radius}px`;
    el.style.top = `${cy - radius}px`;
  }

  function closeLens() {
    cancelHold();
    if (!state.open) return;
    state.open = false;
    state.target = null;
    if (state.el) state.el.style.display = 'none';
    document.documentElement.classList.remove('__frank-lens-open');
  }

  function cancelHold() {
    if (!state.holdTimer) return;
    window.clearTimeout(state.holdTimer);
    state.holdTimer = null;
  }

  /* ---------- gestures ---------- */
  // A quick tap is left alone so Kindle still turns pages and webtoons still
  // scroll; only a press held past HOLD_MS without travel becomes a peek. Once
  // it does, the gesture is ours: the reader must not also pan, drag or turn.

  /// Take the event away from the page entirely.
  ///
  /// preventDefault only stops the browser's own default action — Kindle's
  /// drag handler is a listener like any other and would still see the move,
  /// which is what made the page slide around under an open lens.
  function swallow(event) {
    if (event.cancelable) event.preventDefault();
    event.stopPropagation();
    if (typeof event.stopImmediatePropagation === 'function') event.stopImmediatePropagation();
  }

  function beginHold(x, y, candidate, pointerType) {
    state.holding = true;
    state.pointerType = pointerType || '';
    state.lastX = x;
    state.lastY = y;
    if (candidate.ready) {
      state.pendingEl = null;
      openLens(x, y, candidate.el, pointerType);
      return;
    }
    // Nothing to show yet. Hold the gesture anyway so releasing does not turn
    // the page out from under a reader who is waiting for this very page, and
    // show an empty ring: holding and getting nothing back is indistinguishable
    // from the lens being broken.
    state.pendingEl = candidate.el;
    openWaitingRing(x, y);
  }

  function endHold() {
    const wasHolding = state.holding;
    state.holding = false;
    state.pendingEl = null;
    closeLens();
    // Anything the reader selected under the lens goes with it.
    if (wasHolding) clearSelection();
  }

  function onPointerDown(event) {
    if (!state.enabled) return;
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    endHold();
    const candidate = candidateAt(event.clientX, event.clientY);
    if (!candidate) return;
    state.pointerId = event.pointerId;
    state.startX = event.clientX;
    state.startY = event.clientY;
    state.lastX = event.clientX;
    state.lastY = event.clientY;
    // A mouse press over a page we can peek is ours from the start.
    state.pressCaptured = state.pressCapture && event.pointerType === 'mouse';
    if (state.pressCaptured) swallow(event);
    const { clientX, clientY, pointerType } = event;
    cancelHold();
    state.holdTimer = window.setTimeout(() => {
      state.holdTimer = null;
      const fresh = candidateAt(clientX, clientY) || candidate;
      beginHold(clientX, clientY, fresh, pointerType);
    }, HOLD_MS);
  }

  function onPointerMove(event) {
    if (isOurs(event)) return;
    if (state.pointerId !== null && event.pointerId !== state.pointerId) return;
    if (state.holdTimer) {
      if (Math.hypot(event.clientX - state.startX, event.clientY - state.startY) > MOVE_CANCEL_PX) cancelHold();
      return;
    }
    if (!state.holding) return;
    swallow(event);
    clearSelection();
    state.lastX = event.clientX;
    state.lastY = event.clientY;
    if (state.open) updateLens(event.clientX, event.clientY);
    else if (state.pendingEl) openWaitingRing(event.clientX, event.clientY);
  }

  function onPointerUp(event) {
    // A peek must survive the cancel it sends to the reader.
    if (isOurs(event)) return;
    if (state.pointerId !== null && event.pointerId !== state.pointerId) return;
    state.pointerId = null;
    const wasHolding = state.holding;
    const captured = state.pressCaptured;
    const target = state.target;
    state.pressCaptured = false;
    endHold();

    if (!wasHolding) {
      // A tap: the reader never saw the press, so hand it back.
      if (captured) {
        swallow(event);
        replayTap(event.clientX, event.clientY, target);
      }
      return;
    }
    // The press was a peek, not a page turn: swallow what it would spawn.
    state.suppressClick = true;
    swallow(event);
    if (state.suppressTimer) window.clearTimeout(state.suppressTimer);
    state.suppressTimer = window.setTimeout(() => {
      state.suppressClick = false;
      state.suppressTimer = null;
    }, CLICK_SUPPRESS_MS);
  }

  // Touch also generates compatibility mouse events after pointerup, and Kindle
  // turns pages on those as readily as on click.
  function onSyntheticMouse(event) {
    if (isOurs(event)) return;
    // The compatibility mousedown would restart the very gesture the captured
    // pointerdown was taken to prevent.
    if (state.pressCaptured) {
      swallow(event);
      return;
    }
    if (state.holding) {
      swallow(event);
      return;
    }
    if (!state.suppressClick) return;
    if (event.type === 'click') state.suppressClick = false;
    swallow(event);
  }

  // Mouse drags raise mousemove alongside pointermove, and Kindle pans on
  // either, so both have to be taken away.
  function onMouseMove(event) {
    if (state.holding) swallow(event);
  }

  function onTouchMove(event) {
    if (state.holding) swallow(event);
  }

  // Dragging an <img> is a native gesture of its own; without this the page
  // image gets picked up while peeking.
  function onDragOrSelect(event) {
    if (state.holding || state.holdTimer) swallow(event);
  }

  function onContextMenu(event) {
    if (state.holding || state.holdTimer) event.preventDefault();
  }

  function onScroll() {
    cancelHold();
    closeLens();
  }

  // Injected before the page's own scripts run, so there may be no head yet.
  const style = document.createElement('style');
  style.textContent =
    '.__frank-lens-open, .__frank-lens-open * {' +
    '-webkit-user-select:none !important;user-select:none !important;' +
    '-webkit-touch-callout:none !important;}' +
    '@keyframes frankLensWaiting{0%,100%{opacity:0.25}50%{opacity:0.6}}';
  (document.head || document.documentElement)?.appendChild(style);

  listen(window, 'pointerdown', onPointerDown, { capture: true });
  listen(window, 'pointermove', onPointerMove, { capture: true, passive: false });
  listen(window, 'pointerup', onPointerUp, { capture: true });
  listen(window, 'pointercancel', onPointerUp, { capture: true });
  listen(window, 'mousedown', onSyntheticMouse, { capture: true });
  listen(window, 'mouseup', onSyntheticMouse, { capture: true });
  listen(window, 'click', onSyntheticMouse, { capture: true });
  listen(window, 'mousemove', onMouseMove, { capture: true, passive: false });
  listen(window, 'touchmove', onTouchMove, { capture: true, passive: false });
  listen(window, 'dragstart', onDragOrSelect, { capture: true });
  listen(window, 'selectstart', onDragOrSelect, { capture: true });
  listen(window, 'contextmenu', onContextMenu, { capture: true });
  listen(window, 'scroll', onScroll, { capture: true, passive: true });
  listen(window, 'resize', closeLens);
})();
