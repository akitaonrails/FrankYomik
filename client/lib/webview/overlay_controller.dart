import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'platform/app_webview_controller.dart';

/// Manages translated page overlay on the WebView.
class OverlayController {
  /// Replace an <img> element's src with translated image bytes,
  /// matching by original src URL for reliable identification.
  Future<bool> replaceImageBySrc(
    AppWebViewController controller,
    String originalSrc,
    Uint8List imageBytes,
    String? pageId,
  ) async {
    final encodeSw = Stopwatch()..start();
    final base64Data = await compute(base64Encode, imageBytes);
    encodeSw.stop();
    // jsonEncode properly escapes all special chars (quotes, newlines, backticks, etc.)
    final safeSrc = jsonEncode(originalSrc);
    final evalSw = Stopwatch()..start();
    final result = await controller.evaluateJavascript(
      source:
          '''
(function() {
  var targetSrc = $safeSrc;

  // Find the img by matching src
  var allImgs = document.querySelectorAll('img');
  var img = null;
  var matchType = '';
  for (var i = 0; i < allImgs.length; i++) {
    if (allImgs[i].src === targetSrc) {
      img = allImgs[i];
      matchType = 'current-src';
      break;
    }
  }
  // Fallback 1: match by data-frank-index (set during webtoon detection)
  if (!img && '${pageId ?? ''}'.startsWith('wt-')) {
    var wtIdx = '${pageId ?? ''}'.replace('wt-', '');
    for (var i = 0; i < allImgs.length; i++) {
      if (allImgs[i].dataset.frankIndex === wtIdx) {
        img = allImgs[i]; matchType = 'frank-index'; break;
      }
    }
  }
  // Fallback 2: match by DOM position among toon_image elements
  if (!img && '${pageId ?? ''}'.startsWith('wt-')) {
    var wtIdx = parseInt('${pageId ?? ''}'.replace('wt-', ''));
    var toonImgs = document.querySelectorAll('img.toon_image');
    if (wtIdx >= 0 && wtIdx < toonImgs.length) {
      img = toonImgs[wtIdx]; matchType = 'dom-index';
    }
  }
  if (!img) {
    console.log('[Frank] No img found for src: ' + targetSrc);
    console.log('[Frank] Available srcs:');
    for (var i = 0; i < Math.min(allImgs.length, 10); i++) {
      console.log('[Frank]   [' + i + '] src=' + allImgs[i].src.substring(0, 80) +
        ' translated=' + (allImgs[i].dataset.frankTranslated || 'none'));
    }
    return false;
  }
  console.log('[Frank] Replacing img matched by ' + matchType + ': ' + targetSrc.substring(0, 80));

  // Convert base64 to blob URL (base64 is safe: A-Za-z0-9+/= only)
  var binary = atob('$base64Data');
  var bytes = new Uint8Array(binary.length);
  for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  var blob = new Blob([bytes], { type: 'image/png' });
  var blobUrl = URL.createObjectURL(blob);

  img.src = blobUrl;
  img.dataset.frankTranslated = 'true';
  if ('${pageId ?? ''}') img.dataset.frankPageId = '${pageId ?? ''}';

  // Fire-and-forget: decode the new image, then nudge the compositor.
  // Can't use await (WebKitGTK doesn't resolve Promise return values).
  if (typeof img.decode === 'function') {
    img.decode().then(function() {
      console.log('[Frank] webtoon decode OK for ' + ('${pageId ?? ''}'));
      img.style.opacity = '0.999';
      void img.offsetWidth;
      img.style.opacity = '';
    }).catch(function(e) {
      console.error('[Frank] webtoon decode FAILED: ' + e);
    });
  }

  return true;
})();
''',
    );
    evalSw.stop();
    debugPrint(
      '[OverlayPerf] webtoon page=$pageId bytes=${imageBytes.length} '
      'b64=${base64Data.length} encodeMs=${encodeSw.elapsedMilliseconds} '
      'evalMs=${evalSw.elapsedMilliseconds}',
    );
    return result == true;
  }

