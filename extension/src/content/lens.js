(function frankLensModule() {
  'use strict';

  if (window.FrankLens) return;

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
  };

  window.FrankLens = {
    attach,
    release,
    markPending,
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

  window.addEventListener('pagehide', clear);

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
    entries.set(pageId, { el: target, url });
    while (entries.size > MAX_REGISTRATIONS) {
      release(entries.keys().next().value);
    }
    target.dataset.frankLensPageId = pageId;
    target.dataset.frankLensSrc = url;

    // Warm the decode so the first peek does not stutter.
    const warm = new Image();
    warm.src = url;

    // The reader may already be holding on this page, waiting for it.
    if (state.holding && !state.open && state.pendingEl === target) {
      openLens(state.lastX, state.lastY, target, state.pointerType);
    }
    return true;
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

  function has(pageId) {
    return entries.has(String(pageId));
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
      'background-color:#fff',
      'box-shadow:0 6px 24px rgba(0,0,0,0.45), 0 0 0 3px rgba(255,255,255,0.9), 0 0 0 5px rgba(0,0,0,0.35)',
      'will-change:left,top,background-position',
    ].join(';');
    document.body.appendChild(el);
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

  /// The page under the pointer, and whether its translation is ready.
  function candidateAt(x, y) {
    const registered = [];
    for (const [pageId, entry] of entries) {
      if (state.activePage && pageId !== state.activePage) continue;
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
    try {
      target.dispatchEvent(new PointerEvent('pointercancel', {
        bubbles: true,
        cancelable: false,
        pointerId: state.pointerId ?? 1,
        pointerType: pointerType || 'mouse',
        clientX: x,
        clientY: y,
      }));
    } catch {
      // Synthetic pointer events are a courtesy; never break the peek over one.
    }
  }

  function clearSelection() {
    const selection = window.getSelection?.();
    if (selection && !selection.isCollapsed) selection.removeAllRanges();
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
    el.style.backgroundSize = `${rect.width * zoom}px ${rect.height * zoom}px`;
    el.style.backgroundPosition = `${radius - (x - rect.left) * zoom}px ${radius - (y - rect.top) * zoom}px`;

    const lift = state.pointerType === 'touch' ? radius + TOUCH_LIFT_PX : 0;
    const minX = radius + LENS_EDGE_MARGIN_PX;
    const minY = radius + LENS_EDGE_MARGIN_PX;
    const cx = Math.max(minX, Math.min(window.innerWidth - minX, x));
    const cy = Math.max(minY, Math.min(window.innerHeight - minY, y - lift));
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
    // the page out from under a reader who is waiting for this very page.
    state.pendingEl = candidate.el;
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
    const { clientX, clientY, pointerType } = event;
    cancelHold();
    state.holdTimer = window.setTimeout(() => {
      state.holdTimer = null;
      const fresh = candidateAt(clientX, clientY) || candidate;
      beginHold(clientX, clientY, fresh, pointerType);
    }, HOLD_MS);
  }

  function onPointerMove(event) {
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
  }

  function onPointerUp(event) {
    if (state.pointerId !== null && event.pointerId !== state.pointerId) return;
    state.pointerId = null;
    const wasHolding = state.holding;
    endHold();
    if (!wasHolding) return;
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

  const style = document.createElement('style');
  style.textContent =
    '.__frank-lens-open, .__frank-lens-open * {' +
    '-webkit-user-select:none !important;user-select:none !important;' +
    '-webkit-touch-callout:none !important;}';
  document.head.appendChild(style);

  window.addEventListener('pointerdown', onPointerDown, { capture: true });
  window.addEventListener('pointermove', onPointerMove, { capture: true, passive: false });
  window.addEventListener('pointerup', onPointerUp, { capture: true });
  window.addEventListener('pointercancel', onPointerUp, { capture: true });
  window.addEventListener('mousedown', onSyntheticMouse, { capture: true });
  window.addEventListener('mouseup', onSyntheticMouse, { capture: true });
  window.addEventListener('click', onSyntheticMouse, { capture: true });
  window.addEventListener('mousemove', onMouseMove, { capture: true, passive: false });
  window.addEventListener('touchmove', onTouchMove, { capture: true, passive: false });
  window.addEventListener('dragstart', onDragOrSelect, { capture: true });
  window.addEventListener('selectstart', onDragOrSelect, { capture: true });
  window.addEventListener('contextmenu', onContextMenu, { capture: true });
  window.addEventListener('scroll', () => { cancelHold(); closeLens(); }, { capture: true, passive: true });
  window.addEventListener('resize', closeLens);
})();
