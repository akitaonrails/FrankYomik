import 'dart:async';
import 'dart:convert';
import 'dart:math';
import 'package:flutter/foundation.dart';
import 'platform/background_webview_controller.dart';
import 'strategies/kindle_strategy.dart';

/// Callback when a prefetched page image is captured from the bg webview.
typedef PrefetchCapturedCallback =
    void Function(Uint8List imageBytes, String pageMode);
typedef PrefetchStateChangedCallback =
    void Function(Map<String, Object?> state);

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
  bool _initializing = false;
  bool _pendingTopUp = false;
  final _rng = Random();
  String _direction = 'forward';

  /// Whether the bg webview has been initialized.
  bool initialized = false;

  /// Whether the manager has been disposed.
  bool disposed = false;

  /// Pages per prefetch batch.
  static const _batchSize = 3;
  static const _triggerThreshold = 2;
  static const _maxPagesAhead = 6;

  /// How many pages ahead have been captured since init/resync.
  int _prefetchedCount = 0;

  /// The user's current page index (from main webview detection).
  int _mainPageIndex = 0;

  /// The page index at which the bg webview was synced.
  int _startIndex = 0;

  /// Called when a prefetched page is captured.
  PrefetchCapturedCallback? onCaptured;

  /// Called when internal prefetch state changes (debug/telemetry).
  PrefetchStateChangedCallback? onStateChanged;
  String _lastNote = 'idle';
  int _capturedBatches = 0;

  /// Initialize the bg webview and load the same Kindle book URL.
  Future<void> init(String kindleUrl) async {
    if (initialized || disposed || _initializing) return;
    _initializing = true;
    _emitState('init_start');
    try {
      _bgController = BackgroundWebViewController();
      _bgController!.startListening(
        onLoadStop: (url) {
        },
      );

      await _bgController!.create(url: kindleUrl);
      initialized = true;
      _prefetchedCount = 0;
      _startIndex = _mainPageIndex; // Sync start index with user's current page
      debugPrint(
        '[BgPrefetch] Initialized with $kindleUrl '
        '(startIndex=$_startIndex, mainPage=$_mainPageIndex, dir=$_direction)',
      );

      // Wait for the first Kindle blob to become visible before prefetching.
      final deadline = DateTime.now().add(const Duration(seconds: 10));
      while (!disposed && DateTime.now().isBefore(deadline)) {
        final blob = await _getCurrentBlobUrl();
        if (blob != null) break;
        await Future.delayed(const Duration(milliseconds: 300));
      }

      // Prime an initial batch so users don't need to page ahead first.
      _prefetchNextBatch();
    } finally {
      _initializing = false;
      _emitState('init_done');
    }
  }

  /// Notify the manager that the user turned to a new page.
  /// Triggers a prefetch batch if the user is close to the edge.
  void onMainPageChanged(
    int pageIndex, {
    String direction = 'forward',
    String? syncUrl,
  }) {
    if (disposed) return;

    final normalizedDirection = direction == 'backward'
        ? 'backward'
        : 'forward';
    if (normalizedDirection != _direction) {
      _direction = normalizedDirection;
      _prefetchedCount = 0;
      _startIndex = pageIndex;
      _prefetching = false;
      debugPrint(
        '[BgPrefetch] Direction changed to $_direction; '
        'resetting prefetch window at page=$pageIndex',
      );
      _emitState('direction_$_direction');
      if (initialized && syncUrl != null && syncUrl.isNotEmpty) {
        unawaited(navigateTo(syncUrl));
        return;
      }
    }

    _mainPageIndex = pageIndex;

    final remaining = _prefetchedCount - (pageIndex - _startIndex);
    debugPrint(
      '[BgPrefetch] Page changed to $pageIndex '
      '(remaining=$remaining, prefetched=$_prefetchedCount, '
      'start=$_startIndex, dir=$_direction)',
    );
    if (remaining <= _triggerThreshold) {
      if (_prefetching) {
        _pendingTopUp = true;
        _emitState('topup_pending');
      } else {
        _prefetchNextBatch();
      }
    }
    _emitState('main_page_$pageIndex');
  }

  /// Navigate the bg webview to a new URL (e.g., after chapter jump).
  Future<void> navigateTo(String url) async {
    if (disposed || _bgController == null) return;
    _prefetchedCount = 0;
    _startIndex = _mainPageIndex;
    _prefetching = false;
    await _bgController!.create(url: url);
    final deadline = DateTime.now().add(const Duration(seconds: 10));
    while (!disposed && DateTime.now().isBefore(deadline)) {
      final blob = await _getCurrentBlobUrl();
      if (blob != null) break;
      await Future.delayed(const Duration(milliseconds: 300));
    }
    _emitState('resynced');
    _prefetchNextBatch();
  }

  Future<String?> _turnPageAndWaitForBlob(String? previousBlob) async {
    if (_bgController == null) return null;
    final key = _direction == 'backward'
        ? BackgroundWebViewController.gdkKeyRight
        : BackgroundWebViewController.gdkKeyLeft;

    // Retry once with the same key in case Kindle is still settling.
    for (var attempt = 0; attempt < 2; attempt++) {
      await _bgController!.sendKey(key);
      final changed = await _waitForBlobChange(previousBlob);
      if (changed != null) return changed;
      if (attempt == 0) {
        await Future.delayed(const Duration(milliseconds: 300));
      }
    }
    return null;
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
  Future<String?> _waitForBlobChange(
    String? previousBlob, {
    int timeoutMs = 5000,
  }) async {
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
    final aheadNow = _prefetchedCount - (_mainPageIndex - _startIndex);
    if (aheadNow >= _maxPagesAhead) return;
    _pendingTopUp = false;
    _prefetching = true;
    _capturedBatches++;
    _emitState('batch_start');

    debugPrint(
      '[BgPrefetch] Starting batch of $_batchSize pages '
      '(prefetched=$_prefetchedCount, main=$_mainPageIndex, '
      'start=$_startIndex)',
    );

    var madeProgress = false;
    try {
      for (var i = 0; i < _batchSize; i++) {
        if (disposed) break;

        final ahead = _prefetchedCount - (_mainPageIndex - _startIndex);
        if (ahead >= _maxPagesAhead) break;

        // Get current blob URL before turning.
        final previousBlob = await _getCurrentBlobUrl();

        // Add small randomized delay between turns to avoid bot-like cadence.
        final turnDelayMs = 550 + _rng.nextInt(451);
        await Future.delayed(Duration(milliseconds: turnDelayMs));

        // Turn page in current direction and wait for actual blob change.
        final newBlob = await _turnPageAndWaitForBlob(previousBlob);
        if (disposed) break;

        if (newBlob == null) {
          debugPrint(
            '[BgPrefetch] Blob unchanged after turn (dir=$_direction) — '
            'page turn may have failed or reached end of chapter',
          );
          _emitState('turn_unchanged');
          break;
        }

        // Small settle delay for Kindle to finish rendering.
        await Future.delayed(const Duration(milliseconds: 500));
        if (disposed) break;

        // Capture the current page from the bg webview.
        final dataUrl = await _bgController!.evaluateJavascript(
          source: KindleStrategy.captureCurrentPageScript,
        );

        if (dataUrl is String && dataUrl.startsWith('data:image/png;base64,')) {
          final b64 = dataUrl.split(',')[1];
          final imageBytes = await compute(base64Decode, b64);

          // Determine page mode from the bg webview.
          final modeJson = await _bgController!.evaluateJavascript(
            source: _pageModeScript,
          );
          final pageMode = (modeJson is String && modeJson == 'spread')
              ? 'spread'
              : 'single';

          _prefetchedCount++;
          madeProgress = true;
          _emitState('captured');
          debugPrint(
            '[BgPrefetch] Captured page +$_prefetchedCount '
            '(${imageBytes.length} bytes, $pageMode)',
          );

          onCaptured?.call(imageBytes, pageMode);
        } else {
          debugPrint(
            '[BgPrefetch] Capture returned null — '
            'may have reached end of chapter',
          );
          _emitState('capture_null');
          break;
        }
      }
    } catch (e) {
      debugPrint('[BgPrefetch] Error during prefetch: $e');
      _emitState('batch_error');
    } finally {
      _prefetching = false;
      _emitState(madeProgress ? 'batch_done' : 'batch_stalled');
      if (!disposed && initialized && _pendingTopUp) {
        _pendingTopUp = false;
        Future.delayed(const Duration(milliseconds: 200), _prefetchNextBatch);
      }
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
  function overlapAreaInViewport(r) {
    var ox = Math.min(r.right, vw) - Math.max(r.left, 0);
    var oy = Math.min(r.bottom, vh) - Math.max(r.top, 0);
    if (ox <= 0 || oy <= 0) return 0;
    return ox * oy;
  }
  for (var i = 0; i < imgs.length; i++) {
    if (!imgs[i].src || !imgs[i].src.startsWith('blob:')) continue;
    var r = imgs[i].getBoundingClientRect();
    if (r.width < 100 || r.height < 100) continue;
    var area = overlapAreaInViewport(r);
    if (area < 2000) continue;
    if (area > bestArea) { bestArea = area; best = imgs[i]; }
  }
  return best ? best.src : null;
})();
''';

  @visibleForTesting
  static String get debugGetBlobUrlScript => _getBlobUrlScript;

  /// JS to check if the current visible page is a spread or single.
  static const _pageModeScript =
      '''
(function() {
  var imgs = document.querySelectorAll('img');
  var best = null;
  var bestArea = 0;
  var vw = window.innerWidth;
  var vh = window.innerHeight;
  function overlapAreaInViewport(r) {
    var ox = Math.min(r.right, vw) - Math.max(r.left, 0);
    var oy = Math.min(r.bottom, vh) - Math.max(r.top, 0);
    if (ox <= 0 || oy <= 0) return 0;
    return ox * oy;
  }
  for (var i = 0; i < imgs.length; i++) {
    if (!imgs[i].src || !imgs[i].src.startsWith('blob:')) continue;
    var r = imgs[i].getBoundingClientRect();
    if (r.width < 100 || r.height < 100) continue;
    var area = overlapAreaInViewport(r);
    if (area < 2000) continue;
    if (area > bestArea) { bestArea = area; best = imgs[i]; }
  }
  if (!best) return 'single';
  var r = best.getBoundingClientRect();
  return (r.width > r.height * ${KindleStrategy.spreadThreshold}) ? 'spread' : 'single';
})();
''';

  @visibleForTesting
  static String get debugPageModeScript => _pageModeScript;

  /// Destroy the bg webview and clean up.
  Future<void> dispose() async {
    disposed = true;
    _prefetching = false;
    _emitState('disposed');
    if (_bgController != null) {
      try {
        await _bgController!.destroy();
      } catch (e) {
        debugPrint('[BgPrefetch] Error destroying bg webview: $e');
      }
      _bgController = null;
    }
    initialized = false;
  }

  void _emitState(String note) {
    _lastNote = note;
    final ahead = _prefetchedCount - (_mainPageIndex - _startIndex);
    final state = <String, Object?>{
      'initialized': initialized,
      'disposed': disposed,
      'prefetching': _prefetching,
      'initializing': _initializing,
      'direction': _direction,
      'prefetchedCount': _prefetchedCount,
      'mainPageIndex': _mainPageIndex,
      'startIndex': _startIndex,
      'ahead': ahead,
      'remaining': ahead,
      'batchCount': _capturedBatches,
      'pendingTopUp': _pendingTopUp,
      'lastNote': _lastNote,
    };
    onStateChanged?.call(state);
  }
}
