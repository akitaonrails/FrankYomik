import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:http/http.dart' as http;
import '../providers/connection_provider.dart';
import '../providers/jobs_provider.dart';
import '../providers/settings_provider.dart';
import '../services/image_capture_service.dart';
import '../webview/dom_inspector.dart';
import '../webview/js_bridge.dart';
import '../webview/overlay_controller.dart';
import '../webview/platform/app_webview.dart';
import '../webview/platform/app_webview_controller.dart';
import '../webview/strategies/kindle_strategy.dart';
import '../webview/strategies/naver_webtoon_strategy.dart';
import 'package:shared_preferences/shared_preferences.dart';

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

class _ReaderScreenState extends ConsumerState<ReaderScreen>
    with WidgetsBindingObserver {
  AppWebViewController? _webController;
  final _jsBridge = JsBridge();
  final _inspector = DomInspector();
  final _overlay = OverlayController();
  final _capture = ImageCaptureService();

  String _currentUrl = '';
  String _lastLoadStopUrl = '';
  final bool _inspectorMode = false;
  final bool _showOverlay = true;

  /// The pageId of the currently visible Kindle page (for overlay gating).
  String? _currentKindlePageId;

  /// The last page info detected for Kindle (for re-capture on pipeline change).
  Map<String, dynamic>? _lastKindlePageInfo;

  /// Kindle pageId -> blob src seen at detection time.
  final Map<String, String> _kindleBlobByPageId = {};
  final Map<String, Map<String, num>> _kindleRectByPageId = {};
  final Map<String, List<Timer>> _kindleOverlayTimers = {};
  String _kindleNavIntent = 'forward';
  final bool _kindleDebugHudEnabled = false;
  final bool _kindleVerboseProbeLogs = false;
  int _kindleOverlayOk = 0;
  int _kindleOverlayFail = 0;
  int _kindleOverlayFallback = 0;

  /// Selected pipeline for Kindle pages (furigana vs english translation).
  /// Initialized from global settings in initState, overridden per-volume.
  late String _kindlePipeline;

  /// Current Kindle ASIN for per-title pipeline persistence.
  String? _currentAsin;

  /// Whether Kindle fit-width zoom mode is active (persisted per-ASIN).
  bool _kindleFitWidth = false;

  // --- Webtoon batching state ---
  static const _batchSize = 5;
  static const _prefetchThreshold = 2;

  /// All detected webtoon page infos, keyed by index.
  final Map<int, Map<String, dynamic>> _detectedWebtoonPages = {};

  /// Total webtoon images reported by JS (includes unloaded ones).
  int _webtoonTotalPages = 0;

  /// Webtoon page indices that were successfully captured and submitted.
  final Set<int> _submittedWebtoonIndices = {};

  /// Whether a batch submission is currently in progress.
  bool _batchInProgress = false;

  /// Track active listenManual subscriptions so we can cancel them.
  final Map<String, ProviderSubscription> _completionListeners = {};
  Timer? _statusClearTimer;
  Timer? _progressSyncTimer;
  int _statusMessageVersion = 0;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _kindlePipeline = ref.read(settingsProvider).pipeline;
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _statusClearTimer?.cancel();
    _progressSyncTimer?.cancel();
    _cancelKindleReapplies();
    for (final sub in _completionListeners.values) {
      sub.close();
    }
    _completionListeners.clear();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      debugPrint('[Reader] App resumed — reconnecting');
      ref.read(connectionProvider.notifier).connect();
      // Sync progress for in-flight webtoon jobs
      if (_jsBridge.activeStrategy?.siteName == 'webtoon') {
        _updateWebtoonProgress();
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        bottom: false,
        child: AppWebView(
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
            setState(() => _currentUrl = urlStr);
            final isKindleNow = urlStr.contains('read.amazon.co.jp');
            final preserveKindleSessionState =
                isKindleNow && _lastLoadStopUrl == urlStr;

            // Reset state on true page load/navigation. Kindle often emits
            // same-URL load stops while paging; avoid wiping runtime state.
            if (!preserveKindleSessionState) {
              _detectedWebtoonPages.clear();
              _submittedWebtoonIndices.clear();
              _webtoonTotalPages = 0;
              _batchInProgress = false;
              // Reset JS detection state so re-injection can re-detect pages
              controller.evaluateJavascript(
                source:
                    'window.__frankDetectorActive = false; '
                    'if(window.__frankDetectedPages) window.__frankDetectedPages.clear();',
              );
              _currentKindlePageId = null;
              _currentAsin = null;
              _kindleBlobByPageId.clear();
              _kindleRectByPageId.clear();
              _cancelKindleReapplies();
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
            _lastLoadStopUrl = urlStr;
            _jsBridge.onUrlChanged(controller, urlStr);
            _injectDesktopViewportFit(controller);
            // Sync toolbar button states after injection
            Future.delayed(const Duration(milliseconds: 500), () {
              _syncPipelineButtonState();
              _syncTranslateButtonState();
              _syncZoomButtonState();
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
      ),
    );
  }

  String _kindleDebugHudText() {
    final line1 = 'kindle=${_currentKindlePageId ?? '-'} nav=$_kindleNavIntent';
    final line2 =
        'overlay ok=$_kindleOverlayOk fail=$_kindleOverlayFail fallback=$_kindleOverlayFallback';
    return '$line1\n$line2';
  }

  void _pushKindleDebugHudToPage() {
    if (!kDebugMode) return;
    final controller = _webController;
    if (controller == null) return;
    final isKindle =
        _jsBridge.activeStrategy?.siteName == 'kindle' ||
        _currentUrl.contains('read.amazon.co.jp');
    if (!isKindle || !_kindleDebugHudEnabled) {
      controller.evaluateJavascript(
        source:
            "if(window.__frankSetDebugHud) window.__frankSetDebugHud('', false);",
      );
      return;
    }
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
    _updateInPageStatus('Debug copied', clearAfter: const Duration(seconds: 2));
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

  void _probeKindleOverlay({
    required String stage,
    required String pageId,
    String? expectedBlob,
    Map<String, num>? expectedRect,
    String? overlayToken,
  }) {
    if (!kDebugMode || !_kindleVerboseProbeLogs) return;
    final controller = _webController;
    if (controller == null) return;
    unawaited(
      _overlay
          .probeKindleOverlay(
            controller,
            expectedBlobSrc: expectedBlob,
            expectedRect: expectedRect,
            overlayToken: overlayToken,
          )
          .then((probe) {
            if (probe == null) return;
            final top = (probe['topAtCenter'] is Map)
                ? Map<String, dynamic>.from(probe['topAtCenter'] as Map)
                : const <String, dynamic>{};
            final candidates = (probe['candidates'] is List)
                ? List<dynamic>.from(probe['candidates'] as List)
                : const <dynamic>[];
            final sample = <String>[];
            for (var i = 0; i < candidates.length && i < 3; i++) {
              final c = candidates[i];
              if (c is! Map) continue;
              final m = Map<String, dynamic>.from(c);
              sample.add(
                'h=${m['topHits']} vis=${m['visible']} '
                'exp=${m['expectedMatch']} tok=${m['hasToken']} '
                'tr=${m['translated']} rect=${m['rect']}',
              );
            }
            debugPrint(
              '[OverlayProbe] $stage page=$pageId token=$overlayToken '
              'top=${top['tag'] ?? '-'}#${top['id'] ?? ''} '
              'cls=${top['cls'] ?? ''} cand=${candidates.length} '
              '${sample.join(' | ')}',
            );
          }),
    );
  }

  Future<void> _onPageDetected(Map<String, dynamic> pageInfo) async {
    final pageId = pageInfo['pageId'] as String?;
    if (pageId == null) return;

    // Track current Kindle page so overlays only apply to the visible page
    if (_jsBridge.activeStrategy?.siteName == 'kindle') {
      // Close stale listeners from previous Kindle pages
      if (_currentKindlePageId != null && _currentKindlePageId != pageId) {
        _closeStaleKindleListeners(keepPageId: pageId);
      }
      _cancelKindleReapplies(keepPageId: pageId);
      _currentKindlePageId = pageId;
      _lastKindlePageInfo = pageInfo;
      final blobSrc = pageInfo['imgSrc'] as String?;
      if (blobSrc != null && blobSrc.startsWith('blob:')) {
        _kindleBlobByPageId[pageId] = blobSrc;
      }
      final rect = pageInfo['readerRect'];
      if (rect is Map) {
        final x = (rect['x'] as num?)?.toDouble();
        final y = (rect['y'] as num?)?.toDouble();
        final width = (rect['width'] as num?)?.toDouble();
        final height = (rect['height'] as num?)?.toDouble();
        if (x != null && y != null && width != null && height != null) {
          _kindleRectByPageId[pageId] = {
            'x': x,
            'y': y,
            'width': width,
            'height': height,
          };
        }
      }
      final navIntent = pageInfo['navIntent'] as String?;
      final newIntent = navIntent == 'backward' ? 'backward' : 'forward';
      _kindleNavIntent = newIntent;
      if (kDebugMode) {
        final rr = _kindleRectByPageId[pageId];
        final rw = rr != null ? (rr['width']?.toStringAsFixed(0) ?? '-') : '-';
        final rh = rr != null ? (rr['height']?.toStringAsFixed(0) ?? '-') : '-';
        final dpr =
            (pageInfo['devicePixelRatio'] as num?)?.toStringAsFixed(2) ?? '-';
        debugPrint(
          '[KindleDetect] page=$pageId nav=$newIntent '
          'rect=${rw}x$rh dpr=$dpr',
        );
      }
      // Load per-title pipeline preference from ASIN (await so auto-translate
      // uses the correct pipeline on the first page of a returning visit).
      final meta = _jsBridge.parseCurrentUrl(_currentUrl);
      final asin = meta?.title;
      if (asin != null && asin.isNotEmpty && asin != _currentAsin) {
        _currentAsin = asin;
        final prefs = await SharedPreferences.getInstance();
        final saved = prefs.getString('kindle_pipeline_$asin');
        if (saved != null && saved.isNotEmpty && saved != _kindlePipeline) {
          setState(() {
            _kindlePipeline = saved;
          });
          _syncPipelineButtonState();
        }
        final savedZoom = prefs.getBool('kindle_zoom_$asin') ?? false;
        if (savedZoom != _kindleFitWidth) {
          _kindleFitWidth = savedZoom;
          _syncZoomButtonState();
        }
      }
      _pushKindleDebugHudToPage();
    }

    _logKindle('kindle_detect', {
      'pageId': pageId,
      'pageMode': pageInfo['pageMode'],
      'type': pageInfo['type'],
    });

    final settings = ref.read(settingsProvider);
    final autoOn = settings.isLoaded && settings.autoTranslate;
    _syncTranslateButtonState();
    _syncZoomButtonState();
    final isKindle = _jsBridge.activeStrategy?.siteName == 'kindle';
    if (isKindle && autoOn) {
      _showKindleSpinner();
    }

    // For spread pages (Kindle), check both left and right sub-page jobs
    final pageMode = pageInfo['pageMode'] as String?;
    if (pageMode == 'spread') {
      if (!autoOn) return;
      _handleSpreadDetection(pageId, pageInfo);
      return;
    }

    // Webtoon batching: store detected page and trigger batch if needed
    final index = (pageInfo['index'] as num?)?.toInt();
    if (index != null && pageId.startsWith('wt-')) {
      _detectedWebtoonPages[index] = pageInfo;
      final totalFromJs = (pageInfo['totalPages'] as num?)?.toInt() ?? 0;
      if (totalFromJs > _webtoonTotalPages) _webtoonTotalPages = totalFromJs;
      _updateWebtoonProgress();

      if (!autoOn) {
        _updateInPageStatus('Auto-translate OFF');
        return;
      }

      // First detection → submit initial batch
      // Or page not yet submitted and close to frontier → submit next batch
      if (_submittedWebtoonIndices.isEmpty ||
          (!_submittedWebtoonIndices.contains(index) &&
              index >=
                  (_submittedWebtoonIndices.isEmpty
                          ? 0
                          : _submittedWebtoonIndices.reduce(
                              (a, b) => a > b ? a : b,
                            )) -
                      _prefetchThreshold)) {
        _submitNextBatch();
      }
      return;
    }

    // Non-webtoon single page (Kindle single)
    if (!autoOn) {
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
      _hideKindleSpinner();
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

    final captureSw = Stopwatch()..start();
    Uint8List? imageBytes;
    String captureMode = 'unknown';

    final type = pageInfo['type'] as String?;
    if (type == 'dom') {
      captureMode = 'kindle_dom';
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
        captureMode = 'kindle_dom_fallback_screenshot';
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
      captureMode = 'screenshot';
      // Legacy screenshot fallback
      imageBytes = await _capture.takeScreenshot(controller);
    } else {
      captureMode = 'webtoon_js_or_http';
      // Webtoon: use JS fetch in the WebView context (has cookies + correct referer)
      final script = _jsBridge.getCaptureScript(pageId);
      if (script != null) {
        try {
          final b64 = await controller.evaluateJavascript(source: script);
          if (b64 is String && b64.isNotEmpty && b64 != 'null') {
            imageBytes = await compute(base64Decode, b64);
          }
        } catch (e) {
          debugPrint('[Reader] JS capture error $pageId: $e');
        }
      }
      // Fallback: direct HTTP download if JS capture failed
      if (imageBytes == null) {
        final src = pageInfo['src'] as String?;
        if (src != null &&
            src.isNotEmpty &&
            NaverWebtoonStrategy.isAllowedImageUrl(src)) {
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
        } else if (src != null && src.isNotEmpty) {
          debugPrint('[Reader] Blocked fallback fetch for $pageId: $src');
        }
      }
    }

    if (imageBytes == null || imageBytes.isEmpty) {
      debugPrint('[Reader] No image captured for $pageId');
      if (pageId.startsWith('kindle-')) {
        _hideKindleSpinner();
      }
      return;
    }

    captureSw.stop();
    debugPrint(
      '[Perf] capture page=$pageId mode=$captureMode '
      'bytes=${imageBytes.length} ms=${captureSw.elapsedMilliseconds}',
    );

    // Use site-specific pipeline: Kindle uses _kindlePipeline toggle,
    // webtoon uses its own default, others fall through to user setting.
    final pipeline = _jsBridge.activeStrategy?.siteName == 'kindle'
        ? _kindlePipeline
        : _jsBridge.activeStrategy?.defaultPipeline;
    final isWebtoon = _jsBridge.activeStrategy?.siteName == 'webtoon';

    // Extract metadata from URL
    final meta = _jsBridge.parseCurrentUrl(_currentUrl);

    // Kindle: skip pageNumber — DOM text is unreliable, hash cache handles re-visits.
    // Webtoon: use image index. Others: URL-derived page number.
    final isKindle = _jsBridge.activeStrategy?.siteName == 'kindle';
    final pageNumber = isKindle
        ? null
        : (pageInfo['index']?.toString() ?? meta?.pageNumber);
    final submitSw = Stopwatch()..start();
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
    submitSw.stop();
    debugPrint(
      '[Perf] submit page=$pageId pipeline=${pipeline ?? 'default'} '
      'ms=${submitSw.elapsedMilliseconds}',
    );

    if (isWebtoon) {
      _updateWebtoonProgress();
    }

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
        if (_submittedWebtoonIndices.contains(idx)) continue;
        final pageId = 'wt-$idx';
        // Already in jobsProvider (cache hit) — mark submitted, skip capture
        if (jobs.containsKey(pageId)) {
          _submittedWebtoonIndices.add(idx);
          continue;
        }
        toSubmit.add(idx);
        if (toSubmit.length >= _batchSize) break;
      }

      if (toSubmit.isEmpty) return;

      // Start periodic progress sync to catch missed WS completions
      _startProgressSyncTimer();

      debugPrint(
        '[Batch] Submitting ${toSubmit.length} pages: $toSubmit '
        '(detected=${_detectedWebtoonPages.length}, submitted=${_submittedWebtoonIndices.length})',
      );
      _updateWebtoonProgress();

      // Submit all pages in parallel for faster throughput
      await Future.wait(
        toSubmit.map((idx) async {
          final pageInfo = _detectedWebtoonPages[idx]!;
          final pageId = pageInfo['pageId'] as String;
          try {
            await _capturePageImage(pageId, pageInfo);
            // Only mark as submitted if capture+submit succeeded
            _submittedWebtoonIndices.add(idx);
          } catch (e) {
            debugPrint('[Reader] Failed to capture $pageId: $e');
          }
        }),
      );

      _updateWebtoonProgress();
    } finally {
      _batchInProgress = false;
      // Check if more pages are waiting — schedule next batch
      final sortedIndices = _detectedWebtoonPages.keys.toList()..sort();
      final hasMore = sortedIndices.any(
        (idx) => !_submittedWebtoonIndices.contains(idx),
      );
      if (hasMore) {
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
      if (_jsBridge.activeStrategy?.siteName == 'webtoon') {
        _updateWebtoonProgress();
      }
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
      final isWebtoonSite = _jsBridge.activeStrategy?.siteName == 'webtoon';
      if (job.isComplete && job.translatedImage != null) {
        if (isWebtoonSite) {
          _updateWebtoonProgress();
        }
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
        if (_jsBridge.activeStrategy?.siteName == 'kindle') {
          _hideKindleSpinner();
        }
      } else if (job.isFailed) {
        _completionListeners[pageId]?.close();
        _completionListeners.remove(pageId);
        if (_jsBridge.activeStrategy?.siteName == 'kindle' &&
            pageId == _currentKindlePageId) {
          _hideKindleSpinner();
        }
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
        try {
          await _overlay.replaceImageBySrc(
            controller,
            originalSrc,
            imageBytes,
            pageId,
          );
        } catch (e) {
          debugPrint('[Overlay] replaceImageBySrc threw: $e');
        }
      }
    } else if (_jsBridge.activeStrategy?.siteName == 'kindle') {
      await _applyKindleOverlayBytes(
        pageId: pageId,
        imageBytes: imageBytes,
        isSpread: false,
      );
    }
  }

  Future<void> _applyKindleOverlayBytes({
    required String pageId,
    required Uint8List imageBytes,
    required bool isSpread,
  }) async {
    final controller = _webController;
    if (controller == null) return;
    if (_jsBridge.activeStrategy?.siteName != 'kindle') return;
    final expectedBlob = _kindleBlobByPageId[pageId];
    final expectedRect = _kindleRectByPageId[pageId];
    final overlayToken = 'ov-$pageId-${DateTime.now().millisecondsSinceEpoch}';
    final postStageOk = isSpread ? 'post_spread_ok' : 'post_ok';
    final postStageFail = isSpread ? 'post_spread_fail' : 'post_fail';
    final postStage180 = isSpread ? 'post_spread_180ms' : 'post_180ms';
    final postStage900 = isSpread ? 'post_spread_900ms' : 'post_900ms';

    _probeKindleOverlay(
      stage: isSpread ? 'pre_spread' : 'pre',
      pageId: pageId,
      expectedBlob: expectedBlob,
      expectedRect: expectedRect,
      overlayToken: overlayToken,
    );

    final overlaySw = Stopwatch()..start();
    var ok = false;
    var usedFallback = false;
    try {
      ok = await _overlay.replaceVisibleKindlePage(
        controller,
        imageBytes,
        pageId: pageId,
        expectedBlobSrc: expectedBlob,
        expectedRect: expectedRect,
        overlayToken: overlayToken,
      );
      if (!ok && pageId == _currentKindlePageId) {
        usedFallback = true;
        ok = await _overlay.replaceVisibleKindlePage(
          controller,
          imageBytes,
          pageId: pageId,
          expectedRect: expectedRect,
          overlayToken: overlayToken,
        );
      }
    } catch (e) {
      debugPrint('[Overlay] replaceVisibleKindlePage threw: $e');
    }
    if (ok) {
      _kindleOverlayOk++;
    } else {
      _kindleOverlayFail++;
    }
    if (usedFallback) _kindleOverlayFallback++;
    _pushKindleDebugHudToPage();
    overlaySw.stop();
    debugPrint(
      '[Overlay] ${isSpread ? 'spread ' : ''}$pageId replace=${ok ? 'OK' : 'FAIL'}'
      '${usedFallback ? ' (fallback)' : ''} imageBytes=${imageBytes.length} '
      'ms=${overlaySw.elapsedMilliseconds}',
    );
    final logData = <String, dynamic>{
      if (isSpread) 'spreadPageId': pageId,
      if (!isSpread) 'pageId': pageId,
      if (isSpread) 'stitchedSize': '${imageBytes.length} bytes',
      'expectedBlob': expectedBlob,
      'success': ok,
    };
    _logKindle(isSpread ? 'kindle_spread_overlay' : 'kindle_overlay', logData);

    _probeKindleOverlay(
      stage: ok ? postStageOk : postStageFail,
      pageId: pageId,
      expectedBlob: expectedBlob,
      expectedRect: expectedRect,
      overlayToken: overlayToken,
    );
    if (_kindleVerboseProbeLogs) {
      Future.delayed(const Duration(milliseconds: 180), () {
        if (!mounted || _currentKindlePageId != pageId) return;
        _probeKindleOverlay(
          stage: postStage180,
          pageId: pageId,
          expectedBlob: expectedBlob,
          expectedRect: expectedRect,
          overlayToken: overlayToken,
        );
      });
      Future.delayed(const Duration(milliseconds: 900), () {
        if (!mounted || _currentKindlePageId != pageId) return;
        _probeKindleOverlay(
          stage: postStage900,
          pageId: pageId,
          expectedBlob: expectedBlob,
          expectedRect: expectedRect,
          overlayToken: overlayToken,
        );
      });
    }
    if (ok && pageId == _currentKindlePageId) {
      _scheduleKindleOverlayReapply(
        pageId: pageId,
        imageBytes: imageBytes,
        expectedBlob: expectedBlob,
        expectedRect: expectedRect,
        baseToken: overlayToken,
      );
    } else if (!ok && pageId == _currentKindlePageId) {
      _scheduleKindleOverlayRecovery(
        pageId: pageId,
        imageBytes: imageBytes,
        expectedBlob: expectedBlob,
        expectedRect: expectedRect,
        baseToken: overlayToken,
      );
    }
  }

  /// Apply overlay for a 2-page spread: stitch halves and DOM-replace.
  Future<void> _applySpreadOverlay(
    String spreadPageId,
    Uint8List leftImage,
    Uint8List rightImage,
  ) async {
    final stitched = await ImageCaptureService.stitchSpreadAsync(
      leftImage,
      rightImage,
    );
    if (stitched == null) return;
    await _applyKindleOverlayBytes(
      pageId: spreadPageId,
      imageBytes: stitched,
      isSpread: true,
    );
  }

  /// Kindle can repaint the visible page shortly after we replace the img src.
  /// Re-apply once or twice for the still-visible page to make replacement stick.
  void _scheduleKindleOverlayReapply({
    required String pageId,
    required Uint8List imageBytes,
    String? expectedBlob,
    Map<String, num>? expectedRect,
    String? baseToken,
  }) {
    // Require blob anchor for delayed re-apply to prevent stale-page paints.
    if (expectedBlob == null || expectedBlob.isEmpty) return;
    _cancelKindleReappliesFor(pageId);

    // One short post-apply pass handles most Kindle repaint churn.
    // 260ms sits just after Kindle's typical compositor flush (~200-250ms)
    // while staying under the threshold where users notice a flicker.
    final delays = <int>[260];
    final timers = <Timer>[];
    _kindleOverlayTimers[pageId] = timers;
    for (final ms in delays) {
      final token =
          '${baseToken ?? 'ov-$pageId'}-reapply-$ms-${DateTime.now().millisecondsSinceEpoch}';
      final timer = Timer(Duration(milliseconds: ms), () async {
        if (!mounted) return;
        if (_currentKindlePageId != pageId) return;
        if (_jsBridge.activeStrategy?.siteName != 'kindle') return;
        if (!_showOverlay) return;
        final controller = _webController;
        if (controller == null) return;
        final ok = await _overlay.reapplyVisibleKindlePage(
          controller,
          pageId: pageId,
          expectedBlobSrc: expectedBlob,
          expectedRect: expectedRect,
          overlayToken: token,
        );
        debugPrint(
          '[Overlay] reapply page=$pageId delay=${ms}ms '
          'result=${ok ? 'OK' : 'FAIL'}',
        );
        _probeKindleOverlay(
          stage: 'reapply_${ms}ms',
          pageId: pageId,
          expectedBlob: expectedBlob,
          expectedRect: expectedRect,
          overlayToken: token,
        );
      });
      timers.add(timer);
    }
  }

  /// If first overlay attempt happens during Kindle loader/repaint, retry
  /// a few times while user stays on the same page.
  void _scheduleKindleOverlayRecovery({
    required String pageId,
    required Uint8List imageBytes,
    String? expectedBlob,
    Map<String, num>? expectedRect,
    String? baseToken,
  }) {
    _cancelKindleReappliesFor(pageId);
    // Exponential-ish backoff tuned to Kindle's page-load lifecycle:
    //   180ms — catch fast repaint after initial blob swap
    //   420ms — after Kindle's JS page-turn animation settles
    //   820ms — after lazy image decode on slower devices
    //  1400ms — final attempt covering network-loaded page assets
    final delays = <int>[180, 420, 820, 1400];
    final timers = <Timer>[];
    _kindleOverlayTimers[pageId] = timers;
    for (var idx = 0; idx < delays.length; idx++) {
      final ms = delays[idx];
      final token =
          '${baseToken ?? 'ov-$pageId'}-recovery-$ms-${DateTime.now().millisecondsSinceEpoch}';
      final timer = Timer(Duration(milliseconds: ms), () async {
        if (!mounted) return;
        if (_currentKindlePageId != pageId) return;
        if (_jsBridge.activeStrategy?.siteName != 'kindle') return;
        if (!_showOverlay) return;
        final controller = _webController;
        if (controller == null) return;

        // Try anchored replacement first; on later attempts allow fallback to
        // current visible blob (blob URLs can churn during Kindle load).
        var ok = await _overlay.replaceVisibleKindlePage(
          controller,
          imageBytes,
          pageId: pageId,
          expectedBlobSrc: expectedBlob,
          expectedRect: expectedRect,
          overlayToken: token,
        );
        if (!ok && idx >= 1) {
          ok = await _overlay.replaceVisibleKindlePage(
            controller,
            imageBytes,
            pageId: pageId,
            expectedRect: expectedRect,
            overlayToken: token,
          );
        }
        debugPrint(
          '[Overlay] recovery page=$pageId delay=${ms}ms '
          'result=${ok ? 'OK' : 'FAIL'}',
        );
        _probeKindleOverlay(
          stage: 'recovery_${ms}ms',
          pageId: pageId,
          expectedBlob: expectedBlob,
          expectedRect: expectedRect,
          overlayToken: token,
        );
      });
      timers.add(timer);
    }
  }

  void _cancelKindleReapplies({String? keepPageId}) {
    final keys = _kindleOverlayTimers.keys.toList();
    for (final pageId in keys) {
      if (keepPageId != null && pageId == keepPageId) continue;
      final timers = _kindleOverlayTimers.remove(pageId);
      if (timers == null) continue;
      for (final t in timers) {
        t.cancel();
      }
    }
  }

  void _cancelKindleReappliesFor(String pageId) {
    final timers = _kindleOverlayTimers.remove(pageId);
    if (timers == null) return;
    for (final t in timers) {
      t.cancel();
    }
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
    '<button id="__frankPipeline" title="Switch pipeline" style="display:none;"></button>' +
    '<button id="__frankZoom" title="Fit width" style="display:none;">Fit W</button>' +
    '<button id="__frankTranslate" title="Translate current page" style="display:none;">&#x1F30D; Translate</button>' +
    '<button id="__frankReload" title="Reload page">&#x21BB; Reload</button>' +
    '<button id="__frankCopyDbg" title="Copy debug" style="display:none;">Copy Debug</button>' +
    '<span id="__frankStatus"></span>' +
    '<span id="__frankSpinner" style="display:none;align-items:center;">' +
      '<svg width="16" height="16" viewBox="0 0 16 16" style="animation:frankspin 0.8s linear infinite;">' +
        '<circle cx="8" cy="8" r="6" fill="none" stroke="rgba(255,255,255,0.3)" stroke-width="2"/>' +
        '<path d="M8 2a6 6 0 0 1 6 6" fill="none" stroke="#4caf50" stroke-width="2" stroke-linecap="round"/>' +
      '</svg>' +
      '<style>@keyframes frankspin{to{transform:rotate(360deg)}}</style>' +
    '</span>' +
    '<div id="__frankProgress" style="display:none;align-items:center;gap:6px;">' +
      '<div style="width:120px;height:8px;background:rgba(255,255,255,0.2);border-radius:4px;overflow:hidden;">' +
        '<div id="__frankProgressFill" style="width:0%;height:100%;background:#4caf50;border-radius:4px;transition:width 0.3s;"></div>' +
      '</div>' +
      '<span id="__frankProgressText" style="font-size:11px;color:rgba(255,255,255,0.7);"></span>' +
    '</div>';
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
  var pipeBtn = document.getElementById('__frankPipeline');
  var zoomBtn = document.getElementById('__frankZoom');
  var translateBtn = document.getElementById('__frankTranslate');
  var reloadBtn = document.getElementById('__frankReload');
  var copyDbgBtn = document.getElementById('__frankCopyDbg');
  if (backBtn) backBtn.style.cssText = btnStyle;
  if (pipeBtn) pipeBtn.style.cssText = btnStyle + 'display:none;';
  if (zoomBtn) zoomBtn.style.cssText = btnStyle + 'display:none;';
  if (translateBtn) translateBtn.style.cssText = btnStyle + 'display:none;';
  if (reloadBtn) reloadBtn.style.cssText = btnStyle;
  if (copyDbgBtn) copyDbgBtn.style.cssText = btnStyle + 'display:none;';

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

  if (backBtn) backBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'back');
  });
  if (pipeBtn) pipeBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'toggle_pipeline');
  });
  if (zoomBtn) zoomBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'toggle_zoom');
  });
  if (translateBtn) translateBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'translate');
  });
  if (reloadBtn) reloadBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'reload');
  });
  if (copyDbgBtn) copyDbgBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'copy_debug');
  });

  /* Global functions Dart can call */
  window.__frankSetStatus = function(text) {
    var el = document.getElementById('__frankStatus');
    if (el) el.textContent = text;
  };
  window.__frankShowSpinner = function() {
    var el = document.getElementById('__frankSpinner');
    if (el) el.style.display = 'inline-flex';
  };
  window.__frankHideSpinner = function() {
    var el = document.getElementById('__frankSpinner');
    if (el) el.style.display = 'none';
  };
  window.__frankSetProgress = function(completed, total) {
    var prog = document.getElementById('__frankProgress');
    var fill = document.getElementById('__frankProgressFill');
    var text = document.getElementById('__frankProgressText');
    if (!prog || !fill || !text) return;
    if (total <= 0) { prog.style.display = 'none'; return; }
    prog.style.display = 'flex';
    var pct = Math.round((completed / total) * 100);
    fill.style.width = pct + '%';
    text.textContent = completed + '/' + total;
    if (completed >= total) {
      setTimeout(function() { prog.style.display = 'none'; }, 3000);
    }
  };
  window.__frankZoomActive = false;
  window.__frankSetZoom = function(active, visible) {
    var btn = document.getElementById('__frankZoom');
    if (btn) {
      btn.style.display = visible ? '' : 'none';
      btn.style.borderColor = active ? '#64b5f6' : 'rgba(255,255,255,0.3)';
      btn.style.color = active ? '#64b5f6' : '#fff';
    }
    window.__frankZoomActive = active;
    window.__frankApplyZoom();
  };
  window.__frankApplyZoom = function() {
    var active = window.__frankZoomActive;
    // Kindle DOM hierarchy (from actual page inspection):
    //   .kr-renderer-container (overflow:hidden — clips everything)
    //     #kr-renderer (fixed width/height)
    //       .kg-view (position:absolute, current page at left:0)
    //         div (display:flex)
    //           .kg-full-page-img (pointer-events:none)
    //             img[src^="blob:"] (inline width/height, object-fit:cover)
    var root = document.getElementById('kr-renderer') || document.body;
    var imgs = root.querySelectorAll('img');
    for (var i = 0; i < imgs.length; i++) {
      var img = imgs[i];
      if (!img.src || !img.src.startsWith('blob:')) continue;
      if (active) {
        img.style.setProperty('width', '100vw', 'important');
        img.style.setProperty('height', 'auto', 'important');
        img.style.setProperty('max-width', 'none', 'important');
        img.style.setProperty('max-height', 'none', 'important');
        img.style.setProperty('object-fit', 'contain', 'important');
      } else {
        img.style.removeProperty('width');
        img.style.removeProperty('height');
        img.style.removeProperty('max-width');
        img.style.removeProperty('max-height');
        img.style.removeProperty('object-fit');
      }
    }
    // Fix #kr-renderer: release the fixed height so the taller image can show
    if (active) {
      root.style.setProperty('height', 'auto', 'important');
      root.style.setProperty('min-height', '100vh', 'important');
      root.style.setProperty('overflow', 'visible', 'important');
    } else {
      root.style.removeProperty('height');
      root.style.removeProperty('min-height');
      root.style.removeProperty('overflow');
    }
    // Fix .kr-renderer-container: this has overflow:hidden that clips the page
    var container = document.querySelector('.kr-renderer-container');
    if (container) {
      if (active) {
        container.style.setProperty('overflow-y', 'auto', 'important');
        container.style.setProperty('overflow-x', 'hidden', 'important');
      } else {
        container.style.removeProperty('overflow-y');
        container.style.removeProperty('overflow-x');
      }
    }
    // Fix .kg-view: release absolute positioning height constraint
    var views = root.querySelectorAll('.kg-view');
    for (var v = 0; v < views.length; v++) {
      if (active) {
        views[v].style.setProperty('height', 'auto', 'important');
      } else {
        views[v].style.removeProperty('height');
      }
    }
    // Fix the interaction layer and pagination container heights
    var interactionLayer = document.querySelector('.kr-interaction-layer');
    if (interactionLayer) {
      if (active) {
        interactionLayer.style.setProperty('height', 'auto', 'important');
        interactionLayer.style.setProperty('min-height', '100vh', 'important');
      } else {
        interactionLayer.style.removeProperty('height');
        interactionLayer.style.removeProperty('min-height');
      }
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
  window.__frankSetTranslateBtn = function(visible) {
    var el = document.getElementById('__frankTranslate');
    if (el) el.style.display = visible ? '' : 'none';
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
          case 'toggle_pipeline':
            _toggleKindlePipeline();
            break;
          case 'toggle_zoom':
            _toggleKindleFitWidth();
            break;
          case 'translate':
            _translateCurrentPage();
            break;
          case 'reload':
            _reloadPage();
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

  /// Toggle Kindle pipeline between furigana and english translation.
  /// Cancels all active Kindle jobs and re-submits the current page.
  void _toggleKindlePipeline() {
    _kindlePipeline = _kindlePipeline == 'manga_furigana'
        ? 'manga_translate'
        : 'manga_furigana';
    _syncPipelineButtonState();

    // Persist per-title pipeline preference
    final asin = _currentAsin;
    if (asin != null && asin.isNotEmpty) {
      SharedPreferences.getInstance().then((prefs) {
        prefs.setString('kindle_pipeline_$asin', _kindlePipeline);
      });
    }

    // Cancel all Kindle jobs and re-submit current page with new pipeline
    _cancelKindleJobs();
    if (_currentKindlePageId != null && _lastKindlePageInfo != null) {
      _capturePageImage(_currentKindlePageId!, _lastKindlePageInfo!);
    }
  }

  /// Toggle Kindle fit-width zoom mode and persist per-ASIN.
  void _toggleKindleFitWidth() {
    _kindleFitWidth = !_kindleFitWidth;
    _syncZoomButtonState();

    final asin = _currentAsin;
    if (asin != null && asin.isNotEmpty) {
      SharedPreferences.getInstance().then((prefs) {
        prefs.setBool('kindle_zoom_$asin', _kindleFitWidth);
      });
    }
  }

  /// Sync the in-page zoom button with current state.
  /// Also re-applies zoom CSS to the current blob image (needed after page turns
  /// since Kindle replaces the img element).
  void _syncZoomButtonState() {
    final controller = _webController;
    if (controller == null) return;
    final isKindle = _jsBridge.activeStrategy?.siteName == 'kindle';
    controller.evaluateJavascript(
      source:
          "if(window.__frankSetZoom) window.__frankSetZoom($_kindleFitWidth, $isKindle);",
    );
    // Re-apply after a short delay so the new blob image is in the DOM
    if (_kindleFitWidth && isKindle) {
      Future.delayed(const Duration(milliseconds: 300), () {
        controller.evaluateJavascript(
          source: "if(window.__frankApplyZoom) window.__frankApplyZoom();",
        );
      });
    }
  }

  /// Manually translate the current page (used when auto-translate is off).
  void _translateCurrentPage() {
    _showKindleSpinner();
    _updateInPageStatus('Translating...');
    if (_currentKindlePageId != null && _lastKindlePageInfo != null) {
      _capturePageImage(_currentKindlePageId!, _lastKindlePageInfo!);
      return;
    }
    if (_jsBridge.activeStrategy?.siteName == 'webtoon') {
      _submitNextBatch();
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

  /// Show or hide the manual translate button based on auto-translate setting.
  void _syncTranslateButtonState() {
    final controller = _webController;
    if (controller == null) return;
    final settings = ref.read(settingsProvider);
    final visible = settings.isLoaded && !settings.autoTranslate;
    controller.evaluateJavascript(
      source:
          "if(window.__frankSetTranslateBtn) window.__frankSetTranslateBtn($visible);",
    );
  }

  /// Full reload of the WebView page.
  void _reloadPage() {
    _webController?.evaluateJavascript(source: 'location.reload();');
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
      _kindleRectByPageId.remove(id);
      final timers = _kindleOverlayTimers.remove(id);
      if (timers != null) {
        for (final t in timers) {
          t.cancel();
        }
      }
    }
  }

  /// Cancel all active Kindle jobs, prefetch, and their completion listeners.
  void _cancelKindleJobs() {
    _hideKindleSpinner();
    _kindleBlobByPageId.clear();
    _kindleRectByPageId.clear();
    _cancelKindleReapplies();
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
  }

  /// Periodic timer that syncs webtoon progress every 2 seconds.
  /// Catches missed WebSocket completion events on mobile.
  void _startProgressSyncTimer() {
    if (_progressSyncTimer != null) return;
    _progressSyncTimer = Timer.periodic(const Duration(seconds: 2), (_) {
      if (!mounted) {
        _progressSyncTimer?.cancel();
        _progressSyncTimer = null;
        return;
      }
      _updateWebtoonProgress();

      // Auto-stop when all detected pages are complete
      final jobs = ref.read(jobsProvider);
      final allDone = _detectedWebtoonPages.keys.every((idx) {
        final job = jobs['wt-$idx'];
        return job != null && job.isComplete;
      });
      if (allDone && _detectedWebtoonPages.isNotEmpty) {
        _progressSyncTimer?.cancel();
        _progressSyncTimer = null;
      }
    });
  }

  /// Update the webtoon progress bar (completed/total).
  void _updateWebtoonProgress() {
    if (_jsBridge.activeStrategy?.siteName != 'webtoon') return;
    final controller = _webController;
    if (controller == null) return;
    // Use JS-reported total (includes unloaded images) so the bar doesn't
    // show 1/1 while only the first image has loaded.
    final detected = _detectedWebtoonPages.length;
    final total = _webtoonTotalPages > detected ? _webtoonTotalPages : detected;
    if (total == 0) return;
    final jobs = ref.read(jobsProvider);
    int completed = 0;
    for (final idx in _detectedWebtoonPages.keys) {
      final job = jobs['wt-$idx'];
      if (job != null && job.isComplete) completed++;
    }
    controller.evaluateJavascript(
      source:
          'if(window.__frankSetProgress) window.__frankSetProgress($completed, $total);',
    );
  }

  /// Push a status message into the in-page toolbar.
  void _showKindleSpinner() {
    _webController?.evaluateJavascript(
      source: "if(window.__frankShowSpinner) window.__frankShowSpinner();",
    );
  }

  void _hideKindleSpinner() {
    _webController?.evaluateJavascript(
      source: "if(window.__frankHideSpinner) window.__frankHideSpinner();",
    );
  }

  void _updateInPageStatus(String text, {Duration? clearAfter}) {
    final controller = _webController;
    if (controller == null) return;
    _statusClearTimer?.cancel();
    final messageVersion = ++_statusMessageVersion;
    final escaped = text.replaceAll("'", "\\'").replaceAll('\n', ' ');
    controller.evaluateJavascript(
      source:
          "if(window.__frankSetStatus) window.__frankSetStatus('$escaped');",
    );
    if (clearAfter != null && text.isNotEmpty) {
      _statusClearTimer = Timer(clearAfter, () {
        if (!mounted) return;
        if (_statusMessageVersion != messageVersion) return;
        final c = _webController;
        if (c == null) return;
        c.evaluateJavascript(
          source: "if(window.__frankSetStatus) window.__frankSetStatus('');",
        );
      });
    }
  }
}
