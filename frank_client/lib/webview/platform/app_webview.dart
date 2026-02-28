import 'dart:collection';
import 'dart:io' show Platform;
import 'package:flutter/material.dart';
import 'package:flutter_inappwebview/flutter_inappwebview.dart';
import '../../screens/reader_screen.dart' show antiBotScript;
import 'app_webview_controller.dart';
import 'inappwebview_controller.dart';
import 'linux_webview_widget.dart';

/// Platform-switching WebView widget.
///
/// On Android/macOS/Windows: renders [InAppWebView] from flutter_inappwebview.
/// On Linux: renders [LinuxWebViewWidget] backed by native WebKitGTK.
class AppWebView extends StatelessWidget {
  final String initialUrl;
  final String? userAgent;
  final void Function(AppWebViewController controller)? onWebViewCreated;
  final void Function(AppWebViewController controller, String? url)? onLoadStop;
  final void Function(
          AppWebViewController controller, String? url, bool? isReload)?
      onUpdateVisitedHistory;

  const AppWebView({
    super.key,
    required this.initialUrl,
    this.userAgent,
    this.onWebViewCreated,
    this.onLoadStop,
    this.onUpdateVisitedHistory,
  });

  @override
  Widget build(BuildContext context) {
    if (Platform.isLinux) {
      return LinuxWebViewWidget(
        initialUrl: initialUrl,
        userAgent: userAgent,
        onWebViewCreated: onWebViewCreated,
        onLoadStop: onLoadStop,
        onUpdateVisitedHistory: onUpdateVisitedHistory,
      );
    }

    // Android, macOS, Windows — use flutter_inappwebview
    InAppWebViewControllerWrapper? wrapper;
    return InAppWebView(
      initialUrlRequest: URLRequest(url: WebUri(initialUrl)),
      initialSettings: InAppWebViewSettings(
        javaScriptEnabled: true,
        domStorageEnabled: true,
        databaseEnabled: true,
        cacheEnabled: true,
        thirdPartyCookiesEnabled: true,
        userAgent: userAgent ??
            'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36',
        javaScriptCanOpenWindowsAutomatically: true,
        supportMultipleWindows: false,
        mixedContentMode: MixedContentMode.MIXED_CONTENT_ALWAYS_ALLOW,
      ),
      initialUserScripts: UnmodifiableListView([
        UserScript(
          source: antiBotScript,
          injectionTime: UserScriptInjectionTime.AT_DOCUMENT_START,
        ),
      ]),
      onWebViewCreated: (controller) {
        wrapper = InAppWebViewControllerWrapper(controller);
        onWebViewCreated?.call(wrapper!);
      },
      onLoadStop: (controller, url) {
        wrapper ??= InAppWebViewControllerWrapper(controller);
        onLoadStop?.call(wrapper!, url?.toString());
      },
      onUpdateVisitedHistory: (controller, url, isReload) {
        wrapper ??= InAppWebViewControllerWrapper(controller);
        onUpdateVisitedHistory?.call(wrapper!, url?.toString(), isReload);
      },
    );
  }
}
