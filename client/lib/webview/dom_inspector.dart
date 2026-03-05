import 'platform/app_webview_controller.dart';

/// DOM inspector for debugging site rendering.
/// Injects JS to log DOM elements, highlight tapped elements, and capture network requests.
class DomInspector {
  bool isActive = false;
  final List<Map<String, dynamic>> _logs = [];
  static const _maxLogs = 500;

  List<Map<String, dynamic>> get logs => List.unmodifiable(_logs);

  /// Inject inspector JS into the WebView.
  Future<void> inject(AppWebViewController controller) async {
    await controller.evaluateJavascript(source: _inspectorScript);
  }

  /// Register the log handler on the controller.
  void attach(AppWebViewController controller) {
    controller.addJavaScriptHandler(
      handlerName: 'onInspectorLog',
      callback: (args) {
        if (args.isNotEmpty && args[0] is Map) {
          _logs.add(Map<String, dynamic>.from(args[0] as Map));
          if (_logs.length > _maxLogs) {
            _logs.removeAt(0);
          }
        }
        return null;
      },
    );
  }

  /// Add a Dart-side log entry into the same stream as JS logs.
  void log(Map<String, dynamic> entry) {
    _logs.add(entry);
    if (_logs.length > _maxLogs) {
      _logs.removeAt(0);
    }
  }

  void clear() => _logs.clear();

  static const _inspectorScript = '''
(function() {
  if (window.__frankInspector) return;
  window.__frankInspector = true;

  // Log all images on the page
  function logImages() {
    const imgs = document.querySelectorAll('img');
    imgs.forEach((img, i) => {
      window.flutter_inappwebview.callHandler('onInspectorLog', {
        type: 'image',
        index: i,
        tag: img.tagName,
        src: img.src,
        naturalWidth: img.naturalWidth,
        naturalHeight: img.naturalHeight,
        displayed: img.offsetWidth > 0 && img.offsetHeight > 0,
        classes: img.className,
        id: img.id,
        alt: img.alt,
      });
    });
  }

  // Log canvas elements
  function logCanvases() {
    const canvases = document.querySelectorAll('canvas');
    canvases.forEach((c, i) => {
      window.flutter_inappwebview.callHandler('onInspectorLog', {
        type: 'canvas',
        index: i,
        width: c.width,
        height: c.height,
        classes: c.className,
        id: c.id,
      });
    });
  }

  // Highlight element on tap
  let highlightEl = null;
  document.addEventListener('click', (e) => {
    if (!window.__frankInspectorTapMode) return;
    e.preventDefault();
    e.stopPropagation();

    if (highlightEl) highlightEl.style.outline = '';

    const el = e.target;
    el.style.outline = '3px solid red';
    highlightEl = el;

    const rect = el.getBoundingClientRect();
    window.flutter_inappwebview.callHandler('onInspectorLog', {
      type: 'tap',
      tag: el.tagName,
      id: el.id,
      classes: el.className,
      src: el.src || '',
      rect: { x: rect.x, y: rect.y, w: rect.width, h: rect.height },
      innerHTML: el.innerHTML.substring(0, 200),
      attributes: Array.from(el.attributes).map(a => a.name + '=' + a.value),
    });
  }, true);

  // Log DOM summary
  function logSummary() {
    window.flutter_inappwebview.callHandler('onInspectorLog', {
      type: 'summary',
      url: location.href,
      title: document.title,
      images: document.querySelectorAll('img').length,
      canvases: document.querySelectorAll('canvas').length,
      iframes: document.querySelectorAll('iframe').length,
      scripts: document.querySelectorAll('script').length,
      bodyChildren: document.body ? document.body.children.length : 0,
    });
  }

  logSummary();
  logImages();
  logCanvases();

  console.log('[Frank] DOM Inspector injected');
})();
''';
}
