import 'dart:typed_data';

/// Platform-agnostic WebView controller interface.
///
/// Wraps the 3 WebView APIs the app actually uses so we can swap
/// implementations per platform (InAppWebView on Android/macOS/Windows,
/// WebKitGTK via method channel on Linux).
abstract class AppWebViewController {
  /// Execute JavaScript in the WebView and return the result.
  Future<dynamic> evaluateJavascript({required String source});

  /// Take a PNG screenshot of the WebView contents.
  Future<Uint8List?> takeScreenshot();

  /// Register a JavaScript handler that can be called from the web page
  /// via `window.flutter_inappwebview.callHandler(name, ...args)`.
  void addJavaScriptHandler({
    required String handlerName,
    required Function(List<dynamic>) callback,
  });
}
