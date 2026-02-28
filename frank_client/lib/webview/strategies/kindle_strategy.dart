import 'base_strategy.dart';

/// Strategy for read.amazon.co.jp — DOM-based capture via blob img extraction.
class KindleStrategy extends SiteStrategy {
  @override
  String get siteName => 'kindle';

  @override
  String get urlPattern => r'read\.amazon\.co\.jp';

  /// Aspect ratio threshold: width > height * this = 2-page spread.
  static const double spreadThreshold = 1.3;

  /// Shared JS helper: find the largest visible blob img in the viewport.
  /// Kindle centers pages on wide/maximized windows, so we can't assume x ≈ 0.
  /// Instead, pick the largest blob img whose bounding rect overlaps the viewport.
  static const String _findVisibleBlobFn = '''
  function __frankFindVisibleBlob() {
    var imgs = document.querySelectorAll('img');
    var best = null;
    var bestArea = 0;
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    for (var i = 0; i < imgs.length; i++) {
      if (!imgs[i].src || !imgs[i].src.startsWith('blob:')) continue;
      var r = imgs[i].getBoundingClientRect();
      if (r.width < 100 || r.height < 100) continue;
      // Check that the image overlaps the viewport
      if (r.right < 0 || r.left > vw || r.bottom < 0 || r.top > vh) continue;
      var area = r.width * r.height;
      if (area > bestArea) {
        bestArea = area;
        best = imgs[i];
      }
    }
    return best;
  }
''';

