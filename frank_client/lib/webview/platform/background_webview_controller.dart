import 'dart:async';
import 'dart:convert';
import 'package:flutter/services.dart';

/// Method channel client for the hidden background WebKitGTK webview.
///
/// Uses the `frank_client/bg_webview` channel (separate from the main webview).
/// The bg webview shares the same WebKitWebContext as the main webview,
/// meaning it inherits cookies/session — enabling authenticated prefetch.
class BackgroundWebViewController {
  static const _channel = MethodChannel('frank_client/bg_webview');
  static const _eventChannel = EventChannel('frank_client/bg_webview_events');

  final Map<String, Function(List<dynamic>)> _handlers = {};
  bool _listening = false;
  StreamSubscription<dynamic>? _eventSub;

  // GDK keyvals for arrow keys (from gdk/gdkkeysyms.h).
  static const int gdkKeyLeft = 0xff51;
  static const int gdkKeyRight = 0xff53;

  /// Create the background webview and navigate to [url].
  Future<void> create({required String url}) async {
    await _channel.invokeMethod('create', {'url': url});
  }

  /// Execute JavaScript in the background webview.
  Future<dynamic> evaluateJavascript({required String source}) async {
    return _channel.invokeMethod('evaluateJavascript', {'source': source});
  }

  /// Register a JS handler on the background webview's content manager.
  void addJavaScriptHandler({
    required String handlerName,
    required Function(List<dynamic>) callback,
  }) {
    _handlers[handlerName] = callback;
    _channel.invokeMethod('addJavaScriptHandler', {'name': handlerName});
  }

  /// Send a trusted GDK key event to the background webview.
  Future<void> sendKey(int keyval) async {
    await _channel.invokeMethod('sendKey', {'keyval': keyval});
  }

  /// Turn to the next page (ArrowLeft for manga RTL reading).
  Future<void> nextPage() => sendKey(gdkKeyLeft);

  /// Turn to the previous page (ArrowRight for manga RTL reading).
  Future<void> previousPage() => sendKey(gdkKeyRight);

  /// Start listening for events from the background webview.
  void startListening({void Function(String url)? onLoadStop}) {
    if (_listening) return;
    _listening = true;

    _eventSub = _eventChannel.receiveBroadcastStream().listen((event) {
      if (event is! Map) return;
      final type = event['type'] as String?;
      final data = event['data'];

      switch (type) {
        case 'onLoadStop':
          final url = data is Map ? data['url'] as String? : null;
          onLoadStop?.call(url ?? '');
          break;
        case 'onJavaScriptHandler':
          if (data is Map) {
            final name = data['name'] as String? ?? '';
            final args = data['args'] as String? ?? '[]';
            _dispatchHandler(name, args);
          }
          break;
      }
    });
  }

  void _dispatchHandler(String name, String argsJson) {
    final handler = _handlers[name];
    if (handler == null) return;
    try {
      final decoded = jsonDecode(argsJson);
      handler(decoded is List ? decoded : [decoded]);
    } catch (_) {
      handler([argsJson]);
    }
  }

  /// Destroy the background webview.
  Future<void> destroy() async {
    await _eventSub?.cancel();
    _eventSub = null;
    _listening = false;
    await _channel.invokeMethod('destroy');
    _handlers.clear();
  }
}
