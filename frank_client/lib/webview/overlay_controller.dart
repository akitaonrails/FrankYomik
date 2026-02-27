import 'dart:convert';
import 'dart:typed_data';
import 'package:flutter_inappwebview/flutter_inappwebview.dart';

/// Manages translated page overlay on the WebView.
class OverlayController {
  /// Replace an <img> element's src with translated image bytes (webtoon strategy).
  /// The image is injected via a blob URL created from base64 data.
  Future<bool> replaceImage(
    InAppWebViewController controller,
    String pageId,
    Uint8List imageBytes,
  ) async {
    final base64Data = base64Encode(imageBytes);
    final result = await controller.evaluateJavascript(source: '''
(function() {
  const index = parseInt('$pageId'.replace('wt-', ''));
  const selectors = [
    'img._images', 'img.comic-image', '#_imageList img',
    '.viewer-img img', '.toon_image',
  ];

  let imgs = [];
  for (const sel of selectors) {
    const found = document.querySelectorAll(sel);
    if (found.length > 0) { imgs = Array.from(found); break; }
  }
  if (!imgs.length) {
    imgs = Array.from(document.querySelectorAll('img')).filter(
      img => img.naturalWidth > 600
    );
  }

  const img = imgs[index];
  if (!img) return false;

  // Store original src for toggle
  if (!img.dataset.frankOriginalSrc) {
    img.dataset.frankOriginalSrc = img.src;
  }

  // Convert base64 to blob URL
  const binary = atob('$base64Data');
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  const blob = new Blob([bytes], { type: 'image/png' });
  const blobUrl = URL.createObjectURL(blob);

  img.src = blobUrl;
  img.dataset.frankTranslated = 'true';

  // Add toggle on click
  if (!img.dataset.frankToggle) {
    img.dataset.frankToggle = 'true';
    img.addEventListener('click', (e) => {
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

  /// Enable tap-to-toggle inspector mode for highlighting elements.
  Future<void> enableTapMode(InAppWebViewController controller) async {
    await controller.evaluateJavascript(
        source: 'window.__frankInspectorTapMode = true;');
  }

  /// Disable tap-to-toggle inspector mode.
  Future<void> disableTapMode(InAppWebViewController controller) async {
    await controller.evaluateJavascript(
        source: 'window.__frankInspectorTapMode = false;');
  }
}
