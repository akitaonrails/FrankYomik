import 'dart:convert';
import 'package:flutter/services.dart';
import 'app_webview_controller.dart';

/// Method channel client implementing [AppWebViewController] for Linux.
/// Communicates with the native WebKitGTK bridge via platform channels.
class LinuxWebViewController implements AppWebViewController {
  static const _channel = MethodChannel('frank_client/webview');

  final Map<String, Function(List<dynamic>)> _handlers = {};

  /// Create and show the native WebKitWebView, loading [url].
  Future<void> create({required String url, String? userAgent}) async {
    await _channel.invokeMethod('create', {
      'url': url,
      if (userAgent != null) 'userAgent': userAgent,
    });
  }

  /// Update the WebView overlay position/size (reserved for future use).
  Future<void> setFrame(double left, double top, double width, double height) async {
    await _channel.invokeMethod('setFrame', {
      'left': left,
      'top': top,
      'width': width,
      'height': height,
    });
  }

  @override
  Future<dynamic> evaluateJavascript({required String source}) async {
    return _channel.invokeMethod('evaluateJavascript', {'source': source});
  }

  @override
  Future<Uint8List?> takeScreenshot() async {
    final result = await _channel.invokeMethod<Uint8List>('takeScreenshot');
    return result;
  }

  @override
  void addJavaScriptHandler({
    required String handlerName,
    required Function(List<dynamic>) callback,
  }) {
    _handlers[handlerName] = callback;
    // Register on native side (fire-and-forget).
    _channel.invokeMethod('addJavaScriptHandler', {'name': handlerName});
  }

  /// Dispatch a JS handler callback received from the native event channel.
  void dispatchHandler(String name, String argsJson) {
    final handler = _handlers[name];
    if (handler == null) return;
    try {
      final decoded = jsonDecode(argsJson);
      handler(decoded is List ? decoded : [decoded]);
    } catch (_) {
      handler([argsJson]);
    }
  }

  /// Destroy the native WebKitWebView.
  Future<void> destroy() async {
    await _channel.invokeMethod('destroy');
    _handlers.clear();
  }
}