  /// Replace the visible Kindle blob <img> with translated image bytes.
  /// Finds the largest visible blob img since Kindle centers pages on wide screens.
  Future<bool> replaceVisibleKindlePage(
    AppWebViewController controller,
    Uint8List imageBytes, {
    String? pageId,
    String? expectedBlobSrc,
    Map<String, num>? expectedRect,
    String? overlayToken,
  }) async {
    final encodeSw = Stopwatch()..start();
    final base64Data = await compute(base64Encode, imageBytes);
    encodeSw.stop();
    // jsonEncode properly escapes all special chars; produces "quoted" strings
    final safeExpected = expectedBlobSrc != null
        ? jsonEncode(expectedBlobSrc)
        : 'null';
    final expectedRectJson = expectedRect != null
        ? jsonEncode(expectedRect)
        : 'null';
    final safeToken = overlayToken != null ? jsonEncode(overlayToken) : 'null';
    final evalSw = Stopwatch()..start();
    final result = await controller.evaluateJavascript(
      source:
          '''
(function() {
  var expected = $safeExpected;
  var expectedRect = $expectedRectJson;
  var overlayToken = $safeToken;
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
    if (!isFinite(opacity) || opacity <= 0.05) return false;
    return true;
  }
  function topLayerHits(el) {
    if (!el) return false;
    var r = el.getBoundingClientRect();
    var pts = [
      [r.left + r.width / 2, r.top + r.height / 2],
      [r.left + r.width * 0.25, r.top + r.height * 0.5],
      [r.left + r.width * 0.75, r.top + r.height * 0.5]
    ];
    var hits = 0;
    for (var i = 0; i < pts.length; i++) {
      var x = Math.max(0, Math.min(window.innerWidth - 1, pts[i][0]));
      var y = Math.max(0, Math.min(window.innerHeight - 1, pts[i][1]));
      var top = document.elementFromPoint(x, y);
      if (!top) continue;
      if (top === el || el.contains(top) || top.contains(el)) hits++;
    }
    return hits;
  }
  function rectBiasScore(r) {
    if (!expectedRect) return 0;
    var ex = Number(expectedRect.x || 0);
    var ey = Number(expectedRect.y || 0);
    var ew = Math.max(1, Number(expectedRect.width || 1));
    var eh = Math.max(1, Number(expectedRect.height || 1));
    var cx = r.left + (r.width / 2);
    var cy = r.top + (r.height / 2);
    var ecx = ex + (ew / 2);
    var ecy = ey + (eh / 2);
    var centerDist = Math.hypot(cx - ecx, cy - ecy);
    var sizeErr =
      (Math.abs(r.width - ew) / ew) +
      (Math.abs(r.height - eh) / eh);
    // Negative penalty: closer center + similar size gets a better score.
    return -((centerDist * 800) + (sizeErr * 500000));
  }
  function overlapAreaInViewport(r, vw, vh) {
    var ox = Math.min(r.right, vw) - Math.max(r.left, 0);
    var oy = Math.min(r.bottom, vh) - Math.max(r.top, 0);
    if (ox <= 0 || oy <= 0) return 0;
    return ox * oy;
  }
  function overlapAreaWithRect(r, rr) {
    var ox = Math.min(r.right, rr.right) - Math.max(r.left, rr.left);
    var oy = Math.min(r.bottom, rr.bottom) - Math.max(r.top, rr.top);
    if (ox <= 0 || oy <= 0) return 0;
    return ox * oy;
  }

  // Prefer matching the expected blob URL when provided. This avoids replacing
  // the wrong page if the user navigated while the job was processing.
  var readerRoot = findReaderRoot();
  var imgs = readerRoot.querySelectorAll('img');
  if (!imgs || imgs.length === 0) imgs = document.querySelectorAll('img');
  var target = null;
  var targetHits = -1;
  var targetScore = -Infinity;
  var vw = window.innerWidth;
  var vh = window.innerHeight;
  var rootRect = readerRoot.getBoundingClientRect ? readerRoot.getBoundingClientRect() : null;

  if (expected) {
    for (var i = 0; i < imgs.length; i++) {
      if (!imgs[i].src || !imgs[i].src.startsWith('blob:')) continue;
      var r0 = imgs[i].getBoundingClientRect();
      if (r0.width < 100 || r0.height < 100) continue;
      var overlap0 = overlapAreaInViewport(r0, vw, vh);
      if (overlap0 < 2000) continue;
      if (rootRect && readerRoot !== document.body) {
        var rootOverlap0 = overlapAreaWithRect(r0, rootRect);
        if (rootOverlap0 < 2000) continue;
        overlap0 = Math.min(overlap0, rootOverlap0);
      }
      if (!isActuallyVisible(imgs[i])) continue;
      if (imgs[i].src === expected) {
        var hits0 = topLayerHits(imgs[i]);
        var score0 = (hits0 * 1000000000) + overlap0 + rectBiasScore(r0);
        if (!target || score0 > targetScore) {
          target = imgs[i];
          targetHits = hits0;
          targetScore = score0;
        }
      }
    }
    if (!target) {
      console.log('[Frank] Expected Kindle blob not visible, skipping overlay');
      return false;
    }
  }

  // Fallback: largest visible blob img in the viewport.
  var bestScore = -1;
  for (var i = 0; i < imgs.length; i++) {
    if (target) break;
    if (!imgs[i].src || !imgs[i].src.startsWith('blob:')) continue;
    var r = imgs[i].getBoundingClientRect();
    if (r.width < 100 || r.height < 100) continue;
    var overlap = overlapAreaInViewport(r, vw, vh);
    if (overlap < 2000) continue;
    if (rootRect && readerRoot !== document.body) {
      var rootOverlap = overlapAreaWithRect(r, rootRect);
      if (rootOverlap < 2000) continue;
      overlap = Math.min(overlap, rootOverlap);
    }
    if (!isActuallyVisible(imgs[i])) continue;
    var hits = topLayerHits(imgs[i]);
    // Prefer top-layer candidates, then visible overlap.
    var score = (hits * 1000000000) + overlap + rectBiasScore(r);
    if (score > bestScore) {
      bestScore = score;
      target = imgs[i];
    }
  }
  if (!target) {
    console.log('[Frank] No visible Kindle blob img found for overlay');
    return false;
  }
  var tRect = target.getBoundingClientRect();
  console.log('[Frank] Replacing Kindle blob img at x=' + tRect.x.toFixed(0) +
    ' size=' + tRect.width.toFixed(0) + 'x' + tRect.height.toFixed(0) +
    ' natural=' + target.naturalWidth + 'x' + target.naturalHeight +
    ' currentSrc=' + target.src.substring(0, 60));

  // Convert base64 to blob URL (base64 is safe: A-Za-z0-9+/= only)
  var b64Len = '$base64Data'.length;
  console.log('[Frank] base64 length=' + b64Len);
  var binary;
  try {
    binary = atob('$base64Data');
  } catch(e) {
    console.error('[Frank] atob() FAILED: ' + e);
    return JSON.stringify({ok: false, error: 'atob_failed', detail: String(e)});
  }
  var bytes = new Uint8Array(binary.length);
  for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  var blob = new Blob([bytes], { type: 'image/png' });
  var blobUrl = URL.createObjectURL(blob);
  console.log('[Frank] Created blob: ' + blobUrl + ' (' + binary.length + ' bytes)');

  target.src = blobUrl;
  target.dataset.frankTranslated = 'true';
  if ('${pageId ?? ''}') target.dataset.frankPageId = '${pageId ?? ''}';
  target.dataset.frankTranslatedSrc = blobUrl;
  if (overlayToken) target.dataset.frankOverlayToken = overlayToken;

  // Fire-and-forget: decode the new image, then nudge the compositor.
  // Can't use await (WebKitGTK doesn't resolve Promise return values).
  if (typeof target.decode === 'function') {
    target.decode().then(function() {
      var srcAfter = target.src;
      var srcMatch = (srcAfter === blobUrl);
      console.log('[Frank] Post-decode: decodeOk=true srcStuck=' + srcMatch +
        ' natural=' + target.naturalWidth + 'x' + target.naturalHeight +
        (srcMatch ? '' : ' OVERWRITTEN! now=' + srcAfter.substring(0, 60)));
      // Nudge compositor: imperceptible opacity change forces layer texture update.
      target.style.opacity = '0.999';
      void target.offsetWidth;
      target.style.opacity = '';
    }).catch(function(e) {
      console.error('[Frank] decode() FAILED: ' + e +
        ' naturalSize=' + target.naturalWidth + 'x' + target.naturalHeight);
      // Still try the opacity nudge even if decode failed.
      target.style.opacity = '0.999';
      void target.offsetWidth;
      target.style.opacity = '';
    });
  }

  // Sync detection tracker so page-turn detector doesn't re-fire
  if (typeof window.__frankLastBlob !== 'undefined') {
    window.__frankLastBlob = blobUrl;
  }

  // Temporary debug marker
  var dbg = document.getElementById('__frankDebugHud');
  var debugVisible = !!(dbg && dbg.style && dbg.style.display !== 'none');
  if (debugVisible) {
    target.style.outline = '3px solid rgba(76,175,80,0.95)';
    target.style.outlineOffset = '-3px';
  } else {
    target.style.outline = '';
    target.style.outlineOffset = '';
  }

  return JSON.stringify({ok: true, blobBytes: binary.length,
    blobUrl: blobUrl.substring(0, 40),
    targetNatural: target.naturalWidth + 'x' + target.naturalHeight});
})();
''',
    );
    evalSw.stop();
    debugPrint(
      '[OverlayPerf] kindle page=$pageId bytes=${imageBytes.length} '
      'b64=${base64Data.length} encodeMs=${encodeSw.elapsedMilliseconds} '
      'evalMs=${evalSw.elapsedMilliseconds}',
    );
    // Parse the JSON diagnostic result from the JS overlay script.
    if (result is String) {
      try {
        final diag = jsonDecode(result) as Map<String, dynamic>;
        final ok = diag['ok'] == true;
        debugPrint(
          '[OverlayJS] page=$pageId ok=$ok blobBytes=${diag['blobBytes']} '
          'blobUrl=${diag['blobUrl']} natural=${diag['targetNatural']}'
          '${diag['error'] != null ? ' ERROR=${diag['error']} ${diag['detail']}' : ''}',
        );
        return ok;
      } catch (_) {
        debugPrint('[OverlayJS] page=$pageId unexpected result: $result');
      }
    }
    return result == true;
  }

