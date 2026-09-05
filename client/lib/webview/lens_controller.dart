import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'platform/app_webview_controller.dart';

/// Magnifier-lens presentation of a translated page.
///
/// Unlike [OverlayController], which swaps the reader's `img.src` for the
/// translated render, the lens leaves the original page visible and keeps the
/// translated render off-screen. A long press reveals it through a circular
/// magnifier that tracks the pointer, so the page is read in its original
/// language and only the balloon under the finger is translated.
class LensController {
  /// Injected once per page load; guarded by `window.__frankLens` in JS.
  Future<void> inject(AppWebViewController controller) async {
    await controller.evaluateJavascript(source: _moduleScript);
  }

  /// Register [imageBytes] as the translated source for a page.
  ///
  /// The lens needs the same DOM element the full overlay would have replaced:
  /// its on-screen rect is what maps a pointer position to a point in the
  /// translated render. Returns false when no matching element is on screen,
  /// which is the caller's cue to retry while the reader settles.
  Future<bool> register(
    AppWebViewController controller, {
    required String pageId,
    required Uint8List imageBytes,
    String? expectedBlobSrc,
    Map<String, num>? expectedRect,
    String? originalSrc,
  }) async {
    final encodeSw = Stopwatch()..start();
    final base64Data = await compute(base64Encode, imageBytes);
    encodeSw.stop();
    final opts = jsonEncode({
      'pageId': pageId,
      'expectedBlobSrc': ?expectedBlobSrc,
      'expectedRect': ?expectedRect,
      'originalSrc': ?originalSrc,
    });
    final evalSw = Stopwatch()..start();
    final raw = await controller.evaluateJavascript(
      source:
          'window.__frankLens ? '
          "window.__frankLens.register('$base64Data', $opts) : "
          "JSON.stringify({ok:false, error:'module_missing'})",
    );
    evalSw.stop();
    final result = _decode(raw);
    final ok = result?['ok'] == true;
    debugPrint(
      '[LensPerf] page=$pageId bytes=${imageBytes.length} '
      'encodeMs=${encodeSw.elapsedMilliseconds} '
      'evalMs=${evalSw.elapsedMilliseconds} ok=$ok '
      'match=${result?['matchType'] ?? '-'}'
      "${result?['error'] != null ? ' error=${result!['error']}' : ''}",
    );
    return ok;
  }

  /// Set the magnification factor (1.5x, 2x, 3x from the in-page toolbar).
  Future<void> setZoom(AppWebViewController controller, double zoom) async {
    await controller.evaluateJavascript(
      source: 'if(window.__frankLens) window.__frankLens.setZoom($zoom);',
    );
  }

  /// Arm or disarm the long-press gesture without discarding registrations.
  Future<void> setEnabled(AppWebViewController controller, bool enabled) async {
    await controller.evaluateJavascript(
      source: 'if(window.__frankLens) window.__frankLens.setEnabled($enabled);',
    );
  }

  /// Note a page as awaiting its translation.
  ///
  /// A hold on such a page shows nothing, but is still absorbed: the reader
  /// should not turn the page out from under someone waiting for that very
  /// render. Kindle pages are marked by [setActivePage]; webtoon pages have no
  /// single active page, so they are marked by their original src at submit.
  Future<void> markPending(
    AppWebViewController controller, {
    String? originalSrc,
  }) async {
    final opts = originalSrc != null
        ? jsonEncode({'originalSrc': originalSrc})
        : '{}';
    await controller.evaluateJavascript(
      source: 'if(window.__frankLens) window.__frankLens.markPending($opts);',
    );
  }

  /// Restrict peeking to [pageId], releasing translations for other pages.
  ///
  /// Pass null on sites where several pages are on screen at once (webtoon),
  /// which leaves every registration peekable.
  Future<void> setActivePage(
    AppWebViewController controller,
    String? pageId,
  ) async {
    final arg = pageId == null ? 'null' : jsonEncode(pageId);
    await controller.evaluateJavascript(
      source: 'if(window.__frankLens) window.__frankLens.setActivePage($arg);',
    );
  }

