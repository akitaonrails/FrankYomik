import 'base_strategy.dart';

/// Strategy for webtoon.com — intercepts <img> tags via IntersectionObserver.
class WebtoonStrategy extends SiteStrategy {
  @override
  String get siteName => 'webtoon';

  @override
  String get urlPattern => r'webtoons?\.com';

  @override
  String get detectionScript => '''
(function() {
  if (window.__frankDetectorActive) return;
  window.__frankDetectorActive = true;
  window.__frankDetectedPages = new Set();

  // Possible selectors for webtoon page images
  const selectors = [
    'img._images',
    'img.comic-image',
    '#_imageList img',
    '.viewer-img img',
    '.toon_image',
  ];

  function findPageImages() {
    for (const sel of selectors) {
      const imgs = document.querySelectorAll(sel);
      if (imgs.length > 0) return Array.from(imgs);
    }
    // Fallback: large images in the main content area
    return Array.from(document.querySelectorAll('img')).filter(
      img => img.naturalWidth > 600 && img.naturalHeight > 400
    );
  }

  function reportPage(img, index) {
    const pageId = 'wt-' + index;
    if (window.__frankDetectedPages.has(pageId)) return;
    window.__frankDetectedPages.add(pageId);

    window.flutter_inappwebview.callHandler('onPageDetected', {
      pageId: pageId,
      index: index,
      src: img.src,
      width: img.naturalWidth,
      height: img.naturalHeight,
    });
  }

  // Use IntersectionObserver to detect when images scroll into view
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting && entry.target.src) {
        const imgs = findPageImages();
        const idx = imgs.indexOf(entry.target);
        if (idx >= 0) reportPage(entry.target, idx);
      }
    });
  }, { threshold: 0.1 });

  // Observe existing and future images
  function observeImages() {
    findPageImages().forEach(img => observer.observe(img));
  }

  observeImages();

  // Re-scan periodically for lazy-loaded images
  const rescanInterval = setInterval(() => {
    observeImages();
  }, 2000);

  // Also observe DOM mutations for dynamically added images
  const mutationObs = new MutationObserver(() => observeImages());
  mutationObs.observe(document.body, { childList: true, subtree: true });

  console.log('[Frank] Webtoon detection script injected');
})();
''';

  @override
  String captureScript(String pageId) => '''
(async function() {
  const index = parseInt('$pageId'.replace('wt-', ''));
  const selectors = [
    'img._images', 'img.comic-image', '#_imageList img',
    '.viewer-img img', '.toon_image',
  ];

  let imgs = [];
  for (const sel of selectors) {
    const found = document.querySelectorAll(sel);
    if (found.length > 0) { imgs = Array.from(found); break; }
  }
  if (!imgs.length) {
    imgs = Array.from(document.querySelectorAll('img')).filter(
      img => img.naturalWidth > 600
    );
  }

  const img = imgs[index];
  if (!img || !img.src) return null;

  try {
    const resp = await fetch(img.src);
    const blob = await resp.blob();
    return new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result.split(',')[1]);
      reader.readAsDataURL(blob);
    });
  } catch (e) {
    console.error('[Frank] Capture failed:', e);
    return null;
  }
})();
''';

  @override
  PageMetadata? parseUrl(String url) {
    // Webtoon URL patterns:
    // https://www.webtoons.com/en/action/tower-of-god/season-3-ep-297/viewer?title_no=95&episode_no=654
    final uri = Uri.tryParse(url);
    if (uri == null) return null;

    final segments = uri.pathSegments;
    // Look for pattern: /{lang}/{genre}/{title}/{episode}/viewer
    if (segments.length >= 4) {
      final title = segments.length >= 3 ? segments[2] : '';
      final episode = segments.length >= 4 ? segments[3] : '';
      // Extract episode number from slug
      final epMatch = RegExp(r'(\d+)').firstMatch(episode);
      final chapter = epMatch?.group(1) ?? episode;

      if (title.isNotEmpty && chapter.isNotEmpty) {
        return PageMetadata(
          title: title,
          chapter: chapter,
          pageNumber: '0', // Webtoons are single long pages
          sourceUrl: url,
        );
      }
    }
    return null;
  }
}