  /// Reapply an already generated Kindle translated blob without re-sending
  /// image bytes over the Flutter<->JS bridge.
  Future<bool> reapplyVisibleKindlePage(
    AppWebViewController controller, {
    String? pageId,
    String? expectedBlobSrc,
    Map<String, num>? expectedRect,
    String? overlayToken,
  }) async {
    final safeExpected = expectedBlobSrc != null
        ? jsonEncode(expectedBlobSrc)
        : 'null';
    final expectedRectJson = expectedRect != null
        ? jsonEncode(expectedRect)
        : 'null';
    final safeToken = overlayToken != null ? jsonEncode(overlayToken) : 'null';
    final safePageId = pageId != null ? jsonEncode(pageId) : 'null';
    final sw = Stopwatch()..start();
    final result = await controller.evaluateJavascript(
      source:
          '''
(function() {
  var expected = $safeExpected;
  var expectedRect = $expectedRectJson;
  var overlayToken = $safeToken;
  var pageId = $safePageId;

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
    if (!isFinite(opacity) || opacity <= 0.05) return false;
    return true;
  }
  function topLayerHits(el) {
    if (!el) return 0;
    var r = el.getBoundingClientRect();
    var pts = [
      [r.left + r.width / 2, r.top + r.height / 2],
      [r.left + r.width * 0.25, r.top + r.height * 0.5],
      [r.left + r.width * 0.75, r.top + r.height * 0.5]
    ];
    var hits = 0;
    for (var i = 0; i < pts.length; i++) {
      var x = Math.max(0, Math.min(window.innerWidth - 1, pts[i][0]));
      var y = Math.max(0, Math.min(window.innerHeight - 1, pts[i][1]));
      var top = document.elementFromPoint(x, y);
      if (!top) continue;
      if (top === el || el.contains(top) || top.contains(el)) hits++;
    }
    return hits;
  }
  function rectBiasScore(r) {
    if (!expectedRect) return 0;
    var ex = Number(expectedRect.x || 0);
    var ey = Number(expectedRect.y || 0);
    var ew = Math.max(1, Number(expectedRect.width || 1));
    var eh = Math.max(1, Number(expectedRect.height || 1));
    var cx = r.left + (r.width / 2);
    var cy = r.top + (r.height / 2);
    var ecx = ex + (ew / 2);
    var ecy = ey + (eh / 2);
    var centerDist = Math.hypot(cx - ecx, cy - ecy);
    var sizeErr =
      (Math.abs(r.width - ew) / ew) +
      (Math.abs(r.height - eh) / eh);
    return -((centerDist * 800) + (sizeErr * 500000));
  }
  function overlapAreaInViewport(r, vw, vh) {
    var ox = Math.min(r.right, vw) - Math.max(r.left, 0);
    var oy = Math.min(r.bottom, vh) - Math.max(r.top, 0);
    if (ox <= 0 || oy <= 0) return 0;
    return ox * oy;
  }
  function overlapAreaWithRect(r, rr) {
    var ox = Math.min(r.right, rr.right) - Math.max(r.left, rr.left);
    var oy = Math.min(r.bottom, rr.bottom) - Math.max(r.top, rr.top);
    if (ox <= 0 || oy <= 0) return 0;
    return ox * oy;
  }

  var readerRoot = findReaderRoot();
  var imgs = readerRoot.querySelectorAll('img');
  if (!imgs || imgs.length === 0) imgs = document.querySelectorAll('img');
  var target = null;
  var bestScore = -Infinity;
  var vw = window.innerWidth;
  var vh = window.innerHeight;
  var rootRect = readerRoot.getBoundingClientRect ? readerRoot.getBoundingClientRect() : null;

  for (var i = 0; i < imgs.length; i++) {
    if (!imgs[i].src || !imgs[i].src.startsWith('blob:')) continue;
    var r = imgs[i].getBoundingClientRect();
    if (r.width < 100 || r.height < 100) continue;
    var overlap = overlapAreaInViewport(r, vw, vh);
    if (overlap < 2000) continue;
    if (rootRect && readerRoot !== document.body) {
      var rootOverlap = overlapAreaWithRect(r, rootRect);
      if (rootOverlap < 2000) continue;
      overlap = Math.min(overlap, rootOverlap);
    }
    if (!isActuallyVisible(imgs[i])) continue;
    if (expected &&
        imgs[i].src !== expected) {
      continue;
    }
    var score = (topLayerHits(imgs[i]) * 1000000000) + overlap + rectBiasScore(r);
    if (score > bestScore) {
      bestScore = score;
      target = imgs[i];
    }
  }

  if (!target) return false;

  // Re-apply requires the translated blob URL stored on the element
  var translatedSrc = target.dataset.frankTranslatedSrc;
  if (!translatedSrc) return false;

  target.dataset.frankTranslated = 'true';
  if (pageId) target.dataset.frankPageId = pageId;
  if (overlayToken) target.dataset.frankOverlayToken = overlayToken;
  target.src = translatedSrc;

  if (typeof target.decode === 'function') {
    target.decode().then(function() {
      target.style.opacity = '0.999';
      void target.offsetWidth;
      target.style.opacity = '';
    }).catch(function() {
      target.style.opacity = '0.999';
      void target.offsetWidth;
      target.style.opacity = '';
    });
  }
  return true;
})();
''',
    );
    sw.stop();
    debugPrint(
      '[OverlayPerf] kindle-reapply page=$pageId '
      'ms=${sw.elapsedMilliseconds} ok=${result == true}',
    );
    return result == true;
  }

