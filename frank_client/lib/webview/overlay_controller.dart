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
    String? expectedBlobSrc,
  }) async {
    final base64Data = await compute(base64Encode, imageBytes);
    final escapedExpected = expectedBlobSrc?.replaceAll("'", "\\'");
    final result = await controller.evaluateJavascript(
      source:
          '''
(function() {
  var expected = ${escapedExpected != null ? "'$escapedExpected'" : 'null'};

  // Prefer matching the expected blob URL when provided. This avoids replacing
  // the wrong page if the user navigated while the job was processing.
  var imgs = document.querySelectorAll('img');
  var target = null;
  var vw = window.innerWidth;
  var vh = window.innerHeight;

  if (expected) {
    for (var i = 0; i < imgs.length; i++) {
      if (!imgs[i].src || !imgs[i].src.startsWith('blob:')) continue;
      var r0 = imgs[i].getBoundingClientRect();
      if (r0.width < 100 || r0.height < 100) continue;
      if (r0.right < 0 || r0.left > vw || r0.bottom < 0 || r0.top > vh) continue;
      if (imgs[i].src === expected || imgs[i].dataset.frankOriginalSrc === expected) {
        target = imgs[i];
        break;
      }
    }
    if (!target) {
      console.log('[Frank] Expected Kindle blob not visible, skipping overlay');
      return false;
    }
  }

  // Fallback: largest visible blob img in the viewport.
  var bestArea = 0;
  for (var i = 0; i < imgs.length; i++) {
    if (target) break;
    if (!imgs[i].src || !imgs[i].src.startsWith('blob:')) continue;
    var r = imgs[i].getBoundingClientRect();
    if (r.width < 100 || r.height < 100) continue;
    if (r.right < 0 || r.left > vw || r.bottom < 0 || r.top > vh) continue;
    var area = r.width * r.height;
    if (area > bestArea) {
      bestArea = area;
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
  // Store the translated blob URL so toggle handler can access the latest one.
  target.dataset.frankTranslatedSrc = blobUrl;

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

  /// Legacy index-based replace (kept for non-webtoon use).
  Future<bool> replaceImage(
    AppWebViewController controller,
    String pageId,
    Uint8List imageBytes,
  ) async {
    return replaceImageBySrc(controller, pageId, imageBytes);
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
