import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';
import 'package:flutter/foundation.dart';
import 'platform/background_webview_controller.dart';
import 'strategies/kindle_strategy.dart';

/// Callback when a prefetched page image is captured from the bg webview.
typedef PrefetchCapturedCallback = void Function(
    Uint8List imageBytes, String pageMode);

/// Orchestrates background Kindle page prefetching via a hidden WebKitGTK webview.
///
/// Uses trusted GDK key events (not JS dispatchEvent) to turn pages in the
/// bg webview, captures each page, and fires [onCaptured] so the caller can
/// submit low-priority translation jobs.
///
/// Rate limiting: waits for blob change after each turn (up to 5s timeout).
/// Batched: fetches [_batchSize] pages at a time, only triggers the next batch
/// when the user approaches the end of what's already prefetched.
class KindlePrefetchManager {
  BackgroundWebViewController? _bgController;
  bool _prefetching = false;

  /// Whether the bg webview has been initialized.
  bool initialized = false;

  /// Whether the manager has been disposed.
  bool disposed = false;

  /// Pages per prefetch batch.
  static const _batchSize = 3;

  /// How many pages ahead have been captured since init/resync.
  int _prefetchedCount = 0;

  /// The user's current page index (from main webview detection).
  int _mainPageIndex = 0;

  /// The page index at which the bg webview was synced.
  int _startIndex = 0;

  /// Called when a prefetched page is captured.
  PrefetchCapturedCallback? onCaptured;

  /// Initialize the bg webview and load the same Kindle book URL.
  Future<void> init(String kindleUrl) async {
    if (initialized || disposed) return;

    _bgController = BackgroundWebViewController();
    _bgController!.startListening(onLoadStop: (url) {
      debugPrint('[BgPrefetch] BG loaded: $url');
    });

    await _bgController!.create(url: kindleUrl);
    initialized = true;
    _prefetchedCount = 0;
    _startIndex = _mainPageIndex; // Sync start index with user's current page
    debugPrint('[BgPrefetch] Initialized with $kindleUrl '
        '(startIndex=$_startIndex, mainPage=$_mainPageIndex)');

    // Wait for the page to finish loading before capturing.
    await Future.delayed(const Duration(seconds: 5));
  }

  /// Notify the manager that the user turned to a new page.
  /// Triggers a prefetch batch if the user is close to the edge.
  void onMainPageChanged(int pageIndex) {
    if (disposed) return;
    _mainPageIndex = pageIndex;

    final remaining = _prefetchedCount - (pageIndex - _startIndex);
    debugPrint('[BgPrefetch] Page changed to $pageIndex '
        '(remaining=$remaining, prefetched=$_prefetchedCount, '
        'start=$_startIndex)');
    if (remaining <= 1) {
      _prefetchNextBatch();
    }
  }

  /// Navigate the bg webview to a new URL (e.g., after chapter jump).
  Future<void> navigateTo(String url) async {
    if (disposed || _bgController == null) return;
    _prefetchedCount = 0;
    _startIndex = _mainPageIndex;
    _prefetching = false;
    await _bgController!.create(url: url);
    await Future.delayed(const Duration(seconds: 5));
    debugPrint('[BgPrefetch] Resynced to $url (startIndex=$_startIndex)');
  }

  /// Get the current blob URL from the bg webview (null if none visible).
  Future<String?> _getCurrentBlobUrl() async {
    if (_bgController == null) return null;
    final result = await _bgController!.evaluateJavascript(
      source: _getBlobUrlScript,
    );
    if (result is String && result.startsWith('blob:')) {
      return result;
    }
    return null;
  }

  /// Wait for the blob URL to change after a key event, indicating the page
  /// has actually turned. Polls every 300ms up to [timeoutMs].
  /// Returns the new blob URL, or null if timed out.
  Future<String?> _waitForBlobChange(String? previousBlob,
      {int timeoutMs = 5000}) async {
    final deadline = DateTime.now().add(Duration(milliseconds: timeoutMs));
    while (DateTime.now().isBefore(deadline)) {
      if (disposed) return null;
      await Future.delayed(const Duration(milliseconds: 300));
      final current = await _getCurrentBlobUrl();
      if (current != null && current != previousBlob) {
        return current;
      }
    }
    return null; // Timed out — page didn't change
  }