  /// Probe Kindle DOM to understand why replacement may not be visible.
  Future<Map<String, dynamic>?> probeKindleOverlay(
    AppWebViewController controller, {
    String? expectedBlobSrc,
    Map<String, num>? expectedRect,
    String? overlayToken,
  }) async {
    // jsonEncode properly escapes all special chars; produces "quoted" strings
    final safeExpected = expectedBlobSrc != null
        ? jsonEncode(expectedBlobSrc)
        : 'null';
    final expectedRectJson = expectedRect != null
        ? jsonEncode(expectedRect)
        : 'null';
    final safeToken = overlayToken != null ? jsonEncode(overlayToken) : 'null';

    final raw = await controller.evaluateJavascript(
      source:
          '''
(function() {
  function findReaderRoot() {
    return document.querySelector(
      '#kr-renderer, #kindle-reader-content, .reader-content, ' +
      '[id*="kindle-reader"], [id*="kr-renderer"], [class*="reader-content"]'
    ) || document.body;
  }
  function shortStr(v, n) {
    if (!v) return '';
    var s = String(v);
    return s.length > n ? s.substring(0, n) : s;
  }
  function cssNum(v, dflt) {
    var n = parseFloat(v || '');
    return isFinite(n) ? n : dflt;
  }
  function isVisible(el) {
    if (!el) return false;
    var st = window.getComputedStyle(el);
    if (!st) return false;
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    if (cssNum(st.opacity, 1) <= 0.05) return false;
    var r = el.getBoundingClientRect();
    if (r.width < 30 || r.height < 30) return false;
    return true;
  }
  function overlapAreaInViewport(r, vw, vh) {
    var ox = Math.min(r.right, vw) - Math.max(r.left, 0);
    var oy = Math.min(r.bottom, vh) - Math.max(r.top, 0);
    if (ox <= 0 || oy <= 0) return 0;
    return ox * oy;
  }
  function overlapAreaWithRect(r, rr) {
    var ox = Math.min(r.right, rr.right) - Math.max(r.left, rr.left);
    var oy = Math.min(r.bottom, rr.bottom) - Math.max(r.top, rr.top);
    if (ox <= 0 || oy <= 0) return 0;
    return ox * oy;
  }
  function topHits(el) {
    if (!el) return 0;
    var r = el.getBoundingClientRect();
    var pts = [
      [r.left + r.width / 2, r.top + r.height / 2],
      [r.left + r.width * 0.25, r.top + r.height * 0.5],
      [r.left + r.width * 0.75, r.top + r.height * 0.5]
    ];
    var hits = 0;
    for (var i = 0; i < pts.length; i++) {
      var x = Math.max(0, Math.min(window.innerWidth - 1, pts[i][0]));
      var y = Math.max(0, Math.min(window.innerHeight - 1, pts[i][1]));
      var top = document.elementFromPoint(x, y);
      if (!top) continue;
      if (top === el || el.contains(top) || top.contains(el)) hits++;
    }
    return hits;
  }
  function elementInfo(el) {
    if (!el) return null;
    var st = window.getComputedStyle(el);
    var r = el.getBoundingClientRect();
    return {
      tag: el.tagName || '',
      id: el.id || '',
      cls: shortStr(el.className || '', 80),
      z: st ? st.zIndex : '',
      op: st ? st.opacity : '',
      pe: st ? st.pointerEvents : '',
      rect: {
        x: Math.round(r.left),
        y: Math.round(r.top),
        w: Math.round(r.width),
        h: Math.round(r.height),
      }
    };
  }

  var expected = $safeExpected;
  var expectedRect = $expectedRectJson;
  var token = $safeToken;
  var root = findReaderRoot();
  var rootRect = root.getBoundingClientRect ? root.getBoundingClientRect() : null;
  var imgs = root.querySelectorAll('img');
  if (!imgs || imgs.length === 0) imgs = document.querySelectorAll('img');

  var candidates = [];
  for (var i = 0; i < imgs.length; i++) {
    var img = imgs[i];
    if (!img.src || !img.src.startsWith('blob:')) continue;
    var r = img.getBoundingClientRect();
    var overlapVp = overlapAreaInViewport(r, window.innerWidth, window.innerHeight);
    var inViewport = overlapVp >= 2000;
    var overlapRoot = overlapVp;
    var inRoot = true;
    if (rootRect && root !== document.body) {
      overlapRoot = overlapAreaWithRect(r, rootRect);
      inRoot = overlapRoot >= 2000;
    }
    if (!inViewport || !inRoot) continue;
    var st = window.getComputedStyle(img);
    candidates.push({
      i: i,
      src: shortStr(img.src, 48),
      visible: isVisible(img),
      overlapVp: Math.round(overlapVp),
      overlapRoot: Math.round(overlapRoot),
      topHits: topHits(img),
      expectedMatch: !!(expected && img.src === expected),
      hasToken: !!(token && img.dataset.frankOverlayToken === token),
      translated: img.dataset.frankTranslated === 'true',
      token: shortStr(img.dataset.frankOverlayToken || '', 80),
      z: st ? st.zIndex : '',
      op: st ? st.opacity : '',
      rect: {
        x: Math.round(r.left),
        y: Math.round(r.top),
        w: Math.round(r.width),
        h: Math.round(r.height),
      }
    });
  }
  candidates.sort(function(a, b) {
    if (b.topHits !== a.topHits) return b.topHits - a.topHits;
    return (b.rect.w * b.rect.h) - (a.rect.w * a.rect.h);
  });
  if (candidates.length > 8) candidates = candidates.slice(0, 8);

  var cx = window.innerWidth / 2;
  var cy = window.innerHeight / 2;
  if (expectedRect && expectedRect.width && expectedRect.height) {
    cx = Number(expectedRect.x || 0) + Number(expectedRect.width || 0) / 2;
    cy = Number(expectedRect.y || 0) + Number(expectedRect.height || 0) / 2;
  }
  cx = Math.max(0, Math.min(window.innerWidth - 1, cx));
  cy = Math.max(0, Math.min(window.innerHeight - 1, cy));
  var top = document.elementFromPoint(cx, cy);

  return JSON.stringify({
    root: elementInfo(root),
    expectedBlob: shortStr(expected || '', 48),
    expectedRect: expectedRect || null,
    overlayToken: token || null,
    center: { x: Math.round(cx), y: Math.round(cy) },
    topAtCenter: elementInfo(top),
    candidates: candidates,
  });
})();
''',
    );

    if (raw is! String || raw.isEmpty) return null;
    try {
      return Map<String, dynamic>.from(jsonDecode(raw) as Map);
    } catch (_) {
      return null;
    }
  }

  /// Legacy index-based replace (kept for non-webtoon use).
  Future<bool> replaceImage(
    AppWebViewController controller,
    String pageId,
    Uint8List imageBytes,
  ) async {
    return replaceImageBySrc(controller, pageId, imageBytes, pageId);
  }

  /// Enable tap-to-toggle inspector mode for highlighting elements.
  Future<void> enableTapMode(AppWebViewController controller) async {
    await controller.evaluateJavascript(
      source: 'window.__frankInspectorTapMode = true;',
    );
  }

  /// Disable tap-to-toggle inspector mode.
  Future<void> disableTapMode(AppWebViewController controller) async {
    await controller.evaluateJavascript(
      source: 'window.__frankInspectorTapMode = false;',
    );
  }
}
