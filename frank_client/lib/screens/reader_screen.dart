import 'dart:convert';
import 'dart:io' show Platform;
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:http/http.dart' as http;
import '../providers/jobs_provider.dart';
import '../providers/settings_provider.dart';
import '../services/image_capture_service.dart';
import '../webview/dom_inspector.dart';
import '../webview/js_bridge.dart';
import '../webview/kindle_prefetch_manager.dart';
import '../webview/overlay_controller.dart';
import '../webview/platform/app_webview.dart';
import '../webview/platform/app_webview_controller.dart';
import '../webview/strategies/kindle_strategy.dart';

/// Anti-bot JS injected at document-start to mask WebView fingerprints.
/// Each override is wrapped in try-catch — some properties may be
/// non-configurable in certain WebKit/Chrome versions.
const String antiBotScript = '''
(function() {
  try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch(e) {}
  try { Object.defineProperty(navigator, 'plugins', {
    get: () => [
      { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
      { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
      { name: 'Native Client', filename: 'internal-nacl-plugin' },
    ]
  }); } catch(e) {}
  try { Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'ja'] }); } catch(e) {}
  if (!window.chrome) window.chrome = {};
  if (!window.chrome.runtime) window.chrome.runtime = {};
  try { Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 }); } catch(e) {}
  try { Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 }); } catch(e) {}
})();
''';

class ReaderScreen extends ConsumerStatefulWidget {
  final String initialUrl;

  const ReaderScreen({super.key, required this.initialUrl});

  @override
  ConsumerState<ReaderScreen> createState() => _ReaderScreenState();
}

class _ReaderScreenState extends ConsumerState<ReaderScreen> {
  AppWebViewController? _webController;
  final _jsBridge = JsBridge();
  final _inspector = DomInspector();
  final _overlay = OverlayController();
  final _capture = ImageCaptureService();

  String _currentUrl = '';
  final bool _inspectorMode = false;
  final bool _showOverlay = true;

  /// The pageId of the currently visible Kindle page (for overlay gating).
  String? _currentKindlePageId;

  /// The last page info detected for Kindle (for re-capture on pipeline change).
  Map<String, dynamic>? _lastKindlePageInfo;

  /// Kindle pageId -> blob src seen at detection time.
  final Map<String, String> _kindleBlobByPageId = {};
  Map<String, Object?> _kindlePrefetchState = const {};
  String _kindleNavIntent = 'forward';
  int _kindleOverlayOk = 0;
  int _kindleOverlayFail = 0;
  int _kindleOverlayFallback = 0;

  /// Selected pipeline for Kindle pages (furigana vs english translation).
  String _kindlePipeline = 'manga_furigana';

  /// Background webview prefetch manager (Linux-only).
  var _kindlePrefetch = KindlePrefetchManager();

  // --- Webtoon batching state ---
  static const _batchSize = 5;
  static const _prefetchThreshold = 2;

  /// All detected webtoon page infos, keyed by index.
  final Map<int, Map<String, dynamic>> _detectedWebtoonPages = {};

  /// Highest webtoon page index that has been submitted (inclusive).
  int _batchSubmittedUpTo = -1;

  /// Whether a batch submission is currently in progress.
  bool _batchInProgress = false;

  /// Track active listenManual subscriptions so we can cancel them.
  final Map<String, ProviderSubscription> _completionListeners = {};

