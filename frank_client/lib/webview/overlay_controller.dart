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
    final base64Data = await compute(base64Encode, imageBytes);
    // Escape single quotes in the URL
    final escapedSrc = originalSrc.replaceAll("'", "\\'");
    final result = await controller.evaluateJavascript(
      source:
          '''
(function() {
  var targetSrc = '$escapedSrc';

  // Find the img by matching src or data-frank-original-src
  var allImgs = document.querySelectorAll('img');
  var img = null;
  var matchType = '';
  for (var i = 0; i < allImgs.length; i++) {
    if (allImgs[i].dataset.frankOriginalSrc === targetSrc) {
      img = allImgs[i];
      matchType = 'original-src-attr(already replaced)';
      break;
    }
    if (allImgs[i].src === targetSrc) {
      img = allImgs[i];
      matchType = 'current-src';
      break;
    }
  }
  if (!img) {
    console.log('[Frank] No img found for src: ' + targetSrc);
    console.log('[Frank] Available srcs:');
    for (var i = 0; i < Math.min(allImgs.length, 10); i++) {
      console.log('[Frank]   [' + i + '] src=' + allImgs[i].src.substring(0, 80) +
        ' orig=' + (allImgs[i].dataset.frankOriginalSrc || 'none'));
    }
    return false;
  }
  console.log('[Frank] Replacing img matched by ' + matchType + ': ' + targetSrc.substring(0, 80));

  // Store original src for toggle
  if (!img.dataset.frankOriginalSrc) {
    img.dataset.frankOriginalSrc = img.src;
  }

  // Convert base64 to blob URL
  var binary = atob('$base64Data');
  var bytes = new Uint8Array(binary.length);
  for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  var blob = new Blob([bytes], { type: 'image/png' });
  var blobUrl = URL.createObjectURL(blob);

  img.src = blobUrl;
  img.dataset.frankTranslated = 'true';
  if ('${pageId ?? ''}') img.dataset.frankPageId = '${pageId ?? ''}';

  // Add toggle on double-click (once) — single clicks pass through
  // to the site's own navigation (Kindle bars, page turns, etc.)
  if (!img.dataset.frankToggle) {
    img.dataset.frankToggle = 'true';
    img.addEventListener('dblclick', function(e) {
      if (img.dataset.frankTranslated === 'true') {
        img.src = img.dataset.frankOriginalSrc;
        img.dataset.frankTranslated = 'false';
      } else {
        img.src = blobUrl;
        img.dataset.frankTranslated = 'true';
      }
    });
  }

  return true;
})();
''',
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
    final base64Data = await compute(base64Encode, imageBytes);
    final escapedExpected = expectedBlobSrc?.replaceAll("'", "\\'");
    final expectedRectJson = expectedRect != null
        ? jsonEncode(expectedRect)
        : 'null';
    final escapedToken = overlayToken?.replaceAll("'", "\\'");
    final result = await controller.evaluateJavascript(
      source:
          '''
(function() {
  var expected = ${escapedExpected != null ? "'$escapedExpected'" : 'null'};
  var expectedRect = $expectedRectJson;
  var overlayToken = ${escapedToken != null ? "'$escapedToken'" : 'null'};
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
      if (imgs[i].src === expected || imgs[i].dataset.frankOriginalSrc === expected) {
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
  console.log('[Frank] Replacing Kindle blob img at x=' + target.getBoundingClientRect().x.toFixed(0));

  // Always update the original src to the CURRENT blob (Kindle regenerates
  // blob URLs on each page visit, so the old originalSrc would be stale).
  target.dataset.frankOriginalSrc = target.src;

  // Convert base64 to blob URL
  var binary = atob('$base64Data');
  var bytes = new Uint8Array(binary.length);
  for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  var blob = new Blob([bytes], { type: 'image/png' });
  var blobUrl = URL.createObjectURL(blob);

  target.src = blobUrl;
  target.dataset.frankTranslated = 'true';
  if ('${pageId ?? ''}') target.dataset.frankPageId = '${pageId ?? ''}';
  // Store the translated blob URL so toggle handler can access the latest one.
  target.dataset.frankTranslatedSrc = blobUrl;
  if (overlayToken) target.dataset.frankOverlayToken = overlayToken;

  // Temporary debug marker: when HUD is visible, draw a green outline so it's
  // obvious that the current page image was replaced.
  var dbg = document.getElementById('__frankDebugHud');
  var debugVisible = !!(dbg && dbg.style && dbg.style.display !== 'none');
  if (debugVisible) {
    target.style.outline = '3px solid rgba(76,175,80,0.95)';
    target.style.outlineOffset = '-3px';
  } else {
    target.style.outline = '';
    target.style.outlineOffset = '';
  }

  // Sync detection tracker so page-turn detector doesn't re-fire
  if (typeof window.__frankLastBlob !== 'undefined') {
    window.__frankLastBlob = blobUrl;
  }

  // Add toggle on double-click (once) — single clicks pass through
  // to Kindle's own navigation (show/hide bars, page turns, etc.).
  // Uses dataset attributes so the handler always uses the latest URLs
  // even when the overlay is re-applied for a different page.
  if (!target.dataset.frankToggle) {
    target.dataset.frankToggle = 'true';
    target.addEventListener('dblclick', function(e) {
      if (target.dataset.frankTranslated === 'true') {
        target.src = target.dataset.frankOriginalSrc;
        target.dataset.frankTranslated = 'false';
        target.style.outline = '';
        target.style.outlineOffset = '';
      } else {
        target.src = target.dataset.frankTranslatedSrc;
        target.dataset.frankTranslated = 'true';
        if (debugVisible) {
          target.style.outline = '3px solid rgba(76,175,80,0.95)';
          target.style.outlineOffset = '-3px';
        }
      }
      // Keep detection tracker in sync after toggle
      if (typeof window.__frankLastBlob !== 'undefined') {
        window.__frankLastBlob = target.src;
      }
    });
  }

  return true;
})();
''',
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
    final escapedExpected = expectedBlobSrc?.replaceAll("'", "\\'");
    final expectedRectJson = expectedRect != null
        ? jsonEncode(expectedRect)
        : 'null';
    final escapedToken = overlayToken?.replaceAll("'", "\\'");

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

  var expected = ${escapedExpected != null ? "'$escapedExpected'" : 'null'};
  var expectedRect = $expectedRectJson;
  var token = ${escapedToken != null ? "'$escapedToken'" : 'null'};
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
      expectedMatch: !!(expected && (img.src === expected || img.dataset.frankOriginalSrc === expected)),
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