  Future<void> _prefetchNextBatch() async {
    if (_prefetching || !initialized || disposed) return;
    _prefetching = true;

    debugPrint('[BgPrefetch] Starting batch of $_batchSize pages '
        '(prefetched=$_prefetchedCount, main=$_mainPageIndex, '
        'start=$_startIndex)');

    try {
      for (var i = 0; i < _batchSize; i++) {
        if (disposed) break;

        // Get current blob URL before turning.
        final previousBlob = await _getCurrentBlobUrl();

        // Turn to the next page via trusted GDK key event.
        await _bgController!.nextPage();

        // Wait for the blob URL to actually change (page rendered).
        final newBlob = await _waitForBlobChange(previousBlob);
        if (disposed) break;

        if (newBlob == null) {
          debugPrint('[BgPrefetch] Blob unchanged after key — '
              'page turn may have failed or reached end of chapter');
          break;
        }

        // Small settle delay for Kindle to finish rendering.
        await Future.delayed(const Duration(milliseconds: 500));
        if (disposed) break;

        // Capture the current page from the bg webview.
        final dataUrl = await _bgController!.evaluateJavascript(
          source: KindleStrategy.captureCurrentPageScript,
        );

        if (dataUrl is String &&
            dataUrl.startsWith('data:image/png;base64,')) {
          final b64 = dataUrl.split(',')[1];
          final imageBytes = await compute(base64Decode, b64);

          // Determine page mode from the bg webview.
          final modeJson = await _bgController!.evaluateJavascript(
            source: _pageModeScript,
          );
          final pageMode =
              (modeJson is String && modeJson == 'spread') ? 'spread' : 'single';

          _prefetchedCount++;
          debugPrint('[BgPrefetch] Captured page +$_prefetchedCount '
              '(${imageBytes.length} bytes, $pageMode)');

          onCaptured?.call(imageBytes, pageMode);
        } else {
          debugPrint('[BgPrefetch] Capture returned null — '
              'may have reached end of chapter');
          break;
        }
      }
    } catch (e) {
      debugPrint('[BgPrefetch] Error during prefetch: $e');
    } finally {
      _prefetching = false;
    }
  }

  /// JS to get the current visible blob img URL.
  static const _getBlobUrlScript = '''
(function() {
  var imgs = document.querySelectorAll('img');
  var best = null;
  var bestArea = 0;
  var vw = window.innerWidth;
  var vh = window.innerHeight;
  for (var i = 0; i < imgs.length; i++) {
    if (!imgs[i].src || !imgs[i].src.startsWith('blob:')) continue;
    var r = imgs[i].getBoundingClientRect();
    if (r.width < 100 || r.height < 100) continue;
    if (r.right < 0 || r.left > vw || r.bottom < 0 || r.top > vh) continue;
    var area = r.width * r.height;
    if (area > bestArea) { bestArea = area; best = imgs[i]; }
  }
  return best ? best.src : null;
})();
''';

  /// JS to check if the current visible page is a spread or single.
  static const _pageModeScript = '''
(function() {
  var imgs = document.querySelectorAll('img');
  var best = null;
  var bestArea = 0;
  var vw = window.innerWidth;
  var vh = window.innerHeight;
  for (var i = 0; i < imgs.length; i++) {
    if (!imgs[i].src || !imgs[i].src.startsWith('blob:')) continue;
    var r = imgs[i].getBoundingClientRect();
    if (r.width < 100 || r.height < 100) continue;
    if (r.right < 0 || r.left > vw || r.bottom < 0 || r.top > vh) continue;
    var area = r.width * r.height;
    if (area > bestArea) { bestArea = area; best = imgs[i]; }
  }
  if (!best) return 'single';
  var r = best.getBoundingClientRect();
  return (r.width > r.height * ${KindleStrategy.spreadThreshold}) ? 'spread' : 'single';
})();
''';

  /// Destroy the bg webview and clean up.
  Future<void> dispose() async {
    disposed = true;
    _prefetching = false;
    if (_bgController != null) {
      try {
        await _bgController!.destroy();
      } catch (e) {
        debugPrint('[BgPrefetch] Error destroying bg webview: $e');
      }
      _bgController = null;
    }
    initialized = false;
    debugPrint('[BgPrefetch] Disposed');
  }
}