  /// Drop every registered translation and revoke its blob URL.
  Future<void> clear(AppWebViewController controller) async {
    await controller.evaluateJavascript(
      source: 'if(window.__frankLens) window.__frankLens.clear();',
    );
  }

  /// True when the page currently under the reader has a lens source ready.
  Future<bool> hasSourceFor(
    AppWebViewController controller,
    String pageId,
  ) async {
    final raw = await controller.evaluateJavascript(
      source:
          'window.__frankLens ? '
          "window.__frankLens.has(${jsonEncode(pageId)}) : false",
    );
    return raw == true || raw == 'true' || raw == 1;
  }

  Map<String, dynamic>? _decode(dynamic raw) {
    if (raw is Map) return Map<String, dynamic>.from(raw);
    if (raw is! String || raw.isEmpty) return null;
    try {
      return Map<String, dynamic>.from(jsonDecode(raw) as Map);
    } catch (_) {
      return null;
    }
  }
}

/// The in-page lens module. Kept as a raw string so JS `$` and `{}` survive.
const String _moduleScript = r'''
(function() {
  if (window.__frankLens) return;

  var HOLD_MS = 200;            // press duration that separates peek from tap
  var MOVE_CANCEL_PX = 12;      // finger travel that reclassifies press as scroll
  var MIN_PAGE_SIDE_PX = 100;
  var MIN_VISIBLE_OVERLAP_PX2 = 2000;
  var LENS_MIN_D = 180;
  var LENS_MAX_D = 360;
  var TOUCH_LIFT_PX = 28;       // keep the lens clear of the fingertip
  // Webtoon keeps many pages on screen, so nothing calls setActivePage there;
  // retention is bounded by count instead.
  var MAX_REGISTRATIONS = 8;

  var state = {
    enabled: true,
    zoom: 2.0,
    activePage: '',     // '' means every registered page may be peeked
    sources: {},        // pageId -> blob url
    // Pages the reader knows about that have no translation yet. A hold on one
    // is still our gesture: it shows nothing, but it must not fall through and
    // turn the page while the reader waits for the render.
    pending: [],
    el: null,
    open: false,        // the magnifier is on screen
    holding: false,     // the press became a peek; the reader is locked
    pendingEl: null,
    holdTimer: null,
    pointerId: null,
    pointerType: '',
    startX: 0,
    startY: 0,
    lastX: 0,
    lastY: 0,
    target: null,
    suppressClick: false
  };

  window.addEventListener('pagehide', clearAll);

  /* ---------- target discovery ---------- */

  function findReaderRoot() {
    return document.querySelector(
      '#kr-renderer, #kindle-reader-content, .reader-content, ' +
      '[id*="kindle-reader"], [id*="kr-renderer"], [class*="reader-content"]'
    ) || document.body;
  }

  function isActuallyVisible(el) {
    if (!el) return false;
    var st = window.getComputedStyle(el);
    if (!st) return false;
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    var opacity = parseFloat(st.opacity || '1');
    return !isFinite(opacity) || opacity > 0.05;
  }

  function overlapAreaInViewport(r) {
    var ox = Math.min(r.right, window.innerWidth) - Math.max(r.left, 0);
    var oy = Math.min(r.bottom, window.innerHeight) - Math.max(r.top, 0);
    return (ox <= 0 || oy <= 0) ? 0 : ox * oy;
  }

  function overlapArea(a, b) {
    var ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    var oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    return (ox <= 0 || oy <= 0) ? 0 : ox * oy;
  }

  function rectBiasScore(r, expectedRect) {
    if (!expectedRect) return 0;
    var ew = Math.max(1, Number(expectedRect.width || 1));
    var eh = Math.max(1, Number(expectedRect.height || 1));
    var ecx = Number(expectedRect.x || 0) + (ew / 2);
    var ecy = Number(expectedRect.y || 0) + (eh / 2);
    var cx = r.left + (r.width / 2);
    var cy = r.top + (r.height / 2);
    var centerDist = Math.hypot(cx - ecx, cy - ecy);
    var sizeErr = (Math.abs(r.width - ew) / ew) + (Math.abs(r.height - eh) / eh);
    return -((centerDist * 800) + (sizeErr * 500000));
  }

  // Kindle: the visible blob-backed page image, preferring the exact blob the
  // capture came from so a page turn mid-job cannot bind the wrong page.
  function findKindleTarget(opts) {
    var expected = opts.expectedBlobSrc || '';
    var root = findReaderRoot();
    var imgs = root.querySelectorAll('img');
    if (!imgs || imgs.length === 0) imgs = document.querySelectorAll('img');
    var rootRect = root.getBoundingClientRect ? root.getBoundingClientRect() : null;
    var best = null;
    var bestScore = -Infinity;

    for (var i = 0; i < imgs.length; i++) {
      var img = imgs[i];
      if (!img.src || img.src.indexOf('blob:') !== 0) continue;
      var r = img.getBoundingClientRect();
      if (r.width < MIN_PAGE_SIDE_PX || r.height < MIN_PAGE_SIDE_PX) continue;
      var overlap = overlapAreaInViewport(r);
      if (overlap < MIN_VISIBLE_OVERLAP_PX2) continue;
      if (rootRect && root !== document.body) {
        var rootOverlap = overlapArea(r, rootRect);
        if (rootOverlap < MIN_VISIBLE_OVERLAP_PX2) continue;
        overlap = Math.min(overlap, rootOverlap);
      }
      if (!isActuallyVisible(img)) continue;
      if (expected && img.src !== expected) continue;
      var score = overlap + rectBiasScore(r, opts.expectedRect);
      if (score > bestScore) { bestScore = score; best = img; }
    }
    return best;
  }

  function findWebtoonTarget(opts) {
    var imgs = document.querySelectorAll('img');
    var i;
    if (opts.originalSrc) {
      for (i = 0; i < imgs.length; i++) {
        if (imgs[i].src === opts.originalSrc ||
            imgs[i].dataset.frankOriginalSrc === opts.originalSrc) return imgs[i];
      }
    }
    var pageId = String(opts.pageId || '');
    if (pageId.indexOf('wt-') === 0) {
      var wtIdx = pageId.replace('wt-', '');
      for (i = 0; i < imgs.length; i++) {
        if (imgs[i].dataset.frankIndex === wtIdx) return imgs[i];
      }
      var toonImgs = document.querySelectorAll('img.toon_image');
      var n = parseInt(wtIdx, 10);
      if (isFinite(n) && n >= 0 && n < toonImgs.length) return toonImgs[n];
    }
    return null;
  }

  /* ---------- registration ---------- */

  function register(base64Data, opts) {
    opts = opts || {};
    var pageId = String(opts.pageId || '');
    if (!pageId) return JSON.stringify({ ok: false, error: 'missing_page_id' });

    var isWebtoon = !!opts.originalSrc || pageId.indexOf('wt-') === 0;
    var target = isWebtoon ? findWebtoonTarget(opts) : findKindleTarget(opts);
    if (!target) return JSON.stringify({ ok: false, error: 'no_target' });

    var blobUrl;
    try {
      var binary = atob(base64Data);
      var bytes = new Uint8Array(binary.length);
      for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      blobUrl = URL.createObjectURL(new Blob([bytes], { type: 'image/png' }));
    } catch (e) {
      return JSON.stringify({ ok: false, error: 'decode_failed', detail: String(e) });
    }

    releaseSource(pageId);
    unmarkPending(target);
    state.sources[pageId] = blobUrl;
    var registered = Object.keys(state.sources);
    while (registered.length > MAX_REGISTRATIONS) {
      releaseSource(registered.shift());
    }
    target.dataset.frankLensSrc = blobUrl;
    target.dataset.frankLensPageId = pageId;
    if (!target.dataset.frankOriginalSrc && target.src) {
      target.dataset.frankOriginalSrc = target.src;
    }

    // Warm the decode so the first peek does not stutter.
    var warm = new Image();
    warm.src = blobUrl;

    // The reader may already be holding on this page, waiting for it.
    if (state.holding && !state.open && state.pendingEl === target) {
      openLens(state.lastX, state.lastY, target, state.pointerType);
    }

    var rect = target.getBoundingClientRect();
    return JSON.stringify({
      ok: true,
      matchType: opts.originalSrc ? 'webtoon-src' : 'kindle-blob',
      rect: {
        x: Math.round(rect.left), y: Math.round(rect.top),
        w: Math.round(rect.width), h: Math.round(rect.height)
      }
    });
  }

  function markPending(el) {
    if (!el) return;
    var idx = state.pending.indexOf(el);
    if (idx !== -1) state.pending.splice(idx, 1);
    state.pending.push(el);
    while (state.pending.length > MAX_REGISTRATIONS) state.pending.shift();
  }

  function unmarkPending(el) {
    var idx = state.pending.indexOf(el);
    if (idx !== -1) state.pending.splice(idx, 1);
  }

  function releaseSource(pageId) {
    var url = state.sources[pageId];
    if (!url) return;
    try { URL.revokeObjectURL(url); } catch (e) { /* already gone */ }
    delete state.sources[pageId];
    var stale = document.querySelectorAll('img[data-frank-lens-page-id="' + cssEscape(pageId) + '"]');
    for (var i = 0; i < stale.length; i++) {
      delete stale[i].dataset.frankLensSrc;
      delete stale[i].dataset.frankLensPageId;
    }
  }

  function cssEscape(value) {
    return String(value).replace(/["\\]/g, '\\$&');
  }

  function clearAll() {
    closeLens();
    for (var pageId in state.sources) {
      if (Object.prototype.hasOwnProperty.call(state.sources, pageId)) {
        try { URL.revokeObjectURL(state.sources[pageId]); } catch (e) { /* ignore */ }
      }
    }
    state.sources = {};
    var els = document.querySelectorAll('img[data-frank-lens-src]');
    for (var i = 0; i < els.length; i++) {
      delete els[i].dataset.frankLensSrc;
      delete els[i].dataset.frankLensPageId;
    }
  }

  /* ---------- lens element ---------- */

  function lensDiameter() {
    var base = Math.min(window.innerWidth, window.innerHeight) * 0.42;
    return Math.round(Math.max(LENS_MIN_D, Math.min(LENS_MAX_D, base)));
  }

  function ensureLensEl() {
    if (state.el && state.el.isConnected) return state.el;
    var el = document.createElement('div');
    el.id = '__frankLens';
    el.style.cssText =
      'position:fixed; z-index:2147483646; display:none; pointer-events:none;' +
      'border-radius:50%; background-repeat:no-repeat; background-color:#fff;' +
      'box-shadow:0 6px 24px rgba(0,0,0,0.45), 0 0 0 3px rgba(255,255,255,0.9),' +
      ' 0 0 0 5px rgba(0,0,0,0.35); will-change:left,top,background-position;';
    document.body.appendChild(el);
    state.el = el;
    return el;
  }

  function smallestCovering(els, x, y) {
    var best = null;
    var bestArea = Infinity;
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (!el) continue;
      var r = el.getBoundingClientRect();
      if (r.width < 40 || r.height < 40) continue;
      if (x < r.left || x > r.right || y < r.top || y > r.bottom) continue;
      if (!isActuallyVisible(el)) continue;
      var area = r.width * r.height;
      if (area < bestArea) { bestArea = area; best = el; }
    }
    return best;
  }

  // The page under the pointer, and whether its translation is ready.
  function candidateAt(x, y) {
    var els = document.querySelectorAll('img[data-frank-lens-src]');
    var registered = [];
    for (var i = 0; i < els.length; i++) {
      // Kindle reuses the same <img> across page turns; without this gate a
      // stale registration would magnify the page the reader already left.
      if (state.activePage && els[i].dataset.frankLensPageId !== state.activePage) continue;
      registered.push(els[i]);
    }
    var ready = smallestCovering(registered, x, y);
    if (ready) return { el: ready, ready: true };
    var waiting = smallestCovering(state.pending, x, y);
    return waiting ? { el: waiting, ready: false } : null;
  }

  // Tell the reader its gesture is void. The press that becomes a peek is
  // deliberately let through so taps still turn pages, which means the reader
  // has already begun its own long-press selection by the time the lens
  // opens; releasing then pops its highlight/copy/note menu. A pointercancel
  // is exactly the signal for "this gesture is not happening", and the
  // selection is dropped alongside it.
  function abortReaderGesture(target, pointerType, x, y) {
    clearSelection();
    if (typeof PointerEvent !== 'function' || !target || !target.dispatchEvent) return;
    try {
      target.dispatchEvent(new PointerEvent('pointercancel', {
        bubbles: true,
        cancelable: false,
        pointerId: state.pointerId === null ? 1 : state.pointerId,
        pointerType: pointerType || 'mouse',
        clientX: x,
        clientY: y
      }));
    } catch (e) {
      // Synthetic pointer events are a courtesy; never break the peek over one.
    }
  }

  function clearSelection() {
    if (!window.getSelection) return;
    var selection = window.getSelection();
    if (selection && !selection.isCollapsed) selection.removeAllRanges();
  }

  function openLens(x, y, target, pointerType) {
    var src = target.dataset.frankLensSrc;
    if (!src) return;
    abortReaderGesture(target, pointerType, x, y);
    var el = ensureLensEl();
    var d = lensDiameter();
    el.style.width = d + 'px';
    el.style.height = d + 'px';
    el.style.backgroundImage = 'url("' + src + '")';
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
    var el = state.el;
    var rect = state.target.getBoundingClientRect();
    var d = lensDiameter();
    var r = d / 2;
    var z = state.zoom;

    // The translated render is scaled to the original's on-screen box, so a
    // point in the page maps to the same point in the translation.
    el.style.backgroundSize = (rect.width * z) + 'px ' + (rect.height * z) + 'px';
    var px = (x - rect.left) * z;
    var py = (y - rect.top) * z;
    el.style.backgroundPosition = (r - px) + 'px ' + (r - py) + 'px';

    var lift = state.pointerType === 'touch' ? (r + TOUCH_LIFT_PX) : 0;
    var cx = Math.max(r + 4, Math.min(window.innerWidth - r - 4, x));
    var cy = Math.max(r + 4, Math.min(window.innerHeight - r - 4, y - lift));
    el.style.left = (cx - r) + 'px';
    el.style.top = (cy - r) + 'px';
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
    if (state.holdTimer) {
      clearTimeout(state.holdTimer);
      state.holdTimer = null;
    }
  }

  /* ---------- gestures ---------- */
  // A quick tap is left alone so the reader still turns pages; only a press
  // held past HOLD_MS without travel becomes a peek. Once it does, the gesture
  // is ours: the reader must not also pan, drag or turn.

  // Take the event away from the page entirely. preventDefault only stops the
  // browser's own default action — the reader's drag handler is a listener
  // like any other and would still see the move, which is what made the page
  // slide around under an open lens.
  function swallow(e) {
    if (e.cancelable) e.preventDefault();
    e.stopPropagation();
    if (typeof e.stopImmediatePropagation === 'function') e.stopImmediatePropagation();
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
    var wasHolding = state.holding;
    state.holding = false;
    state.pendingEl = null;
    closeLens();
    // Anything the reader selected under the lens goes with it.
    if (wasHolding) clearSelection();
  }

  function onPointerDown(e) {
    if (!state.enabled) return;
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    endHold();
    var candidate = candidateAt(e.clientX, e.clientY);
    if (!candidate) return;
    state.pointerId = e.pointerId;
    state.startX = e.clientX;
    state.startY = e.clientY;
    state.lastX = e.clientX;
    state.lastY = e.clientY;
    var x = e.clientX;
    var y = e.clientY;
    var type = e.pointerType;
    cancelHold();
    state.holdTimer = setTimeout(function() {
      state.holdTimer = null;
      beginHold(x, y, candidateAt(x, y) || candidate, type);
    }, HOLD_MS);
  }

  function onPointerMove(e) {
    if (state.pointerId !== null && e.pointerId !== state.pointerId) return;
    if (state.holdTimer) {
      var dx = e.clientX - state.startX;
      var dy = e.clientY - state.startY;
      if (Math.hypot(dx, dy) > MOVE_CANCEL_PX) cancelHold();
      return;
    }
    if (!state.holding) return;
    swallow(e);
    clearSelection();
    state.lastX = e.clientX;
    state.lastY = e.clientY;
    if (state.open) updateLens(e.clientX, e.clientY);
  }

  function onPointerUp(e) {
    if (state.pointerId !== null && e.pointerId !== state.pointerId) return;
    state.pointerId = null;
    var wasHolding = state.holding;
    endHold();
    if (!wasHolding) return;
    // The press was a peek, not a page turn: swallow the click it spawns.
    state.suppressClick = true;
    swallow(e);
    setTimeout(function() { state.suppressClick = false; }, 400);
  }

  // Touch also generates compatibility mouse events after pointerup, and the
  // reader turns pages on those as readily as on click.
  function onSyntheticMouseCapture(e) {
    if (state.holding) { swallow(e); return; }
    if (!state.suppressClick) return;
    if (e.type === 'click') state.suppressClick = false;
    swallow(e);
  }

  // Mouse drags raise mousemove alongside pointermove, and readers pan on
  // either, so both have to be taken away.
  function onMouseMove(e) {
    if (state.holding) swallow(e);
  }

  function onTouchMove(e) {
    if (state.holding) swallow(e);
  }

  // Dragging an <img> is a native gesture of its own; without this the page
  // image gets picked up while peeking.
  function onDragOrSelect(e) {
    if (state.holding || state.holdTimer) swallow(e);
  }

  function onContextMenu(e) {
    if (state.holding || state.holdTimer) e.preventDefault();
  }

  var style = document.createElement('style');
  style.textContent =
    '.__frank-lens-open, .__frank-lens-open * {' +
    '  -webkit-user-select:none !important; user-select:none !important;' +
    '  -webkit-touch-callout:none !important;' +
    '}';
  document.head.appendChild(style);

  window.addEventListener('pointerdown', onPointerDown, { capture: true });
  window.addEventListener('pointermove', onPointerMove, { capture: true, passive: false });
  window.addEventListener('pointerup', onPointerUp, { capture: true });
  window.addEventListener('pointercancel', onPointerUp, { capture: true });
  window.addEventListener('mousedown', onSyntheticMouseCapture, { capture: true });
  window.addEventListener('mouseup', onSyntheticMouseCapture, { capture: true });
  window.addEventListener('click', onSyntheticMouseCapture, { capture: true });
  window.addEventListener('mousemove', onMouseMove, { capture: true, passive: false });
  window.addEventListener('touchmove', onTouchMove, { capture: true, passive: false });
  window.addEventListener('dragstart', onDragOrSelect, { capture: true });
  window.addEventListener('selectstart', onDragOrSelect, { capture: true });
  window.addEventListener('contextmenu', onContextMenu, { capture: true });
  window.addEventListener('scroll', function() { cancelHold(); closeLens(); }, { capture: true, passive: true });
  window.addEventListener('resize', function() { closeLens(); });

  window.__frankLens = {
    register: register,
    clear: clearAll,
    markPending: function(opts) {
      var el = (opts && opts.originalSrc)
        ? findWebtoonTarget(opts)
        : findKindleTarget(opts || {});
      if (el) markPending(el);
      return !!el;
    },
    setActivePage: function(pageId) {
      var id = pageId == null ? '' : String(pageId);
      state.activePage = id;
      // The page now on screen has no translation yet; a hold on it should be
      // absorbed rather than turning the page.
      state.pending = [];
      var current = findKindleTarget({});
      if (current) markPending(current);
      if (!id) return;
      if (state.open && state.target &&
          state.target.dataset.frankLensPageId !== id) closeLens();
      // One page is on screen at a time, so anything else is dead weight.
      for (var key in state.sources) {
        if (Object.prototype.hasOwnProperty.call(state.sources, key) && key !== id) {
          releaseSource(key);
        }
      }
    },
    has: function(pageId) { return !!state.sources[String(pageId)]; },
    registeredPages: function() { return Object.keys(state.sources); },
    setZoom: function(z) {
      var n = Number(z);
      if (isFinite(n) && n > 0) state.zoom = n;
      if (state.open) updateLens(state.lastX, state.lastY);
    },
    setEnabled: function(on) {
      state.enabled = !!on;
      if (!state.enabled) closeLens();
    },
    isOpen: function() { return state.open; }
  };
})();
''';
