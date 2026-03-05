import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'app_webview_controller.dart';
import 'linux_webview_controller.dart';

/// Dart widget that manages the native WebKitGTK overlay on Linux.
///
/// Listens on an EventChannel for navigation and JS handler events
/// from the native side and forwards them to the appropriate callbacks.
class LinuxWebViewWidget extends StatefulWidget {
  final String initialUrl;
  final String? userAgent;
  final void Function(AppWebViewController controller)? onWebViewCreated;
  final void Function(AppWebViewController controller, String? url)? onLoadStop;
  final void Function(
    AppWebViewController controller,
    String? url,
    bool? isReload,
  )?
  onUpdateVisitedHistory;

  const LinuxWebViewWidget({
    super.key,
    required this.initialUrl,
    this.userAgent,
    this.onWebViewCreated,
    this.onLoadStop,
    this.onUpdateVisitedHistory,
  });

  @override
  State<LinuxWebViewWidget> createState() => _LinuxWebViewWidgetState();
}

class _LinuxWebViewWidgetState extends State<LinuxWebViewWidget> {
  static const _eventChannel = EventChannel('frank_client/webview_events');

  late final LinuxWebViewController _controller;
  StreamSubscription<dynamic>? _eventSub;
  bool _created = false;

  @override
  void initState() {
    super.initState();
    _controller = LinuxWebViewController();

    // Listen for events from native WebKitGTK.
    _eventSub = _eventChannel.receiveBroadcastStream().listen(_onEvent);

    // Create the native WebView after the first frame.
    WidgetsBinding.instance.addPostFrameCallback((_) => _createWebView());
  }

  Future<void> _createWebView() async {
    await _controller.create(
      url: widget.initialUrl,
      userAgent: widget.userAgent,
    );
    _created = true;
    widget.onWebViewCreated?.call(_controller);
  }

  void _onEvent(dynamic event) {
    if (event is! Map) return;
    final type = event['type'] as String?;
    final data = event['data'];

    switch (type) {
      case 'onLoadStop':
        final url = data is Map ? data['url'] as String? : null;
        widget.onLoadStop?.call(_controller, url);
        break;
      case 'onUpdateVisitedHistory':
        final url = data is Map ? data['url'] as String? : null;
        widget.onUpdateVisitedHistory?.call(_controller, url, false);
        break;
      case 'onJavaScriptHandler':
        if (data is Map) {
          final name = data['name'] as String? ?? '';
          final args = data['args'] as String? ?? '[]';
          _controller.dispatchHandler(name, args);
        }
        break;
    }
  }

  @override
  void dispose() {
    _eventSub?.cancel();
    if (_created) {
      _controller.destroy();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    // The native WebKitWebView is overlaid on the GTK window.
    // This widget is a transparent placeholder that fills the available space.
    return const SizedBox.expand();
  }
}
