import 'dart:convert';
import 'dart:typed_data';
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
    final base64Data = base64Encode(imageBytes);
    // Escape single quotes in the URL
    final escapedSrc = originalSrc.replaceAll("'", "\\'");
    final result = await controller.evaluateJavascript(source: '''
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

  // Add toggle on click (once)
  if (!img.dataset.frankToggle) {
    img.dataset.frankToggle = 'true';
    img.addEventListener('click', function(e) {
      e.preventDefault();
      e.stopPropagation();
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
''');
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
        source: 'window.__frankInspectorTapMode = true;');
  }

  /// Disable tap-to-toggle inspector mode.
  Future<void> disableTapMode(AppWebViewController controller) async {
    await controller.evaluateJavascript(
        source: 'window.__frankInspectorTapMode = false;');
  }
}
