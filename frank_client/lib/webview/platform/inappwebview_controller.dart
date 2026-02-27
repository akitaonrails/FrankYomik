import 'dart:typed_data';
import 'package:flutter_inappwebview/flutter_inappwebview.dart';
import 'app_webview_controller.dart';

/// Wraps [InAppWebViewController] to implement [AppWebViewController].
/// Used on Android, macOS, and Windows where flutter_inappwebview works.
class InAppWebViewControllerWrapper implements AppWebViewController {
  final InAppWebViewController _inner;

  InAppWebViewControllerWrapper(this._inner);

  @override
  Future<dynamic> evaluateJavascript({required String source}) {
    return _inner.evaluateJavascript(source: source);
  }

  @override
  Future<Uint8List?> takeScreenshot() {
    return _inner.takeScreenshot(
      screenshotConfiguration: ScreenshotConfiguration(
        compressFormat: CompressFormat.PNG,
        quality: 100,
      ),
    );
  }

  @override
  void addJavaScriptHandler({
    required String handlerName,
    required Function(List<dynamic>) callback,
  }) {
    _inner.addJavaScriptHandler(
      handlerName: handlerName,
      callback: callback,
    );
  }
}
