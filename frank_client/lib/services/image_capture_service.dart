import 'dart:math';
import 'dart:typed_data';
import 'dart:ui' as ui;
import 'package:flutter/foundation.dart';
import 'package:image/image.dart' as img;
import '../webview/platform/app_webview_controller.dart';

/// Captures images from the WebView using different strategies.
class ImageCaptureService {
  /// Take a screenshot of the WebView and return it as PNG bytes.
  Future<Uint8List?> takeScreenshot(AppWebViewController controller) async {
    return controller.takeScreenshot();
  }

  /// Extract an image from the page by evaluating JS that fetches it as base64.
  Future<Uint8List?> captureImageElement(
      AppWebViewController controller, String selector) async {
    final result = await controller.evaluateJavascript(source: '''
      (function() {
        const img = document.querySelector('$selector');
        if (!img || !img.src) return null;
        return new Promise((resolve) => {
          fetch(img.src)
            .then(r => r.blob())
            .then(blob => {
              const reader = new FileReader();
              reader.onload = () => resolve(reader.result.split(',')[1]);
              reader.readAsDataURL(blob);
            })
            .catch(() => resolve(null));
        });
      })()
    ''');

    if (result == null) return null;
    try {
      // Dart's base64 decode
      return Uint8List.fromList(
        List<int>.from(Uri.parse('data:;base64,$result').data!.contentAsBytes()),
      );
    } catch (_) {
      return null;
    }
  }

  /// Crop a screenshot to the reader content area.
  ///
  /// [screenshot] is the full WebView screenshot as PNG bytes.
  /// [contentRect] is the reader element's bounding rect from JS.
  /// [devicePixelRatio] scales CSS pixels to physical pixels.
  static Uint8List? cropToRect(
    Uint8List screenshot,
    ui.Rect contentRect,
    double devicePixelRatio,
  ) {
    final decoded = img.decodePng(screenshot);
    if (decoded == null) return null;

    final x = (contentRect.left * devicePixelRatio).round().clamp(0, decoded.width - 1);
    final y = (contentRect.top * devicePixelRatio).round().clamp(0, decoded.height - 1);
    var w = (contentRect.width * devicePixelRatio).round();
    var h = (contentRect.height * devicePixelRatio).round();

    // Clamp to image bounds
    if (x + w > decoded.width) w = decoded.width - x;
    if (y + h > decoded.height) h = decoded.height - y;
    if (w <= 0 || h <= 0) return null;

    final cropped = img.copyCrop(decoded, x: x, y: y, width: w, height: h);
    return Uint8List.fromList(img.encodePng(cropped));
  }

  /// Split a 2-page spread image into left and right halves.
  ///
  /// Returns a pair of PNG-encoded images: (left, right).
  /// Returns null if the image can't be decoded.
  static (Uint8List left, Uint8List right)? splitSpread(Uint8List imageBytes) {
    final decoded = img.decodePng(imageBytes);
    if (decoded == null) return null;

    final halfWidth = decoded.width ~/ 2;

    final left = img.copyCrop(decoded,
        x: 0, y: 0, width: halfWidth, height: decoded.height);
    final right = img.copyCrop(decoded,
        x: halfWidth, y: 0, width: decoded.width - halfWidth, height: decoded.height);

    return (
      Uint8List.fromList(img.encodePng(left)),
      Uint8List.fromList(img.encodePng(right)),
    );
  }

  /// Stitch left and right spread halves into a single full-spread image.
  ///
  /// Returns a PNG-encoded image combining both halves side by side.
  /// Returns null if either image can't be decoded.
  static Uint8List? stitchSpread(Uint8List left, Uint8List right) {
    final leftImg = img.decodePng(left);
    final rightImg = img.decodePng(right);
    if (leftImg == null || rightImg == null) return null;

    final height = max(leftImg.height, rightImg.height);
    final stitched = img.Image(
      width: leftImg.width + rightImg.width,
      height: height,
    );
    img.compositeImage(stitched, leftImg, dstX: 0, dstY: 0);
    img.compositeImage(stitched, rightImg, dstX: leftImg.width, dstY: 0);
    return Uint8List.fromList(img.encodePng(stitched));
  }

  // --- Async versions that run on a background isolate ---

  /// [splitSpread] on a background isolate.
  static Future<(Uint8List, Uint8List)?> splitSpreadAsync(
      Uint8List imageBytes) {
    return compute(splitSpread, imageBytes);
  }

  /// [stitchSpread] on a background isolate.
  static Future<Uint8List?> stitchSpreadAsync(
      Uint8List left, Uint8List right) {
    return compute(_stitchSpreadWorker, (left, right));
  }

  static Uint8List? _stitchSpreadWorker((Uint8List, Uint8List) args) {
    return stitchSpread(args.$1, args.$2);
  }

  /// [cropToRect] on a background isolate.
  static Future<Uint8List?> cropToRectAsync(
    Uint8List screenshot,
    ui.Rect contentRect,
    double devicePixelRatio,
  ) {
    return compute(
      _cropToRectWorker,
      (screenshot, contentRect.left, contentRect.top, contentRect.width,
          contentRect.height, devicePixelRatio),
    );
  }

  static Uint8List? _cropToRectWorker(
      (Uint8List, double, double, double, double, double) args) {
    final (bytes, left, top, width, height, dpr) = args;
    return cropToRect(bytes, ui.Rect.fromLTWH(left, top, width, height), dpr);
  }
}
