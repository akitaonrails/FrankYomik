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

  const state = {
    enabled: true,
    zoom: DEFAULT_ZOOM,
    activePage: '',   // '' means every registration may be peeked
    el: null,
    open: false,
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
    setActivePage,
    setZoom,
    setEnabled,
    clear,
    has,
    isOpen: () => state.open,
    registeredPages: () => Array.from(entries.keys()),
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
    entries.set(pageId, { el: target, url });
    while (entries.size > MAX_REGISTRATIONS) {
      release(entries.keys().next().value);
    }
    target.dataset.frankLensPageId = pageId;
    target.dataset.frankLensSrc = url;

    // Warm the decode so the first peek does not stutter.
    const warm = new Image();
    warm.src = url;
    return true;
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
  function setActivePage(pageId) {
    const id = pageId == null ? '' : String(pageId);
    state.activePage = id;
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

  function findTargetAt(x, y) {
    let best = null;
    let bestArea = Infinity;
    for (const [pageId, entry] of entries) {
      if (state.activePage && pageId !== state.activePage) continue;
      const el = entry.el;
      if (!isVisible(el)) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width < MIN_TARGET_SIDE_PX || rect.height < MIN_TARGET_SIDE_PX) continue;
      if (x < rect.left || x > rect.right || y < rect.top || y > rect.bottom) continue;
      const area = rect.width * rect.height;
      if (area < bestArea) {
        bestArea = area;
        best = el;
      }
    }
    return best;
  }

  function openLens(x, y, target, pointerType) {
    const url = target.dataset.frankLensSrc;
    if (!url) return;
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
  // scroll; only a press held past HOLD_MS without travel becomes a peek.

  function onPointerDown(event) {
    if (!state.enabled) return;
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    closeLens();
    const target = findTargetAt(event.clientX, event.clientY);
    if (!target) return;
    state.pointerId = event.pointerId;
    state.startX = event.clientX;
    state.startY = event.clientY;
    const { clientX, clientY, pointerType } = event;
    cancelHold();
    state.holdTimer = window.setTimeout(() => {
      state.holdTimer = null;
      const fresh = findTargetAt(clientX, clientY) || target;
      if (fresh?.dataset?.frankLensSrc) openLens(clientX, clientY, fresh, pointerType);
    }, HOLD_MS);
  }

  function onPointerMove(event) {
    if (state.pointerId !== null && event.pointerId !== state.pointerId) return;
    if (state.holdTimer) {
      if (Math.hypot(event.clientX - state.startX, event.clientY - state.startY) > MOVE_CANCEL_PX) cancelHold();
      return;
    }
    if (!state.open) return;
    if (event.cancelable) event.preventDefault();
    updateLens(event.clientX, event.clientY);
  }

  function onPointerUp(event) {
    if (state.pointerId !== null && event.pointerId !== state.pointerId) return;
    state.pointerId = null;
    const wasOpen = state.open;
    closeLens();
    if (!wasOpen) return;
    // The press was a peek, not a page turn: swallow what it would spawn.
    state.suppressClick = true;
    if (event.cancelable) event.preventDefault();
    event.stopPropagation();
    if (state.suppressTimer) window.clearTimeout(state.suppressTimer);
    state.suppressTimer = window.setTimeout(() => {
      state.suppressClick = false;
      state.suppressTimer = null;
    }, CLICK_SUPPRESS_MS);
  }

  // Touch also generates compatibility mouse events after pointerup, and Kindle
  // turns pages on those as readily as on click.
  function onSyntheticMouse(event) {
    if (!state.suppressClick) return;
    if (event.type === 'click') state.suppressClick = false;
    if (event.cancelable) event.preventDefault();
    event.stopPropagation();
  }

  function onTouchMove(event) {
    if (state.open && event.cancelable) event.preventDefault();
  }

  function onContextMenu(event) {
    if (state.open || state.holdTimer) event.preventDefault();
  }

  const style = document.createElement('style');
  style.textContent =
    '.__frank-lens-open, .__frank-lens-open * {' +
    '-webkit-user-select:none !important;user-select:none !important;' +
    '-webkit-touch-callout:none !important;}';
  document.head.appendChild(style);

  document.addEventListener('pointerdown', onPointerDown, { capture: true });
  document.addEventListener('pointermove', onPointerMove, { capture: true, passive: false });
  document.addEventListener('pointerup', onPointerUp, { capture: true });
  document.addEventListener('pointercancel', onPointerUp, { capture: true });
  document.addEventListener('mousedown', onSyntheticMouse, { capture: true });
  document.addEventListener('mouseup', onSyntheticMouse, { capture: true });
  document.addEventListener('click', onSyntheticMouse, { capture: true });
  document.addEventListener('touchmove', onTouchMove, { capture: true, passive: false });
  document.addEventListener('contextmenu', onContextMenu, { capture: true });
  window.addEventListener('scroll', () => { cancelHold(); closeLens(); }, { capture: true, passive: true });
  window.addEventListener('resize', closeLens);
})();
