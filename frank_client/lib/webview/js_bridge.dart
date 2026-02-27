import 'package:flutter_inappwebview/flutter_inappwebview.dart';
import 'strategies/base_strategy.dart';
import 'strategies/kindle_strategy.dart';
import 'strategies/webtoon_strategy.dart';

typedef PageDetectedCallback = void Function(Map<String, dynamic> pageInfo);

/// Manages the Dart <-> JS bridge for the WebView.
class JsBridge {
  final List<SiteStrategy> _strategies = [
    WebtoonStrategy(),
    KindleStrategy(),
  ];

  SiteStrategy? activeStrategy;
  PageDetectedCallback? onPageDetected;

  /// Register JS handlers on the WebView controller.
  void attach(InAppWebViewController controller) {
    controller.addJavaScriptHandler(
      handlerName: 'onPageDetected',
      callback: (args) {
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
      InAppWebViewController controller, String url) async {
    SiteStrategy? matched;
    for (final s in _strategies) {
      if (s.matches(url)) {
        matched = s;
        break;
      }
    }

    if (matched != null && matched != activeStrategy) {
      activeStrategy = matched;
      // Inject detection script after a short delay to let the page load
      await Future.delayed(const Duration(seconds: 2));
      await controller.evaluateJavascript(source: matched.detectionScript);
    }
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