  @override
  String get detectionScript =>
      '''
(function() {
  if (window.__frankDetectorActive) return;
  window.__frankDetectorActive = true;
  window.__frankSessionId = window.__frankSessionId ||
    (Date.now().toString(36) + Math.random().toString(36).slice(2, 6));
  window.__frankPageCounter = window.__frankPageCounter || 0;
  window.__frankNavIntent = window.__frankNavIntent || 'forward';
  window.__frankLastBlob = null;
  window.__frankLastRect = null;
  window.__frankLastEmitAt = 0;
  window.__frankLastEmitBlob = null;
  window.__frankUserNavAt = Date.now();

$_findVisibleBlobFn

  function detectPageChange() {
    var target = __frankFindVisibleBlob();
    if (!target) return; // Canvas still rendering, not ready yet

    var blobSrc = target.src;
    if (blobSrc === window.__frankLastBlob) return; // Same page

    var rect = target.getBoundingClientRect();
    var w = rect.width;
    var h = rect.height;
    var now = Date.now();
    var userNavRecent = (now - window.__frankUserNavAt) < 4000;

    // Kindle may regenerate blob URLs without a true page turn (resize/repaint).
    // Treat blob-only churn as a page turn only if user navigation happened
    // recently, or if geometry changed meaningfully.
    if (!userNavRecent && window.__frankLastRect) {
      var last = window.__frankLastRect;
      var dw = Math.abs(w - last.w) / Math.max(1, last.w);
      var dh = Math.abs(h - last.h) / Math.max(1, last.h);
      var emittedVeryRecently = (now - window.__frankLastEmitAt) < 600;
      // Kindle can briefly churn blob URLs during repaint. Suppress only
      // near-immediate churn, but do not consume the new blob as "seen".
      if (dw < 0.02 && dh < 0.02 && emittedVeryRecently) {
        return;
      }
    }

    window.__frankLastBlob = blobSrc;
    window.__frankLastRect = { w: w, h: h };
    window.__frankLastEmitAt = now;
    window.__frankLastEmitBlob = blobSrc;
    window.__frankPageCounter++;

    var isSpread = w > h * $spreadThreshold;
    var pageMode = isSpread ? 'spread' : 'single';
    var pageId = 'kindle-' + window.__frankSessionId + '-' + window.__frankPageCounter +
      (isSpread ? '-spread' : '');

    // Try to extract stable page number from the DOM
    var kindlePage = '';
    var pi = document.querySelector(
      '#kr-page-indicator, .page-number, [class*="pageNum"], [class*="page-count"], ' +
      '[class*="location"], [data-cfi], .cfi-marker'
    );
    if (pi) kindlePage = pi.textContent.trim().substring(0, 30);
    // Also try the progress bar / slider
    if (!kindlePage) {
      var slider = document.querySelector('input[type="range"], [role="slider"]');
      if (slider) kindlePage = 'pos:' + (slider.value || slider.getAttribute('aria-valuenow') || '');
    }

    window.flutter_inappwebview.callHandler('onPageDetected', {
      pageId: pageId,
      index: window.__frankPageCounter,
      type: 'dom',
      pageMode: pageMode,
      navIntent: window.__frankNavIntent || 'forward',
      imgSrc: blobSrc,
      naturalWidth: target.naturalWidth,
      naturalHeight: target.naturalHeight,
      readerRect: { x: rect.x, y: rect.y, width: w, height: h },
      devicePixelRatio: window.devicePixelRatio || 1,
      kindlePage: kindlePage,
    });
  }

  setInterval(detectPageChange, 1000);
  document.addEventListener('click', function(e) {
    // Kindle manga is RTL: clicks on left half usually mean "next page".
    if (e && typeof e.clientX === 'number') {
      window.__frankNavIntent = (e.clientX <= (window.innerWidth / 2))
        ? 'forward' : 'backward';
    }
    window.__frankUserNavAt = Date.now();
    setTimeout(detectPageChange, 500);
  });
  document.addEventListener('pointerdown', function() {
    window.__frankUserNavAt = Date.now();
  }, true);
  document.addEventListener('mousedown', function() {
    window.__frankUserNavAt = Date.now();
  }, true);
  document.addEventListener('touchstart', function() {
    window.__frankUserNavAt = Date.now();
  }, true);
  document.addEventListener('wheel', function() {
    window.__frankUserNavAt = Date.now();
  }, { passive: true });
  document.addEventListener('keydown', function(e) {
    if (!e || !e.key) return;
    if (e.key === 'ArrowLeft') window.__frankNavIntent = 'forward';
    else if (e.key === 'ArrowRight') window.__frankNavIntent = 'backward';
    window.__frankUserNavAt = Date.now();
  });
  document.addEventListener('keyup', function() {
    window.__frankUserNavAt = Date.now();
    setTimeout(detectPageChange, 500);
  });
  window.addEventListener('resize', function() {
    // Kindle re-renders on resize — force re-detection after it settles
    window.__frankLastBlob = null;
    window.__frankUserNavAt = Date.now();
    setTimeout(detectPageChange, 1000);
  });

  console.log('[Frank] Kindle detection script injected (DOM blob tracking)');
})();
''';

  @override
  String captureScript(String pageId) {
    return '''
(function() {
$_findVisibleBlobFn
  var target = __frankFindVisibleBlob();
  if (!target) return null;
  var r = target.getBoundingClientRect();
  return JSON.stringify({
    x: r.left,
    y: r.top,
    width: r.width,
    height: r.height,
    devicePixelRatio: window.devicePixelRatio || 1,
    pageMode: (r.width > r.height * $spreadThreshold) ? 'spread' : 'single',
  });
})();
''';
  }

  /// JS that extracts the visible blob img as a base64 PNG data URL.
  /// Synchronous — draws the img onto a canvas and calls toDataURL().
  static String get captureCurrentPageScript =>
      '''
(function() {
$_findVisibleBlobFn
  var target = __frankFindVisibleBlob();
  if (!target) return null;

  var c = document.createElement('canvas');
  c.width = target.naturalWidth;
  c.height = target.naturalHeight;
  c.getContext('2d').drawImage(target, 0, 0);
  try {
    return c.toDataURL('image/png');
  } catch(e) {
    return null;
  }
})();
''';

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