  @override
  void dispose() {
    _kindlePrefetch.dispose();
    for (final sub in _completionListeners.values) {
      sub.close();
    }
    _completionListeners.clear();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Stack(
        children: [
          // WebView fills entire screen
          AppWebView(
            initialUrl: widget.initialUrl,
            userAgent:
                'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36',
            onWebViewCreated: (controller) {
              _webController = controller;
              _jsBridge.attach(controller);
              _inspector.attach(controller);
              _registerToolbarHandler(controller);
              _jsBridge.onPageDetected = _onPageDetected;
            },
            onLoadStop: (controller, url) {
              final urlStr = url ?? '';
              final prevUrl = _currentUrl;
              setState(() => _currentUrl = urlStr);
              final isKindleNow = urlStr.contains('read.amazon.co.jp');
              final preserveKindleSessionState =
                  isKindleNow && prevUrl == urlStr;

              debugPrint(
                '[Reader] onLoadStop url=$urlStr '
                '(preserveKindleSessionState=$preserveKindleSessionState)',
              );

              // Reset state on true page load/navigation. Kindle often emits
              // same-URL load stops while paging; avoid wiping runtime state.
              if (!preserveKindleSessionState) {
                _detectedWebtoonPages.clear();
                _batchSubmittedUpTo = -1;
                _batchInProgress = false;
                _currentKindlePageId = null;
                _kindleBlobByPageId.clear();
                _kindlePrefetchState = const {};
                _kindleNavIntent = 'forward';
                _kindleOverlayOk = 0;
                _kindleOverlayFail = 0;
                _kindleOverlayFallback = 0;
                // Cancel all completion listeners from previous page
                for (final sub in _completionListeners.values) {
                  sub.close();
                }
                _completionListeners.clear();
              }
              _jsBridge.onUrlChanged(controller, urlStr);
              _injectDesktopViewportFit(controller);
              // Sync toolbar button states after injection
              Future.delayed(const Duration(milliseconds: 500), () {
                _syncAutoButtonState();
                _syncPipelineButtonState();
              });
              if (_inspectorMode) {
                _inspector.inject(controller);
                _injectKindleDiagnosticIfNeeded(controller);
                _injectKindleDomExplorerIfNeeded(controller);
              }
              Future.delayed(const Duration(milliseconds: 700), () {
                if (!mounted) return;
                _pushKindleDebugHudToPage();
              });
            },
            onUpdateVisitedHistory: (controller, url, isReload) {
              final urlStr = url ?? '';
              setState(() => _currentUrl = urlStr);
              _jsBridge.onUrlChanged(controller, urlStr);
            },
          ),
        ],
      ),
    );
  }

  String _kindleDebugHudText() {
    final s = _kindlePrefetchState;
    final line1 =
        'kindle=${_currentKindlePageId ?? '-'} nav=$_kindleNavIntent dir=${s['direction'] ?? '-'}';
    final line2 =
        'init=${s['initialized'] ?? false} prefetching=${s['prefetching'] ?? false} pending=${s['pendingTopUp'] ?? false} note=${s['lastNote'] ?? '-'}';
    final line3 =
        'main=${s['mainPageIndex'] ?? '-'} start=${s['startIndex'] ?? '-'} pref=${s['prefetchedCount'] ?? '-'} ahead=${s['ahead'] ?? '-'}';
    final line4 =
        'overlay ok=$_kindleOverlayOk fail=$_kindleOverlayFail fallback=$_kindleOverlayFallback';
    return '$line1\n$line2\n$line3\n$line4';
  }

  void _pushKindleDebugHudToPage() {
    if (!kDebugMode) return;
    final isKindle =
        _jsBridge.activeStrategy?.siteName == 'kindle' ||
        _currentUrl.contains('read.amazon.co.jp');
    if (!isKindle) return;
    final controller = _webController;
    if (controller == null) return;
    final text = _kindleDebugHudText()
        .replaceAll('\\', '\\\\')
        .replaceAll("'", "\\'")
        .replaceAll('\n', '\\n');
    controller.evaluateJavascript(
      source:
          "if(window.__frankSetDebugHud) window.__frankSetDebugHud('$text', true);",
    );
  }

  Future<void> _copyKindleDebugHudToClipboard() async {
    final text = _kindleDebugHudText();
    await Clipboard.setData(ClipboardData(text: text));
    _updateInPageStatus('Debug copied');
    debugPrint('[Reader] Kindle debug copied');
  }

  /// Log a Kindle lifecycle event (only when inspector mode is active).
  void _logKindle(String event, Map<String, dynamic> data) {
    if (!_inspectorMode) return;
    _inspector.log({
      'type': event,
      'timestamp': DateTime.now().millisecondsSinceEpoch,
      ...data,
    });
  }

  void _onPageDetected(Map<String, dynamic> pageInfo) {
    final pageId = pageInfo['pageId'] as String?;
    if (pageId == null) return;

    // Track current Kindle page so overlays only apply to the visible page
    if (_jsBridge.activeStrategy?.siteName == 'kindle') {
      // Close stale listeners from previous Kindle pages
      if (_currentKindlePageId != null && _currentKindlePageId != pageId) {
        _closeStaleKindleListeners(keepPageId: pageId);
      }
      _currentKindlePageId = pageId;
      _lastKindlePageInfo = pageInfo;
      final blobSrc = pageInfo['imgSrc'] as String?;
      if (blobSrc != null && blobSrc.startsWith('blob:')) {
        _kindleBlobByPageId[pageId] = blobSrc;
      }
      // Trigger bg webview prefetch on Kindle page change
      final kindleIndex = (pageInfo['index'] as num?)?.toInt() ?? 0;
      final navIntent = pageInfo['navIntent'] as String?;
      final newIntent = navIntent == 'backward' ? 'backward' : 'forward';
      _kindleNavIntent = newIntent;
      debugPrint(
        '[Reader] kindle detect pageId=$pageId index=$kindleIndex nav=$newIntent',
      );
      _triggerKindlePrefetch(kindleIndex, newIntent);
      _pushKindleDebugHudToPage();
    }

    _logKindle('kindle_detect', {
      'pageId': pageId,
      'pageMode': pageInfo['pageMode'],
      'type': pageInfo['type'],
    });

    final settings = ref.read(settingsProvider);

    // For spread pages (Kindle), check both left and right sub-page jobs
    final pageMode = pageInfo['pageMode'] as String?;
    if (pageMode == 'spread') {
      if (!settings.autoTranslate) return;
      _handleSpreadDetection(pageId, pageInfo);
      return;
    }

    // Webtoon batching: store detected page and trigger batch if needed
    final index = (pageInfo['index'] as num?)?.toInt();
    if (index != null && pageId.startsWith('wt-')) {
      _detectedWebtoonPages[index] = pageInfo;
      final detected = _detectedWebtoonPages.length;
      final submitted = _batchSubmittedUpTo + 1;
      _updateInPageStatus('Page $pageId ($submitted/$detected queued)');

      if (!settings.autoTranslate) {
        _updateInPageStatus('Auto-translate OFF');
        return;
      }

      // First detection → submit initial batch
      // Or user scrolled close to batch boundary → submit next batch
      if (_batchSubmittedUpTo < 0 ||
          index >= _batchSubmittedUpTo - _prefetchThreshold) {
        _submitNextBatch();
      }
      return;
    }

    // Non-webtoon single page (Kindle single)
    if (!settings.autoTranslate) {
      _updateInPageStatus('Auto-translate OFF');
      return;
    }

    // Check if already submitted
    final jobs = ref.read(jobsProvider);
    if (jobs.containsKey(pageId)) {
      final job = jobs[pageId]!;
      if (job.isComplete && job.translatedImage != null && _showOverlay) {
        _applyOverlay(pageId, job.translatedImage!);
      }
      return;
    }

    // Capture and submit
    _capturePageImage(pageId, pageInfo);
  }

  /// Handle spread detection: check if L/R sub-pages are already processed.
  void _handleSpreadDetection(
    String spreadPageId,
    Map<String, dynamic> pageInfo,
  ) {
    // Spread pageId is like 'kindle-5-spread', sub-pages are 'kindle-5-spread-L' and 'kindle-5-spread-R'
    final leftId = '$spreadPageId-L';
    final rightId = '$spreadPageId-R';

    final jobs = ref.read(jobsProvider);

    // If both halves are already complete, apply overlays
    final leftJob = jobs[leftId];
    final rightJob = jobs[rightId];
    if (leftJob != null &&
        leftJob.isComplete &&
        leftJob.translatedImage != null &&
        rightJob != null &&
        rightJob.isComplete &&
        rightJob.translatedImage != null &&
        _showOverlay) {
      _applySpreadOverlay(
        spreadPageId,
        leftJob.translatedImage!,
        rightJob.translatedImage!,
      );
      return;
    }

    // If not yet submitted, capture and split
    if (!jobs.containsKey(leftId) && !jobs.containsKey(rightId)) {
      _capturePageImage(spreadPageId, pageInfo);
    }
  }

  Future<void> _capturePageImage(
    String pageId,
    Map<String, dynamic> pageInfo,
  ) async {
    final controller = _webController;
    if (controller == null) return;

    Uint8List? imageBytes;

    final type = pageInfo['type'] as String?;
    if (type == 'dom') {
      // Kindle DOM: extract visible blob img as base64 PNG via canvas
      final dataUrl = await controller.evaluateJavascript(
        source: KindleStrategy.captureCurrentPageScript,
      );
      if (dataUrl is String && dataUrl.startsWith('data:image/png;base64,')) {
        // Decode on background isolate — data URLs can be 4MB+
        final b64 = dataUrl.split(',')[1];
        imageBytes = await compute(base64Decode, b64);
      } else {
        // Fallback to screenshot if DOM extraction fails
        imageBytes = await _capture.takeScreenshot(controller);
      }

      _logKindle('kindle_capture', {
        'pageId': pageId,
        'pageMode': pageInfo['pageMode'],
        'captureType': dataUrl is String ? 'dom' : 'screenshot_fallback',
        'imageSize': imageBytes != null ? '${imageBytes.length} bytes' : null,
      });

      if (imageBytes == null) return;

      // Handle spread mode: split and submit two jobs
      final pageMode = pageInfo['pageMode'] as String?;
      if (pageMode == 'spread') {
        final halves = await ImageCaptureService.splitSpreadAsync(imageBytes);
        if (halves == null) return;

        final leftId = '$pageId-L';
        final rightId = '$pageId-R';

        _logKindle('kindle_split', {
          'spreadPageId': pageId,
          'leftSize': '${halves.$1.length} bytes',
          'rightSize': '${halves.$2.length} bytes',
        });

        final meta = _jsBridge.parseCurrentUrl(_currentUrl);

        // Submit left and right halves as separate jobs.
        // Kindle pages do NOT pass pageNumber — the DOM page indicator is
        // unreliable (often picks up JS code or stays static across pages).
        // Hash-based cache handles re-visits correctly.
        final spreadPipeline = _jsBridge.activeStrategy?.siteName == 'kindle'
            ? _kindlePipeline
            : _jsBridge.activeStrategy?.defaultPipeline;
        await ref
            .read(jobsProvider.notifier)
            .submitPage(
              pageId: leftId,
              imageBytes: halves.$1,
              pipeline: spreadPipeline,
              title: meta?.title,
              chapter: meta?.chapter,
              sourceUrl: _currentUrl,
            );
        await ref
            .read(jobsProvider.notifier)
            .submitPage(
              pageId: rightId,
              imageBytes: halves.$2,
              pipeline: spreadPipeline,
              title: meta?.title,
              chapter: meta?.chapter,
              sourceUrl: _currentUrl,
            );

        _watchForSpreadCompletion(pageId, leftId, rightId);
        return;
      }
    } else if (type == 'screenshot') {
      // Legacy screenshot fallback
      imageBytes = await _capture.takeScreenshot(controller);
    } else {
      // Webtoon: download image directly from src URL
      final src = pageInfo['src'] as String?;
      if (src != null && src.isNotEmpty) {
        try {
          final response = await http.get(
            Uri.parse(src),
            headers: {'Referer': _currentUrl},
          );
          if (response.statusCode == 200 && response.bodyBytes.isNotEmpty) {
            imageBytes = response.bodyBytes;
          } else {
            debugPrint(
              '[Reader] Download failed $pageId: HTTP ${response.statusCode}',
            );
          }
        } catch (e) {
          debugPrint('[Reader] Download error $pageId: $e');
        }
      }
    }

    if (imageBytes == null || imageBytes.isEmpty) {
      debugPrint('[Reader] No image captured for $pageId');
      return;
    }

    // Use site-specific pipeline: Kindle uses _kindlePipeline toggle,
    // webtoon uses its own default, others fall through to user setting.
    final pipeline = _jsBridge.activeStrategy?.siteName == 'kindle'
        ? _kindlePipeline
        : _jsBridge.activeStrategy?.defaultPipeline;
    _updateInPageStatus('Submitting $pageId...');

    // Extract metadata from URL
    final meta = _jsBridge.parseCurrentUrl(_currentUrl);

    // Kindle: skip pageNumber — DOM text is unreliable, hash cache handles re-visits.
    // Webtoon: use image index. Others: URL-derived page number.
    final isKindle = _jsBridge.activeStrategy?.siteName == 'kindle';
    final pageNumber = isKindle
        ? null
        : (pageInfo['index']?.toString() ?? meta?.pageNumber);
    await ref
        .read(jobsProvider.notifier)
        .submitPage(
          pageId: pageId,
          imageBytes: imageBytes,
          pipeline: pipeline,
          title: meta?.title,
          chapter: meta?.chapter,
          pageNumber: pageNumber,
          sourceUrl: _currentUrl,
        );

    _updateInPageStatus('Queued $pageId');

    // Watch for completion to apply overlay
    _watchForCompletion(pageId);
  }

  /// Submit the next batch of webtoon pages (up to _batchSize).
  /// Downloads and submits pages in parallel for faster throughput.
  Future<void> _submitNextBatch() async {
    if (_batchInProgress) return;
    _batchInProgress = true;

    try {
      // Find the next pages to submit (sorted by index)
      final sortedIndices = _detectedWebtoonPages.keys.toList()..sort();
      final jobs = ref.read(jobsProvider);

      final toSubmit = <int>[];
      for (final idx in sortedIndices) {
        if (idx <= _batchSubmittedUpTo) continue;
        final pageId = 'wt-$idx';
        if (jobs.containsKey(pageId)) continue;
        toSubmit.add(idx);
        if (toSubmit.length >= _batchSize) break;
      }

      if (toSubmit.isEmpty) return;

      debugPrint(
        '[Batch] Submitting ${toSubmit.length} pages: $toSubmit '
        '(detected=${_detectedWebtoonPages.length}, submitted=${_batchSubmittedUpTo + 1})',
      );
      _updateInPageStatus('Batch: submitting ${toSubmit.length} pages...');

      // Submit all pages in parallel for faster throughput
      await Future.wait(
        toSubmit.map((idx) async {
          final pageInfo = _detectedWebtoonPages[idx]!;
          final pageId = pageInfo['pageId'] as String;
          try {
            await _capturePageImage(pageId, pageInfo);
          } catch (e) {
            debugPrint('[Reader] Failed to capture $pageId: $e');
          }
        }),
      );

      // Update batch watermark to highest submitted index
      final maxSubmitted = toSubmit.reduce((a, b) => a > b ? a : b);
      if (maxSubmitted > _batchSubmittedUpTo) {
        _batchSubmittedUpTo = maxSubmitted;
      }

      final total = _detectedWebtoonPages.length;
      final done = _batchSubmittedUpTo + 1;
      _updateInPageStatus('Queued $done/$total pages');
    } finally {
      _batchInProgress = false;
      // Check if more pages are waiting — schedule next batch
      final sortedIndices = _detectedWebtoonPages.keys.toList()..sort();
      final hasMore = sortedIndices.any(
        (idx) =>
            idx > _batchSubmittedUpTo &&
            !ref.read(jobsProvider).containsKey('wt-$idx'),
      );
      if (hasMore) {
        debugPrint('[Batch] More pages pending, scheduling next batch');
        Future.delayed(const Duration(milliseconds: 100), () {
          if (mounted) _submitNextBatch();
        });
      }
    }
  }

  void _watchForCompletion(String pageId) {
    // Don't add duplicate listeners
    if (_completionListeners.containsKey(pageId)) return;

    // If already complete (e.g., cache hit inside submitPage), apply immediately
    final existingJob = ref.read(jobsProvider)[pageId];
    if (existingJob != null &&
        existingJob.isComplete &&
        existingJob.translatedImage != null) {
      _updateInPageStatus('$pageId done (cached)!');
      // For Kindle, only overlay if the user is still viewing this page
      final isKindle = _jsBridge.activeStrategy?.siteName == 'kindle';
      if (_showOverlay && (!isKindle || pageId == _currentKindlePageId)) {
        _applyOverlay(pageId, existingJob.translatedImage!);
      }
      return;
    }

    final sub = ref.listenManual(jobsProvider, (previous, next) {
      final job = next[pageId];
      if (job == null) return;
      final prevJob = previous?[pageId];
      if (prevJob?.status != job.status) {
        _updateInPageStatus('$pageId: ${job.status.name}');
      }
      if (job.isComplete && job.translatedImage != null) {
        _updateInPageStatus('$pageId done!');
        // Cancel this listener — job is done
        _completionListeners[pageId]?.close();
        _completionListeners.remove(pageId);
        if (_showOverlay) {
          // For Kindle, only overlay if the user is still viewing this page
          if (_jsBridge.activeStrategy?.siteName == 'kindle' &&
              pageId != _currentKindlePageId) {
            debugPrint(
              '[Reader] Skipping overlay for $pageId — user moved to $_currentKindlePageId',
            );
          } else {
            _applyOverlay(pageId, job.translatedImage!);
          }
        }
      } else if (job.isFailed) {
        _updateInPageStatus('$pageId failed');
        _completionListeners[pageId]?.close();
        _completionListeners.remove(pageId);
      }
    });
    _completionListeners[pageId] = sub;
  }

  /// Watch for both halves of a spread to complete, then apply overlay.
  void _watchForSpreadCompletion(
    String spreadPageId,
    String leftId,
    String rightId,
  ) {
    // Check if both halves are already complete (e.g. cache hits from resize)
    final jobs = ref.read(jobsProvider);
    final leftNow = jobs[leftId];
    final rightNow = jobs[rightId];
    if (leftNow != null &&
        leftNow.isComplete &&
        leftNow.translatedImage != null &&
        rightNow != null &&
        rightNow.isComplete &&
        rightNow.translatedImage != null) {
      if (_showOverlay && spreadPageId == _currentKindlePageId) {
        _applySpreadOverlay(
          spreadPageId,
          leftNow.translatedImage!,
          rightNow.translatedImage!,
        );
      }
      return;
    }

    final sub = ref.listenManual(jobsProvider, (previous, next) {
      final leftJob = next[leftId];
      final rightJob = next[rightId];
      if (leftJob == null || rightJob == null) return;

      // User navigated away — self-close to prevent O(n²) listener buildup
      if (spreadPageId != _currentKindlePageId) {
        _completionListeners[spreadPageId]?.close();
        _completionListeners.remove(spreadPageId);
        return;
      }

      if (leftJob.isComplete &&
          leftJob.translatedImage != null &&
          rightJob.isComplete &&
          rightJob.translatedImage != null &&
          _showOverlay) {
        _completionListeners[spreadPageId]?.close();
        _completionListeners.remove(spreadPageId);
        _applySpreadOverlay(
          spreadPageId,
          leftJob.translatedImage!,
          rightJob.translatedImage!,
        );
      }
    });
    _completionListeners[spreadPageId] = sub;
  }

  Future<void> _applyOverlay(String pageId, Uint8List imageBytes) async {
    final controller = _webController;
    if (controller == null) return;

    if (_jsBridge.activeStrategy?.siteName == 'webtoon') {
      // Look up the original src URL from detected page info
      final index = int.tryParse(pageId.replaceFirst('wt-', ''));
      String? originalSrc;
      if (index != null) {
        originalSrc = _detectedWebtoonPages[index]?['src'] as String?;
      }
      if (originalSrc != null && originalSrc.isNotEmpty) {
        final ok = await _overlay.replaceImageBySrc(
          controller,
          originalSrc,
          imageBytes,
        );
        debugPrint('[Overlay] $pageId replace=${ok ? 'OK' : 'FAIL'}');
      } else {
        debugPrint(
          '[Overlay] $pageId no src found (detected: ${_detectedWebtoonPages.keys.toList()})',
        );
      }
    } else if (_jsBridge.activeStrategy?.siteName == 'kindle') {
      final expectedBlob = _kindleBlobByPageId[pageId];
      var ok = await _overlay.replaceVisibleKindlePage(
        controller,
        imageBytes,
        expectedBlobSrc: expectedBlob,
      );
      var usedFallback = false;
      if (!ok && pageId == _currentKindlePageId) {
        // Kindle may re-generate blob URLs before replacement; fallback only
        // for the still-visible page to avoid cross-page overlays.
        usedFallback = true;
        ok = await _overlay.replaceVisibleKindlePage(controller, imageBytes);
      }
      if (ok) {
        _kindleOverlayOk++;
      } else {
        _kindleOverlayFail++;
      }
      if (usedFallback) _kindleOverlayFallback++;
      _pushKindleDebugHudToPage();
      debugPrint('[Overlay] $pageId replace=${ok ? 'OK' : 'FAIL'}');
      _logKindle('kindle_overlay', {
        'pageId': pageId,
        'expectedBlob': expectedBlob,
        'success': ok,
      });
    }
  }

  /// Apply overlay for a 2-page spread: stitch halves and DOM-replace.
  Future<void> _applySpreadOverlay(
    String spreadPageId,
    Uint8List leftImage,
    Uint8List rightImage,
  ) async {
    final controller = _webController;
    if (controller == null) return;

    final stitched = await ImageCaptureService.stitchSpreadAsync(
      leftImage,
      rightImage,
    );
    if (stitched == null) return;

    final expectedBlob = _kindleBlobByPageId[spreadPageId];
    var ok = await _overlay.replaceVisibleKindlePage(
      controller,
      stitched,
      expectedBlobSrc: expectedBlob,
    );
    var usedFallback = false;
    if (!ok && spreadPageId == _currentKindlePageId) {
      usedFallback = true;
      ok = await _overlay.replaceVisibleKindlePage(controller, stitched);
    }
    if (ok) {
      _kindleOverlayOk++;
    } else {
      _kindleOverlayFail++;
    }
    if (usedFallback) _kindleOverlayFallback++;
    _pushKindleDebugHudToPage();
    debugPrint('[Overlay] spread $spreadPageId replace=${ok ? 'OK' : 'FAIL'}');
    _logKindle('kindle_spread_overlay', {
      'spreadPageId': spreadPageId,
      'stitchedSize': '${stitched.length} bytes',
      'expectedBlob': expectedBlob,
      'success': ok,
    });
  }

  Future<void> _captureAndTranslate() async {
    final controller = _webController;
    if (controller == null) return;

    _updateInPageStatus('Capturing screenshot...');

    // Take a full screenshot for manual translation
    final imageBytes = await _capture.takeScreenshot(controller);
    if (imageBytes == null) {
      _updateInPageStatus('Capture failed');
      return;
    }

    final meta = _jsBridge.parseCurrentUrl(_currentUrl);
    final pageId = 'manual-${DateTime.now().millisecondsSinceEpoch}';

    _updateInPageStatus('Submitting...');

    final pipeline = _jsBridge.activeStrategy?.siteName == 'kindle'
        ? _kindlePipeline
        : _jsBridge.activeStrategy?.defaultPipeline;

    await ref
        .read(jobsProvider.notifier)
        .submitPage(
          pageId: pageId,
          imageBytes: imageBytes,
          pipeline: pipeline,
          title: meta?.title,
          chapter: meta?.chapter,
          pageNumber: meta?.pageNumber,
          sourceUrl: _currentUrl,
        );

    _updateInPageStatus('Queued $pageId');
    _watchForCompletion(pageId);
  }

  void _injectKindleDiagnosticIfNeeded(AppWebViewController controller) {
    if (_jsBridge.activeStrategy?.siteName == 'kindle') {
      controller.evaluateJavascript(source: KindleStrategy.diagnosticScript);
    }
  }

  /// Inject Kindle DOM explorer JS (debug-only, inspector mode).
  void _injectKindleDomExplorerIfNeeded(AppWebViewController controller) {
    if (_jsBridge.activeStrategy?.siteName == 'kindle') {
      controller.evaluateJavascript(source: KindleStrategy.domExplorerScript);
    }
  }

  /// Inject CSS that caps image width on wide landscape viewports,
  /// and an in-page floating Kindle control bar.
  void _injectDesktopViewportFit(AppWebViewController controller) {
    controller.evaluateJavascript(
      source: '''
(function() {
  if (window.__frankViewportFit) return;
  window.__frankViewportFit = true;

  /* --- Responsive image width --- */
  var style = document.createElement('style');
  style.id = '__frankViewportFit';
  document.head.appendChild(style);

  function updateLayout() {
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    if (vw > vh * 1.2) {
      var maxW = Math.round(vw / 3);
      style.textContent =
        'img.toon_image, #comic_view_area img, .wt_viewer img, #sectionContWide img {' +
        '  max-width: ' + maxW + 'px !important;' +
        '  width: auto !important; height: auto !important;' +
        '  display: block !important;' +
        '  margin-left: auto !important; margin-right: auto !important;' +
        '}';
    } else {
      style.textContent = '';
    }
  }
  updateLayout();
  window.addEventListener('resize', updateLayout);

  /* --- Floating toolbar --- */
  var toggle = document.createElement('button');
  toggle.id = '__frankBarToggle';
  toggle.textContent = '\\u2630';
  toggle.title = 'Show/Hide Frank controls';
  toggle.style.cssText =
    'position:fixed; top:8px; left:8px; z-index:1000000;' +
    'width:34px; height:34px; border:none; border-radius:8px;' +
    'background:rgba(30,30,30,0.72); color:#fff; cursor:pointer;' +
    'font:700 17px/34px sans-serif; text-align:center;' +
    'box-shadow:0 2px 8px rgba(0,0,0,0.35);' +
    'backdrop-filter: blur(2px); -webkit-backdrop-filter: blur(2px);';
  document.body.appendChild(toggle);

  var bar = document.createElement('div');
  bar.id = '__frankBar';
  bar.innerHTML =
    '<button id="__frankBack" title="Back">&#x2190;</button>' +
    '<button id="__frankAuto" title="Toggle auto-translate">Auto: ON</button>' +
    '<button id="__frankPipeline" title="Switch pipeline" style="display:none;"></button>' +
    '<button id="__frankTranslate" title="Translate visible pages">&#x1F30D; Translate</button>' +
    '<button id="__frankCopyDbg" title="Copy debug" style="display:none;">Copy Debug</button>' +
    '<span id="__frankStatus"></span>';
  bar.style.cssText =
    'position:fixed; top:8px; left:48px; z-index:999999;' +
    'display:flex; align-items:center; gap:6px;' +
    'background:rgba(30,30,30,0.85); color:#fff; padding:6px 10px;' +
    'border-radius:8px; font:13px/1.3 sans-serif; box-shadow:0 2px 8px rgba(0,0,0,0.4);' +
    'user-select:none; -webkit-user-select:none;';

  var btnStyle =
    'background:none; border:1px solid rgba(255,255,255,0.3); color:#fff;' +
    'border-radius:4px; padding:4px 8px; cursor:pointer; font:inherit;';

  document.body.appendChild(bar);

  /* --- Debug HUD (top-right, hidden unless enabled by Dart) --- */
  var dbg = document.createElement('pre');
  dbg.id = '__frankDebugHud';
  dbg.style.cssText =
    'position:fixed; top:8px; right:8px; z-index:999999;' +
    'display:none; pointer-events:none; white-space:pre-wrap;' +
    'background:rgba(0,0,0,0.72); color:#fff; padding:8px;' +
    'border-radius:6px; font:11px/1.35 monospace; max-width:48vw;';
  document.body.appendChild(dbg);

  var backBtn = document.getElementById('__frankBack');
  var autoBtn = document.getElementById('__frankAuto');
  var pipeBtn = document.getElementById('__frankPipeline');
  var transBtn = document.getElementById('__frankTranslate');
  var copyDbgBtn = document.getElementById('__frankCopyDbg');
  backBtn.style.cssText = btnStyle;
  autoBtn.style.cssText = btnStyle;
  pipeBtn.style.cssText = btnStyle + 'display:none;';
  transBtn.style.cssText = btnStyle;
  copyDbgBtn.style.cssText = btnStyle + 'display:none;';

  var collapsed = false;
  function setCollapsed(next) {
    collapsed = !!next;
    bar.style.display = collapsed ? 'none' : 'flex';
    toggle.textContent = collapsed ? '\\u25B6' : '\\u2630';
    toggle.title = collapsed ? 'Show Frank controls' : 'Hide Frank controls';
  }
  setCollapsed(false);

  toggle.addEventListener('click', function(e) {
    e.stopPropagation();
    setCollapsed(!collapsed);
  });

  backBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'back');
  });
  autoBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'toggle_auto');
  });
  pipeBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'toggle_pipeline');
  });
  transBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'translate');
  });
  copyDbgBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'copy_debug');
  });

  /* Global functions Dart can call */
  window.__frankSetStatus = function(text) {
    var el = document.getElementById('__frankStatus');
    if (el) el.textContent = text;
  };
  window.__frankSetAutoState = function(on) {
    var el = document.getElementById('__frankAuto');
    if (el) {
      el.textContent = 'Auto: ' + (on ? 'ON' : 'OFF');
      el.style.borderColor = on ? '#4caf50' : 'rgba(255,255,255,0.3)';
      el.style.color = on ? '#4caf50' : '#fff';
    }
  };
  window.__frankSetPipeline = function(label, visible) {
    var el = document.getElementById('__frankPipeline');
    if (el) {
      el.textContent = label;
      el.style.display = visible ? '' : 'none';
      el.style.borderColor = '#64b5f6';
      el.style.color = '#64b5f6';
    }
  };
  window.__frankSetDebugHud = function(text, visible) {
    var el = document.getElementById('__frankDebugHud');
    if (!el) return;
    el.textContent = text || '';
    el.style.display = visible ? 'block' : 'none';
    var btn = document.getElementById('__frankCopyDbg');
    if (btn) btn.style.display = visible ? '' : 'none';
  };
})();
''',
    );
  }

  /// Register the toolbar action handler on the WebView controller.
  void _registerToolbarHandler(AppWebViewController controller) {
    controller.addJavaScriptHandler(
      handlerName: 'onToolbarAction',
      callback: (args) {
        final action = args.isNotEmpty ? args[0] as String? : null;
        switch (action) {
          case 'back':
            Navigator.pop(context);
            break;
          case 'toggle_auto':
            _toggleAutoTranslate();
            break;
          case 'toggle_pipeline':
            _toggleKindlePipeline();
            break;
          case 'translate':
            _translateVisiblePages();
            break;
          case 'copy_debug':
            _copyKindleDebugHudToClipboard();
            break;
        }
        return null;
      },
    );

    // Kindle DOM explorer handler — logs element scan results.
    controller.addJavaScriptHandler(
      handlerName: 'onKindleDomExplore',
      callback: (args) {
        if (!_inspectorMode) return null;
        if (args.isEmpty) return null;
        final data = args[0] as Map<String, dynamic>?;
        if (data == null) return null;
        _inspector.log(data);
        return null;
      },
    );
  }

  /// Toggle auto-translate and update the in-page button state.
  void _toggleAutoTranslate() {
    final settings = ref.read(settingsProvider);
    final newValue = !settings.autoTranslate;
    ref
        .read(settingsProvider.notifier)
        .update(settings.copyWith(autoTranslate: newValue));
    _syncAutoButtonState();
    debugPrint('[Reader] Auto-translate toggled to $newValue');
    if (newValue) {
      _updateInPageStatus('Auto-translate ON');
      // Immediately start submitting detected pages
      if (_detectedWebtoonPages.isNotEmpty) {
        _submitNextBatch();
      }
    } else {
      _updateInPageStatus('Auto-translate OFF');
    }
  }

  /// Sync the in-page Auto button appearance with current settings.
  void _syncAutoButtonState() {
    final controller = _webController;
    if (controller == null) return;
    final on = ref.read(settingsProvider).autoTranslate;
    controller.evaluateJavascript(
      source: 'if(window.__frankSetAutoState) window.__frankSetAutoState($on);',
    );
  }

  /// Toggle Kindle pipeline between furigana and english translation.
  /// Cancels all active Kindle jobs and re-submits the current page.
  void _toggleKindlePipeline() {
    _kindlePipeline = _kindlePipeline == 'manga_furigana'
        ? 'manga_translate'
        : 'manga_furigana';
    _syncPipelineButtonState();
    debugPrint('[Reader] Pipeline switched to $_kindlePipeline');

    // Cancel all Kindle jobs and re-submit current page
    _cancelKindleJobs();
    if (ref.read(settingsProvider).autoTranslate &&
        _currentKindlePageId != null &&
        _lastKindlePageInfo != null) {
      _capturePageImage(_currentKindlePageId!, _lastKindlePageInfo!);
    }
  }

  /// Sync the in-page pipeline button with the current selection.
  void _syncPipelineButtonState() {
    final controller = _webController;
    if (controller == null) return;
    final isKindle = _jsBridge.activeStrategy?.siteName == 'kindle';
    final label = _kindlePipeline == 'manga_furigana' ? 'Furigana' : 'English';
    controller.evaluateJavascript(
      source:
          "if(window.__frankSetPipeline) window.__frankSetPipeline('$label', $isKindle);",
    );
  }

  /// Close completion listeners for Kindle pages the user is no longer viewing.
  void _closeStaleKindleListeners({String? keepPageId}) {
    final stale = _completionListeners.keys
        .where((id) => id.startsWith('kindle-') && id != keepPageId)
        .toList();
    for (final id in stale) {
      _completionListeners[id]?.close();
      _completionListeners.remove(id);
      _kindleBlobByPageId.remove(id);
    }
  }

  /// Cancel all active Kindle jobs, prefetch, and their completion listeners.
  void _cancelKindleJobs() {
    _kindlePrefetch.dispose();
    _kindlePrefetch = KindlePrefetchManager();
    _kindleBlobByPageId.clear();
    final jobs = ref.read(jobsProvider);
    final notifier = ref.read(jobsProvider.notifier);
    final kindlePageIds = jobs.keys
        .where((id) => id.startsWith('kindle-'))
        .toList();
    for (final pageId in kindlePageIds) {
      _completionListeners[pageId]?.close();
      _completionListeners.remove(pageId);
      notifier.removeJob(pageId);
    }
    if (kindlePageIds.isNotEmpty) {
      debugPrint('[Reader] Cancelled ${kindlePageIds.length} Kindle jobs');
    }
  }

  /// Trigger Kindle prefetch via background webview (Linux-only).
  /// On non-Linux platforms, this is a no-op.
  void _triggerKindlePrefetch(int pageIndex, String navIntent) {
    if (!Platform.isLinux) return;
    if (_jsBridge.activeStrategy?.siteName != 'kindle') return;
    if (!ref.read(settingsProvider).autoTranslate) return;
    final direction = navIntent == 'backward' ? 'backward' : 'forward';

    // Lazy-init the bg webview prefetch manager.
    _bindKindlePrefetchCallbacks();
    if (!_kindlePrefetch.initialized && !_kindlePrefetch.disposed) {
      _kindlePrefetch.init(_currentUrl).then((_) {
        if (!mounted || _kindlePrefetch.disposed) return;
        _kindlePrefetch.onMainPageChanged(
          pageIndex,
          direction: direction,
          syncUrl: _currentUrl,
        );
      });
      return;
    }

    _kindlePrefetch.onMainPageChanged(
      pageIndex,
      direction: direction,
      syncUrl: _currentUrl,
    );
  }

  void _bindKindlePrefetchCallbacks() {
    _kindlePrefetch.onCaptured ??= _handleBgPrefetchedPage;
    _kindlePrefetch.onStateChanged = (state) {
      if (!mounted || !kDebugMode) return;
      _kindlePrefetchState = Map<String, Object?>.from(state);
      _pushKindleDebugHudToPage();
    };
  }

  /// Handle a page captured by the background webview prefetch manager.
  Future<void> _handleBgPrefetchedPage(
    Uint8List imageBytes,
    String pageMode,
  ) async {
    // Use a prefetch-specific pageId based on timestamp to avoid clashes.
    final ts = DateTime.now().millisecondsSinceEpoch;
    final prefetchId = 'kindle-pre-$ts';
    debugPrint(
      '[BgPrefetch] Received page (${imageBytes.length} bytes, $pageMode)',
    );

    // Check if we already have this image cached (by hash)
    final cache = ref.read(cacheServiceProvider);
    final hash = await cache.hashImage(imageBytes);
    final cached = await cache.lookupByHash(hash, _kindlePipeline);
    if (cached != null) {
      debugPrint('[BgPrefetch] Already cached (hash=${hash.substring(0, 12)})');
      return;
    }

    final meta = _jsBridge.parseCurrentUrl(_currentUrl);

    if (pageMode == 'spread') {
      final halves = await ImageCaptureService.splitSpreadAsync(imageBytes);
      if (halves == null) return;
      await ref
          .read(jobsProvider.notifier)
          .submitPage(
            pageId: '$prefetchId-L',
            imageBytes: halves.$1,
            pipeline: _kindlePipeline,
            title: meta?.title,
            chapter: meta?.chapter,
            sourceUrl: _currentUrl,
            priority: 'low',
          );
      await ref
          .read(jobsProvider.notifier)
          .submitPage(
            pageId: '$prefetchId-R',
            imageBytes: halves.$2,
            pipeline: _kindlePipeline,
            title: meta?.title,
            chapter: meta?.chapter,
            sourceUrl: _currentUrl,
            priority: 'low',
          );
    } else {
      await ref
          .read(jobsProvider.notifier)
          .submitPage(
            pageId: prefetchId,
            imageBytes: imageBytes,
            pipeline: _kindlePipeline,
            title: meta?.title,
            chapter: meta?.chapter,
            sourceUrl: _currentUrl,
            priority: 'low',
          );
    }
  }

  /// Manual translate: submit the next batch of detected pages.
  Future<void> _translateVisiblePages() async {
    if (_detectedWebtoonPages.isEmpty) {
      // Non-webtoon: fall back to screenshot capture
      _captureAndTranslate();
      return;
    }
    // Force-submit the next batch of webtoon pages
    _submitNextBatch();
  }

  /// Push a status message into the in-page toolbar.
  void _updateInPageStatus(String text) {
    final controller = _webController;
    if (controller == null) return;
    final escaped = text.replaceAll("'", "\\'").replaceAll('\n', ' ');
    controller.evaluateJavascript(
      source:
          "if(window.__frankSetStatus) window.__frankSetStatus('$escaped');",
    );
  }
}
