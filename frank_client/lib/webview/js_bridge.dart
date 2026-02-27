import 'package:flutter/foundation.dart';
import 'platform/app_webview_controller.dart';
import 'strategies/base_strategy.dart';
import 'strategies/kindle_strategy.dart';
import 'strategies/naver_webtoon_strategy.dart';

typedef PageDetectedCallback = void Function(Map<String, dynamic> pageInfo);

/// Manages the Dart <-> JS bridge for the WebView.
class JsBridge {
  final List<SiteStrategy> _strategies = [
    NaverWebtoonStrategy(),
    KindleStrategy(),
  ];

  SiteStrategy? activeStrategy;
  PageDetectedCallback? onPageDetected;

  /// Register JS handlers on the WebView controller.
  void attach(AppWebViewController controller) {
    debugPrint('[JsBridge] attach — registering onPageDetected handler');
    controller.addJavaScriptHandler(
      handlerName: 'onPageDetected',
      callback: (args) {
        debugPrint('[JsBridge] onPageDetected callback: $args');
        if (args.isNotEmpty && args[0] is Map) {
          final info = Map<String, dynamic>.from(args[0] as Map);
          onPageDetected?.call(info);
        }
        return null;
      },
    );
  }

  /// Detect which strategy matches the URL and inject its detection script.
  Future<void> onUrlChanged(
      AppWebViewController controller, String url) async {
    debugPrint('[JsBridge] onUrlChanged: $url');
    SiteStrategy? matched;
    for (final s in _strategies) {
      if (s.matches(url)) {
        matched = s;
        break;
      }
    }

    if (matched == null) {
      debugPrint('[JsBridge] No strategy matched');
      return;
    }

    activeStrategy = matched;
    debugPrint('[JsBridge] Matched strategy: ${matched.siteName}, injecting detection script...');
    await Future.delayed(const Duration(seconds: 2));
    await controller.evaluateJavascript(source: matched.detectionScript);
    debugPrint('[JsBridge] Detection script injected');
  }

  /// Get metadata for the current URL.
  PageMetadata? parseCurrentUrl(String url) {
    return activeStrategy?.parseUrl(url);
  }

  /// Generate capture script for a specific page.
  String? getCaptureScript(String pageId) {
    return activeStrategy?.captureScript(pageId);
  }
}
