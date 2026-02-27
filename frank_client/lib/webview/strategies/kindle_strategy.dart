import 'base_strategy.dart';

/// Strategy for read.amazon.co.jp — screenshot-based capture (bypasses DRM).
class KindleStrategy extends SiteStrategy {
  @override
  String get siteName => 'kindle';

  @override
  String get urlPattern => r'read\.amazon\.co\.jp';

  /// Aspect ratio threshold: width > height * this = 2-page spread.
  static const double spreadThreshold = 1.3;

  @override
  String get detectionScript => '''
(function() {
  if (window.__frankDetectorActive) return;
  window.__frankDetectorActive = true;
  window.__frankCurrentPage = null;

  function detectPageChange() {
    const pageIndicator = document.querySelector(
      '#kr-page-indicator, .page-number, [class*="pageNum"], [class*="page-count"]'
    );
    const readerContent = document.querySelector(
      '#kr-renderer, #kindle-reader-content, .reader-content, canvas'
    );

    if (!readerContent) return;

    let pageNum = '0';
    if (pageIndicator) {
      const match = pageIndicator.textContent.match(/(\\d+)/);
      if (match) pageNum = match[1];
    } else if (location.hash) {
      const hashMatch = location.hash.match(/page=(\\d+)/);
      if (hashMatch) pageNum = hashMatch[1];
    }

    const rect = readerContent.getBoundingClientRect();
    const w = rect.width || window.innerWidth;
    const h = rect.height || window.innerHeight;
    const isSpread = w > h * $spreadThreshold;
    const pageMode = isSpread ? 'spread' : 'single';
    const pageId = isSpread ? ('kindle-' + pageNum + '-spread') : ('kindle-' + pageNum);

    if (pageId !== window.__frankCurrentPage) {
      window.__frankCurrentPage = pageId;
      window.flutter_inappwebview.callHandler('onPageDetected', {
        pageId: pageId,
        index: parseInt(pageNum),
        type: 'screenshot',
        pageMode: pageMode,
        readerRect: {
          x: rect.left,
          y: rect.top,
          width: w,
          height: h,
        },
        devicePixelRatio: window.devicePixelRatio || 1,
      });
    }
  }

  setInterval(detectPageChange, 1000);
  document.addEventListener('click', () => setTimeout(detectPageChange, 500));
  document.addEventListener('keyup', () => setTimeout(detectPageChange, 500));

  console.log('[Frank] Kindle detection script injected');
})();
''';

  @override
  String captureScript(String pageId) {
    return '''
(function() {
  const reader = document.querySelector(
    '#kr-renderer, #kindle-reader-content, .reader-content, canvas'
  );
  if (!reader) return null;

  const rect = reader.getBoundingClientRect();
  const w = rect.width || window.innerWidth;
  const h = rect.height || window.innerHeight;
  return JSON.stringify({
    x: rect.left,
    y: rect.top,
    width: w,
    height: h,
    devicePixelRatio: window.devicePixelRatio || 1,
    pageMode: (w > h * $spreadThreshold) ? 'spread' : 'single',
  });
})();
''';
  }

  @override
  PageMetadata? parseUrl(String url) {
    final uri = Uri.tryParse(url);
    if (uri == null) return null;

    final asinMatch = RegExp(r'[/=](B[A-Z0-9]{9})').firstMatch(url);
    final asin = asinMatch?.group(1) ?? 'unknown';

    return PageMetadata(
      title: asin,
      chapter: '1',
      pageNumber: '0',
      sourceUrl: url,
    );
  }

  /// Determine page mode from dimensions (used in tests and Dart-side logic).
  static String pageModeFromSize(double width, double height) {
    return width > height * spreadThreshold ? 'spread' : 'single';
  }

  /// JS that performs a deep DOM scan of the Kindle reader area.
  /// Results are sent via onInspectorLog with type 'kindle_dom'.
  static String get diagnosticScript => '''
(function() {
  const results = {
    type: 'kindle_dom',
    timestamp: Date.now(),
    viewport: {
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      devicePixelRatio: window.devicePixelRatio || 1,
    },
    body: {
      scrollWidth: document.body ? document.body.scrollWidth : 0,
      scrollHeight: document.body ? document.body.scrollHeight : 0,
      clientWidth: document.body ? document.body.clientWidth : 0,
      clientHeight: document.body ? document.body.clientHeight : 0,
    },
    readerSelectors: {},
    canvases: [],
    readerElements: [],
    computedRect: null,
    spreadDetected: false,
  };

  // Check known reader selectors
  const selectors = [
    '#kr-renderer',
    '#kindle-reader-content',
    '.reader-content',
    'canvas',
    '#kr-page-indicator',
    '.page-number',
    '[class*="pageNum"]',
    '[class*="page-count"]',
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el) {
      const r = el.getBoundingClientRect();
      results.readerSelectors[sel] = {
        tag: el.tagName,
        id: el.id,
        classes: el.className,
        rect: { x: r.x, y: r.y, width: r.width, height: r.height },
        text: el.textContent ? el.textContent.substring(0, 100) : '',
      };
    }
  }

  // All canvas elements
  document.querySelectorAll('canvas').forEach((c, i) => {
    const r = c.getBoundingClientRect();
    const style = window.getComputedStyle(c);
    results.canvases.push({
      index: i,
      width: c.width,
      height: c.height,
      displayWidth: r.width,
      displayHeight: r.height,
      rect: { x: r.x, y: r.y, width: r.width, height: r.height },
      zIndex: style.zIndex,
      id: c.id,
      classes: c.className,
    });
  });

  // Elements with reader/kindle/kr- in id or class
  const allEls = document.querySelectorAll('*');
  const pattern = /reader|page|kindle|kr-/i;
  for (const el of allEls) {
    if ((el.id && pattern.test(el.id)) ||
        (el.className && typeof el.className === 'string' && pattern.test(el.className))) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        results.readerElements.push({
          tag: el.tagName,
          id: el.id,
          classes: typeof el.className === 'string' ? el.className : '',
          rect: { x: r.x, y: r.y, width: r.width, height: r.height },
        });
      }
      if (results.readerElements.length >= 50) break;
    }
  }

  // Compute reader rect the same way detection script does
  const readerContent = document.querySelector(
    '#kr-renderer, #kindle-reader-content, .reader-content, canvas'
  );
  if (readerContent) {
    const rect = readerContent.getBoundingClientRect();
    const w = rect.width || window.innerWidth;
    const h = rect.height || window.innerHeight;
    results.computedRect = { x: rect.left, y: rect.top, width: w, height: h };
    results.spreadDetected = w > h * $spreadThreshold;
  }

  window.flutter_inappwebview.callHandler('onInspectorLog', results);
  console.log('[Frank] Kindle diagnostic scan complete');
})();
''';
}
