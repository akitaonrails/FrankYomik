import 'base_strategy.dart';

/// Strategy for Naver Webtoon (Korean) — m.comic.naver.com / comic.naver.com.
///
/// Image selector: `img.toon_image` (same as the Python scraper).
/// URL pattern: /webtoon/detail?titleId=xxx&no=yyy
class NaverWebtoonStrategy extends SiteStrategy {
  @override
  String get siteName => 'webtoon';

  @override
  String get urlPattern => r'comic\.naver\.com';

  @override
  String? get defaultPipeline => 'webtoon';

  @override
  String get detectionScript => '''
(function() {
  if (window.__frankDetectorActive) return;
  window.__frankDetectorActive = true;
  window.__frankDetectedPages = new Set();
  window.__frankTotalPages = 0;

  function findPageImages() {
    // Primary selector used by Naver Webtoon viewer
    var imgs = document.querySelectorAll('img.toon_image');
    if (imgs.length > 0) return Array.from(imgs);

    // Fallback selectors for alternative layouts
    var selectors = [
      '#comic_view_area img',
      '.wt_viewer img',
      '#sectionContWide img',
    ];
    for (var i = 0; i < selectors.length; i++) {
      imgs = document.querySelectorAll(selectors[i]);
      if (imgs.length > 0) return Array.from(imgs);
    }

    // Last resort: large images
    return Array.from(document.querySelectorAll('img')).filter(
      function(img) { return img.naturalWidth > 600 && img.naturalHeight > 400; }
    );
  }

  function reportPage(img, index) {
    // Skip tiny/placeholder images (must be at least 200px wide and 200px tall)
    if (img.naturalWidth < 200 || img.naturalHeight < 200) return;
    img.dataset.frankIndex = String(index);
    var pageId = 'wt-' + index;
    if (window.__frankDetectedPages.has(pageId)) return;
    window.__frankDetectedPages.add(pageId);

    window.flutter_inappwebview.callHandler('onPageDetected', {
      pageId: pageId,
      index: index,
      src: img.src || img.dataset.src || '',
      width: img.naturalWidth,
      height: img.naturalHeight,
    });
  }

  // Eagerly report ALL images that have loaded (for prefetch)
  function reportAllLoaded() {
    var imgs = findPageImages();
    window.__frankTotalPages = imgs.length;
    imgs.forEach(function(img, index) {
      if (img.complete && img.naturalWidth > 0) {
        reportPage(img, index);
      } else {
        // Listen for lazy-load completion
        img.addEventListener('load', function() {
          reportPage(img, index);
        }, { once: true });
      }
    });
  }

  // Force-load images that are still lazy (have data-src but no src)
  // This enables prefetching images before the user scrolls to them
  function prefetchImages() {
    var imgs = findPageImages();
    var loadedCount = 0;
    for (var i = 0; i < imgs.length; i++) {
      if (imgs[i].complete && imgs[i].naturalWidth > 0) {
        loadedCount++;
        continue;
      }
      // Force load: copy data-src to src if available
      var lazySrc = imgs[i].dataset.src || imgs[i].getAttribute('data-lazy-src');
      if (lazySrc && !imgs[i].src) {
        imgs[i].src = lazySrc;
      }
    }
    console.log('[Frank] Prefetch: ' + loadedCount + '/' + imgs.length + ' loaded');
  }

  // Also use IntersectionObserver as backup for lazy images that need scroll
  var observer = new IntersectionObserver(function(entries) {
    entries.forEach(function(entry) {
      if (entry.isIntersecting && (entry.target.src || entry.target.dataset.src)) {
        var imgs = findPageImages();
        var idx = imgs.indexOf(entry.target);
        if (idx >= 0) reportPage(entry.target, idx);
      }
    });
  }, { threshold: 0.1 });

  function observeImages() {
    findPageImages().forEach(function(img) { observer.observe(img); });
  }

  // Kick off: report all already-loaded images and force-load lazy ones
  reportAllLoaded();
  prefetchImages();
  observeImages();

  // Re-scan periodically for lazy-loaded images
  setInterval(function() {
    reportAllLoaded();
    prefetchImages();
    observeImages();
  }, 2000);

  // Also observe DOM mutations for dynamically added images
  var mutationObs = new MutationObserver(function() {
    reportAllLoaded();
    observeImages();
  });
  mutationObs.observe(document.body, { childList: true, subtree: true });

  console.log('[Frank] Naver Webtoon detection script injected');
})();
''';

  @override
  String captureScript(String pageId) => '''
(async function() {
  var index = parseInt('$pageId'.replace('wt-', ''));

  var imgs = document.querySelectorAll('img.toon_image');
  if (!imgs.length) {
    var selectors = [
      '#comic_view_area img', '.wt_viewer img', '#sectionContWide img',
    ];
    for (var i = 0; i < selectors.length; i++) {
      var found = document.querySelectorAll(selectors[i]);
      if (found.length > 0) { imgs = found; break; }
    }
  }
  if (!imgs.length) {
    imgs = Array.from(document.querySelectorAll('img')).filter(
      function(img) { return img.naturalWidth > 600; }
    );
  }

  var img = imgs[index];
  if (!img) return null;

  var src = img.src || img.dataset.src;
  if (!src) return null;

  try {
    var resp = await fetch(src);
    var blob = await resp.blob();
    return new Promise(function(resolve) {
      var reader = new FileReader();
      reader.onload = function() { resolve(reader.result.split(',')[1]); };
      reader.readAsDataURL(blob);
    });
  } catch (e) {
    console.error('[Frank] Naver capture failed:', e);
    return null;
  }
})();
''';

  @override
  PageMetadata? parseUrl(String url) {
    // Naver Webtoon URL patterns:
    //   https://m.comic.naver.com/webtoon/detail?titleId=747269&no=297
    //   https://comic.naver.com/webtoon/detail?titleId=747269&no=297
    final uri = Uri.tryParse(url);
    if (uri == null) return null;

    final titleId = uri.queryParameters['titleId'];
    final episodeNo = uri.queryParameters['no'];

    if (titleId == null || titleId.isEmpty) return null;

    return PageMetadata(
      title: titleId,
      chapter: episodeNo ?? '0',
      pageNumber: '0', // Webtoons are single long pages
      sourceUrl: url,
    );
  }
}