  /// JS that scans the Kindle reader DOM for visual elements (canvas, img, iframe)
  /// on each page change. Reports extractability, sizes, and structure via
  /// onKindleDomExplore handler. Re-scans on MutationObserver + 1s interval.
  static String get domExplorerScript => '''
(function() {
  if (window.__frankDomExplorer) return;
  window.__frankDomExplorer = true;
  var lastSummary = '';

  function scanDom() {
    var elements = [];
    var iframes = [];

    // Scan all canvas elements
    document.querySelectorAll('canvas').forEach(function(c) {
      var r = c.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) return;
      var info = {
        tag: 'CANVAS',
        id: c.id || '',
        classes: c.className || '',
        rect: { x: r.x, y: r.y, width: r.width, height: r.height },
        pixelSize: c.width + 'x' + c.height,
        extractable: false,
        dataUrlSize: 0,
        error: null
      };
      try {
        var data = c.toDataURL('image/png');
        info.extractable = true;
        info.dataUrlSize = data.length;
      } catch(e) {
        info.error = e.name + ': ' + e.message;
      }
      elements.push(info);
    });

    // Scan all img elements in the reader area
    var readerArea = document.querySelector(
      '#kr-renderer, #kindle-reader-content, .reader-content'
    ) || document.body;
    readerArea.querySelectorAll('img').forEach(function(img) {
      var r = img.getBoundingClientRect();
      if (r.width < 10 || r.height < 10) return;
      var info = {
        tag: 'IMG',
        id: img.id || '',
        src: img.src ? img.src.substring(0, 200) : '',
        srcType: 'unknown',
        rect: { x: r.x, y: r.y, width: r.width, height: r.height },
        naturalSize: img.naturalWidth + 'x' + img.naturalHeight,
        fetchable: null
      };
      if (img.src) {
        if (img.src.startsWith('data:')) info.srcType = 'data-uri';
        else if (img.src.startsWith('blob:')) info.srcType = 'blob';
        else if (img.src.startsWith('http')) info.srcType = 'http';
      }
      elements.push(info);
    });

    // Scan iframes
    document.querySelectorAll('iframe').forEach(function(f) {
      var r = f.getBoundingClientRect();
      if (r.width < 10 || r.height < 10) return;
      var sameOrigin = false;
      try { var _ = f.contentDocument; sameOrigin = true; } catch(e) {}
      iframes.push({
        src: f.src ? f.src.substring(0, 200) : '',
        rect: { x: r.x, y: r.y, width: r.width, height: r.height },
        sameOrigin: sameOrigin
      });
    });

    // Page indicator
    var pageText = '';
    var pi = document.querySelector(
      '#kr-page-indicator, .page-number, [class*="pageNum"], [class*="page-count"]'
    );
    if (pi) pageText = pi.textContent.trim().substring(0, 30);

    var canvasCount = elements.filter(function(e) { return e.tag === 'CANVAS'; }).length;
    var extractable = elements.filter(function(e) { return e.tag === 'CANVAS' && e.extractable; }).length;
    var imgCount = elements.filter(function(e) { return e.tag === 'IMG'; }).length;
    var summary = canvasCount + ' canvas (' + extractable + ' extractable), ' + imgCount + ' images, ' + iframes.length + ' iframes';

    // Only report if something changed
    if (summary === lastSummary) return;
    lastSummary = summary;

    var report = {
      type: 'kindle_dom_explore',
      url: location.href,
      pageIndicator: pageText,
      elements: elements,
      iframes: iframes,
      summary: summary
    };

    window.flutter_inappwebview.callHandler('onKindleDomExplore', report);
    console.log('[Frank] DOM explore: ' + summary);
  }

  // Scan periodically
  setInterval(scanDom, 1000);

  // Scan on mutations in the reader area
  var target = document.querySelector(
    '#kr-renderer, #kindle-reader-content, .reader-content'
  ) || document.body;
  var observer = new MutationObserver(function() {
    setTimeout(scanDom, 200);
  });
  observer.observe(target, { childList: true, subtree: true });

  // Initial scan
  setTimeout(scanDom, 500);
  console.log('[Frank] Kindle DOM explorer injected');
})();
''';

  /// JS that performs a deep DOM scan of the Kindle reader area.
  /// Results are sent via onInspectorLog with type 'kindle_dom'.
  static String get diagnosticScript =>
      '''
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
