import 'dart:async';
import 'dart:convert';
import 'dart:io' show Platform;
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:http/http.dart' as http;
import '../providers/jobs_provider.dart';
import '../providers/connection_provider.dart';
import '../providers/settings_provider.dart';
import '../services/api_service.dart';
import '../services/image_capture_service.dart';
import '../webview/dom_inspector.dart';
import '../webview/js_bridge.dart';
import '../webview/kindle_prefetch_manager.dart';
import '../webview/overlay_controller.dart';
import '../webview/platform/app_webview.dart';
import '../webview/platform/app_webview_controller.dart';
import '../webview/strategies/kindle_strategy.dart';
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

class _ReaderScreenState extends ConsumerState<ReaderScreen> {
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
  Map<String, Object?> _kindlePrefetchState = const {};
  String _kindleNavIntent = 'forward';
  final bool _kindleDebugHudEnabled = false;
  final bool _kindleVerboseProbeLogs = false;
  bool _overlayEditMode = false;
  int _kindleOverlayOk = 0;
  int _kindleOverlayFail = 0;
  int _kindleOverlayFallback = 0;
  final Map<String, Map<String, dynamic>> _metadataByPageId = {};
  final Map<String, Map<String, dynamic>> _metadataOriginalByPageId = {};
  final Set<String> _metadataLoadingPageIds = <String>{};
  final Set<String> _dirtyMetadataPageIds = <String>{};

  /// Selected pipeline for Kindle pages (furigana vs english translation).
  String _kindlePipeline = 'manga_translate';

  /// Current Kindle ASIN for per-title pipeline persistence.
  String? _currentAsin;

  /// Background webview prefetch manager (Linux-only).
  late KindlePrefetchManager _kindlePrefetch;
  int _kindlePrefetchWindow = -1;
  bool _kindleOverlayPending = false;
  int _currentKindleIndex = 0;

  // --- Webtoon batching state ---
  static const _batchSize = 5;
  static const _prefetchThreshold = 2;

  /// All detected webtoon page infos, keyed by index.
  final Map<int, Map<String, dynamic>> _detectedWebtoonPages = {};

  /// Webtoon page indices that were successfully captured and submitted.
  final Set<int> _submittedWebtoonIndices = {};

  /// Whether a batch submission is currently in progress.
  bool _batchInProgress = false;

  /// Track active listenManual subscriptions so we can cancel them.
  final Map<String, ProviderSubscription> _completionListeners = {};
  Timer? _statusClearTimer;
  int _statusMessageVersion = 0;

  @override
  void initState() {
    super.initState();
    _recreateKindlePrefetchForSettings();
  }

  @override
  void dispose() {
    _statusClearTimer?.cancel();
    _cancelKindleReapplies();
    _kindlePrefetch.dispose();
    for (final sub in _completionListeners.values) {
      sub.close();
    }
    _completionListeners.clear();
    super.dispose();
  }

  int _effectivePrefetchWindow() {
    final raw = ref.read(settingsProvider).prefetchPages;
    if (raw <= 0) return 0;
    if (raw > 6) return 6;
    return raw;
  }

  void _recreateKindlePrefetchForSettings() {
    final window = _effectivePrefetchWindow();
    _kindlePrefetchWindow = window;
    _kindlePrefetch = KindlePrefetchManager(
      maxPagesAhead: window,
      batchSize: window >= 3 ? 3 : window,
      triggerThreshold: window <= 1 ? 0 : (window <= 3 ? 1 : 2),
    );
  }

  void _syncKindlePrefetchConfig() {
    final window = _effectivePrefetchWindow();
    if (window == _kindlePrefetchWindow) return;
    _kindlePrefetch.dispose();
    _recreateKindlePrefetchForSettings();
  }

  bool _setKindleOverlayPending(bool pending, {String reason = ''}) {
    if (_kindleOverlayPending == pending) return false;
    _kindleOverlayPending = pending;
    if (kDebugMode) {
      debugPrint(
        '[KindleOverlay] pending=$pending'
        '${reason.isNotEmpty ? ' reason=$reason' : ''}',
      );
    }
    return true;
  }

  void _resumeKindlePrefetchIfReady() {
    if (_kindleOverlayPending) return;
    _triggerKindlePrefetch(_currentKindleIndex, _kindleNavIntent);
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
              setState(() => _currentUrl = urlStr);
              final isKindleNow = urlStr.contains('read.amazon.co.jp');
              final preserveKindleSessionState =
                  isKindleNow && _lastLoadStopUrl == urlStr;

              // Reset state on true page load/navigation. Kindle often emits
              // same-URL load stops while paging; avoid wiping runtime state.
              if (!preserveKindleSessionState) {
                _detectedWebtoonPages.clear();
                _submittedWebtoonIndices.clear();
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
                _kindlePrefetchState = const {};
                _kindleNavIntent = 'forward';
                _kindleOverlayOk = 0;
                _kindleOverlayFail = 0;
                _kindleOverlayFallback = 0;
                _metadataByPageId.clear();
                _metadataOriginalByPageId.clear();
                _metadataLoadingPageIds.clear();
                _dirtyMetadataPageIds.clear();
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
                _syncAutoButtonState();
                _syncPipelineButtonState();
                _syncEditModeButtonState();
                _syncFeedbackActionButtons();
                _syncEditModeToPage();
                _syncFeedbackMarksOverlay();
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

  void _onPageDetected(Map<String, dynamic> pageInfo) {
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
      // Trigger bg webview prefetch on Kindle page change
      final kindleIndex = (pageInfo['index'] as num?)?.toInt() ?? 0;
      _currentKindleIndex = kindleIndex;
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
          '[KindleDetect] page=$pageId index=$kindleIndex nav=$newIntent '
          'rect=${rw}x$rh dpr=$dpr',
        );
      }
      // Load per-title pipeline preference from ASIN
      final meta = _jsBridge.parseCurrentUrl(_currentUrl);
      final asin = meta?.title;
      if (asin != null && asin.isNotEmpty && asin != _currentAsin) {
        _currentAsin = asin;
        SharedPreferences.getInstance().then((prefs) {
          final saved = prefs.getString('kindle_pipeline_$asin');
          if (saved != null && saved.isNotEmpty && saved != _kindlePipeline) {
            setState(() {
              _kindlePipeline = saved;
            });
            _syncPipelineButtonState();
          }
        });
      }
      _pushKindleDebugHudToPage();
      _syncFeedbackMarksOverlay();
      // Keep the prefetch manager running — it submits low-priority jobs
      // that won't compete with the current page's high-priority job.
      // This avoids destroying the bg webview on every page turn (re-init
      // takes ~10s, during which the user pages ahead and kills it again).
      _triggerKindlePrefetch(kindleIndex, newIntent);
    }

    _logKindle('kindle_detect', {
      'pageId': pageId,
      'pageMode': pageInfo['pageMode'],
      'type': pageInfo['type'],
    });
    if (_overlayEditMode) {
      _syncFeedbackMarksOverlay();
    }

    final settings = ref.read(settingsProvider);
    final isKindle = _jsBridge.activeStrategy?.siteName == 'kindle';
    if (isKindle) {
      if (!settings.autoTranslate) {
        _setKindleOverlayPending(false, reason: 'auto_off');
      } else {
        _setKindleOverlayPending(true, reason: 'page_detected');
      }
    }

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
      final submitted = _submittedWebtoonIndices.length;
      _updateInPageStatus('Page $pageId ($submitted/$detected queued)');

      if (!settings.autoTranslate) {
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
      _setKindleOverlayPending(false, reason: 'spread_already_complete');
      _resumeKindlePrefetchIfReady();
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
    Map<String, dynamic> pageInfo, {
    bool force = false,
  }) async {
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
              force: force,
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
              force: force,
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
    }

    if (imageBytes == null || imageBytes.isEmpty) {
      debugPrint('[Reader] No image captured for $pageId');
      if (pageId.startsWith('kindle-')) {
        _setKindleOverlayPending(false, reason: 'capture_empty');
        _resumeKindlePrefetchIfReady();
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
    _updateInPageStatus('Submitting $pageId...');

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
          force: force,
        );
    submitSw.stop();
    debugPrint(
      '[Perf] submit page=$pageId pipeline=${pipeline ?? 'default'} '
      'ms=${submitSw.elapsedMilliseconds}',
    );

    _updateInPageStatus('Queued $pageId');

    // Watch for completion to apply overlay
    _watchForCompletion(pageId);
  }

  /// Submit the next batch of webtoon pages (up to _batchSize).
  /// Downloads and submits pages in parallel for faster throughput.
  Future<void> _submitNextBatch({bool force = false}) async {
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
        if (jobs.containsKey(pageId)) continue;
        toSubmit.add(idx);
        if (toSubmit.length >= _batchSize) break;
      }

      if (toSubmit.isEmpty) return;

      debugPrint(
        '[Batch] Submitting ${toSubmit.length} pages: $toSubmit '
        '(detected=${_detectedWebtoonPages.length}, submitted=${_submittedWebtoonIndices.length})',
      );
      _updateInPageStatus('Batch: submitting ${toSubmit.length} pages...');

      // Submit all pages in parallel for faster throughput
      await Future.wait(
        toSubmit.map((idx) async {
          final pageInfo = _detectedWebtoonPages[idx]!;
          final pageId = pageInfo['pageId'] as String;
          try {
            await _capturePageImage(pageId, pageInfo, force: force);
            // Only mark as submitted if capture+submit succeeded
            _submittedWebtoonIndices.add(idx);
          } catch (e) {
            debugPrint('[Reader] Failed to capture $pageId: $e');
          }
        }),
      );

      final total = _detectedWebtoonPages.length;
      final done = _submittedWebtoonIndices.length;
      _updateInPageStatus('Queued $done/$total pages');
    } finally {
      _batchInProgress = false;
      // Check if more pages are waiting — schedule next batch
      final sortedIndices = _detectedWebtoonPages.keys.toList()..sort();
      final hasMore = sortedIndices.any(
        (idx) =>
            !_submittedWebtoonIndices.contains(idx) &&
            !ref.read(jobsProvider).containsKey('wt-$idx'),
      );
      if (hasMore) {
        Future.delayed(const Duration(milliseconds: 100), () {
          if (mounted) _submitNextBatch(force: force);
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
      _ensureMetadataForPage(pageId);
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
        _ensureMetadataForPage(pageId);
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
        if (_jsBridge.activeStrategy?.siteName == 'kindle' &&
            pageId == _currentKindlePageId) {
          _setKindleOverlayPending(false, reason: 'job_failed');
          _resumeKindlePrefetchIfReady();
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
      _ensureMetadataForPage(leftId);
      _ensureMetadataForPage(rightId);
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
        _ensureMetadataForPage(leftId);
        _ensureMetadataForPage(rightId);
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
    if (pageId == _currentKindlePageId) {
      final changed = _setKindleOverlayPending(
        false,
        reason: ok ? 'overlay_applied' : 'overlay_failed',
      );
      if (changed) {
        _resumeKindlePrefetchIfReady();
      }
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

  Future<void> _captureAndTranslate({bool force = false}) async {
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
          force: force,
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
    '<button id="__frankToggleOrig" title="Toggle original/translated">Original</button>' +
    '<button id="__frankFeedback" title="Toggle feedback mode">Feedback: OFF</button>' +
    '<button id="__frankSaveEdits" title="Save feedback edits" style="display:none;">Save</button>' +
    '<button id="__frankCancelEdits" title="Cancel feedback edits" style="display:none;">Cancel</button>' +
    '<button id="__frankReload" title="Reload page">&#x21BB; Reload</button>' +
    '<button id="__frankClearCache" title="Clear all cached translations">&#x1F5D1; Clear Cache</button>' +
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


  var marksLayer = document.createElement('div');
  marksLayer.id = '__frankFeedbackMarks';
  // Zero-size container with overflow:visible — avoids creating a large GPU
  // compositing surface that WebKitGTK renders as an opaque dark rectangle.
  // Child mark divs use position:fixed so they still cover the viewport.
  marksLayer.style.cssText =
    'position:fixed; left:0; top:0; width:0; height:0; overflow:visible;' +
    'z-index:1000000; pointer-events:none; display:none;';
  document.body.appendChild(marksLayer);

  var editMode = false;
  var feedbackMarksEnabled = false;
  var feedbackMarks = [];
  var renderedFeedbackMarks = [];
  var marksRaf = null;
  function isUiTarget(target) {
    if (!target || !target.closest) return false;
    return !!target.closest('#__frankBar,#__frankBarToggle');
  }
  function isVisibleImage(img) {
    if (!img || img.tagName !== 'IMG') return false;
    var st = window.getComputedStyle(img);
    if (!st) return false;
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    var op = parseFloat(st.opacity || '1');
    if (!isFinite(op) || op <= 0.05) return false;
    var r = img.getBoundingClientRect();
    if (!r || r.width < 40 || r.height < 40) return false;
    return true;
  }
  function findImageAtPoint(x, y) {
    var top = document.elementFromPoint(x, y);
    if (top && top.tagName === 'IMG' && isVisibleImage(top)) return top;
    if (top && top.closest) {
      var near = top.closest('img');
      if (near && isVisibleImage(near)) return near;
    }
    var imgs = document.querySelectorAll('img');
    var best = null;
    var bestScore = -Infinity;
    for (var i = 0; i < imgs.length; i++) {
      var img = imgs[i];
      if (!isVisibleImage(img)) continue;
      var r = img.getBoundingClientRect();
      if (x < r.left || x > r.right || y < r.top || y > r.bottom) continue;
      var area = r.width * r.height;
      // Prefer translated images when available.
      var translatedBoost = (img.dataset && img.dataset.frankTranslated === 'true') ? 1e12 : 0;
      var score = translatedBoost + area;
      if (score > bestScore) {
        bestScore = score;
        best = img;
      }
    }
    return best;
  }
  function findImageByPageId(pageId) {
    if (!pageId) return null;
    // 1. Exact match via data-frank-page-id attribute
    var imgs = document.querySelectorAll('img');
    var best = null;
    var bestScore = -Infinity;
    for (var i = 0; i < imgs.length; i++) {
      var img = imgs[i];
      if (!isVisibleImage(img)) continue;
      if (!img.dataset || img.dataset.frankPageId !== pageId) continue;
      var r = img.getBoundingClientRect();
      var area = r.width * r.height;
      if (area > bestScore) {
        bestScore = area;
        best = img;
      }
    }
    if (best) return best;

    // 2. Fallback: single translated image (overlay applied but id was lost)
    var translated = document.querySelectorAll('img[data-frank-translated="true"]');
    if (translated.length === 1) {
      console.log('[FeedbackMarks] findImageByPageId fallback: single translated img');
      return translated[0];
    }

    // 3. Fallback: largest visible image in viewport (Kindle dominant page image)
    var bestArea = 0;
    for (var j = 0; j < imgs.length; j++) {
      var im = imgs[j];
      var rect = im.getBoundingClientRect();
      if (rect.width < 100 || rect.height < 100) continue;
      if (rect.bottom < 0 || rect.top > window.innerHeight) continue;
      if (rect.right < 0 || rect.left > window.innerWidth) continue;
      var a = rect.width * rect.height;
      if (a > bestArea) { bestArea = a; best = im; }
    }
    if (best) {
      console.log('[FeedbackMarks] findImageByPageId fallback: largest visible img (' + bestArea + 'px²)');
      return best;
    }

    // 4. Same for canvas elements (Kindle sometimes uses canvas)
    var canvases = document.querySelectorAll('canvas');
    for (var k = 0; k < canvases.length; k++) {
      var c = canvases[k];
      var cr = c.getBoundingClientRect();
      if (cr.width < 100 || cr.height < 100) continue;
      if (cr.bottom < 0 || cr.top > window.innerHeight) continue;
      if (cr.right < 0 || cr.left > window.innerWidth) continue;
      var ca = cr.width * cr.height;
      if (ca > bestArea) { bestArea = ca; best = c; }
    }
    if (best) {
      console.log('[FeedbackMarks] findImageByPageId fallback: canvas (' + bestArea + 'px²)');
    }
    return best;
  }
  function markStyle(kind) {
    return { border: 'rgba(156,39,176,0.98)', fill: 'rgba(156,39,176,0.14)', dash: false, label: 'TXT' };
  }
  function detectionStyle(regionKind) {
    if (regionKind === 'artwork_text') {
      return { border: 'rgba(255,235,59,0.7)', fill: 'rgba(255,235,59,0.15)', label: 'AT' };
    }
    if (regionKind === 'sfx') {
      return { border: 'rgba(76,175,80,0.7)', fill: 'rgba(76,175,80,0.15)', label: 'SFX' };
    }
    return { border: 'rgba(0,188,212,0.7)', fill: 'rgba(0,188,212,0.15)', label: 'B' };
  }
  function clearFeedbackMarks() {
    marksLayer.innerHTML = '';
    renderedFeedbackMarks = [];
  }
  function renderFeedbackMarks() {
    console.log('[FeedbackMarks] renderFeedbackMarks called, marks count: ' + (Array.isArray(feedbackMarks) ? feedbackMarks.length : 0) + ', editMode: ' + editMode + ', enabled: ' + feedbackMarksEnabled);
    if (!editMode || !feedbackMarksEnabled || !Array.isArray(feedbackMarks) || feedbackMarks.length === 0) {
      clearFeedbackMarks();
      marksLayer.style.display = 'none';
      return;
    }
    clearFeedbackMarks();
    marksLayer.style.display = 'block';
    var imagesFound = 0, marksRendered = 0;
    for (var i = 0; i < feedbackMarks.length; i++) {
      var mark = feedbackMarks[i];
      if (!mark || typeof mark !== 'object') continue;
      var anchorPageId = mark.anchorPageId || mark.pageId;
      var img = findImageByPageId(anchorPageId);
      if (!img) {
        console.warn('[FeedbackMarks] No image found for pageId: ' + anchorPageId);
        continue;
      }
      imagesFound++;
      var r = img.getBoundingClientRect();
      if (!r || r.width < 10 || r.height < 10) continue;
      var x = Number(mark.x || 0);
      var y = Number(mark.y || 0);
      var w = Number(mark.w || 0);
      var h = Number(mark.h || 0);
      if (!isFinite(x) || !isFinite(y) || !isFinite(w) || !isFinite(h)) continue;
      x = Math.max(0, Math.min(1, x));
      y = Math.max(0, Math.min(1, y));
      w = Math.max(0.005, Math.min(1, w));
      h = Math.max(0.005, Math.min(1, h));

      var px = r.left + (x * r.width);
      var py = r.top + (y * r.height);
      var pw = Math.max(10, w * r.width);
      var ph = Math.max(10, h * r.height);
      var isMarked = !!mark.marked;
      var box = document.createElement('div');

      if (isMarked) {
        var style = markStyle(mark.type);
        box.style.cssText =
          'position:fixed; pointer-events:auto; box-sizing:border-box; cursor:pointer;' +
          'left:' + px + 'px; top:' + py + 'px; width:' + pw + 'px; height:' + ph + 'px;' +
          'border:2px ' + (style.dash ? 'dashed' : 'solid') + ' ' + style.border + ';' +
          'background:' + style.fill + '; border-radius:4px;';
        var badge = document.createElement('div');
        badge.textContent = style.label;
        badge.style.cssText =
          'position:absolute; left:-1px; top:-18px; pointer-events:none;' +
          'background:' + style.border + '; color:#fff; border-radius:3px;' +
          'font:700 10px/1 sans-serif; padding:2px 4px;';
        box.appendChild(badge);
      } else {
        var ds = detectionStyle(mark.regionKind || 'bubble');
        box.style.cssText =
          'position:fixed; pointer-events:auto; box-sizing:border-box; cursor:pointer;' +
          'left:' + px + 'px; top:' + py + 'px; width:' + pw + 'px; height:' + ph + 'px;' +
          'border:1px dashed ' + ds.border + ';' +
          'background:' + ds.fill + '; border-radius:4px;';
        if (mark.ocrText) box.title = mark.ocrText;
        var dlabel = document.createElement('div');
        dlabel.textContent = ds.label;
        dlabel.style.cssText =
          'position:absolute; left:2px; top:2px; pointer-events:none;' +
          'color:' + ds.border + '; font:700 8px/1 sans-serif; opacity:0.8;';
        box.appendChild(dlabel);
      }
      // Store mark data on the DOM element for click handlers
      box.dataset.frankMarkPageId = mark.pageId || '';
      box.dataset.frankMarkRegionId = mark.regionId || '';
      box.dataset.frankMarkAnchorPageId = anchorPageId || '';
      box.dataset.frankMarkMarked = isMarked ? 'true' : 'false';
      marksLayer.appendChild(box);
      marksRendered++;

      renderedFeedbackMarks.push({
        anchorPageId: String(anchorPageId || ''),
        pageId: String(mark.pageId || ''),
        regionId: String(mark.regionId || ''),
        marked: isMarked,
        left: px,
        top: py,
        right: px + pw,
        bottom: py + ph,
        area: pw * ph
      });
    }
    console.log('[FeedbackMarks] Done: ' + imagesFound + ' images found, ' + marksRendered + ' marks rendered');
  }
  function scheduleFeedbackMarksRender() {
    if (marksRaf !== null) return;
    marksRaf = window.requestAnimationFrame(function() {
      marksRaf = null;
      renderFeedbackMarks();
    });
  }
  function findFeedbackMarkAt(clientX, clientY, anchorPageId) {
    var best = null;
    var bestArea = Infinity;
    for (var i = 0; i < renderedFeedbackMarks.length; i++) {
      var m = renderedFeedbackMarks[i];
      if (!m) continue;
      if (anchorPageId && m.anchorPageId && m.anchorPageId !== anchorPageId) continue;
      var pad = 6;
      if (clientX < (m.left - pad) || clientX > (m.right + pad) || clientY < (m.top - pad) || clientY > (m.bottom + pad)) {
        continue;
      }
      var area = Number(m.area || 0);
      if (!isFinite(area) || area <= 0) area = 1;
      if (!best || area < bestArea) {
        best = m;
        bestArea = area;
      }
    }
    return best;
  }
  // Single tap/click on a mark box: open edit_translation dialog
  marksLayer.addEventListener('click', function(e) {
    if (!editMode) return;
    var box = e.target.closest('[data-frank-mark-region-id]');
    if (!box) return;
    e.preventDefault();
    e.stopPropagation();
    var pageId = box.dataset.frankMarkPageId || null;
    var regionId = box.dataset.frankMarkRegionId || null;
    var img = findImageByPageId(box.dataset.frankMarkAnchorPageId);
    if (!img) return;
    var r = img.getBoundingClientRect();
    var br = box.getBoundingClientRect();
    var xNorm = ((br.left + br.width / 2) - r.left) / r.width;
    var yNorm = ((br.top + br.height / 2) - r.top) / r.height;
    window.flutter_inappwebview.callHandler('onOverlayEditAction', {
      action: 'edit_translation',
      pageId: pageId,
      regionId: regionId,
      xNorm: Math.max(0, Math.min(1, xNorm)),
      yNorm: Math.max(0, Math.min(1, yNorm))
    });
  }, true);

  // Right-click in edit mode on existing mark box → edit translation
  document.addEventListener('contextmenu', function(e) {
    if (!editMode) return;
    e.preventDefault();
    e.stopPropagation();
    if (isUiTarget(e.target)) return;
    var box = e.target.closest ? e.target.closest('[data-frank-mark-region-id]') : null;
    if (!box) return;
    var pageId = box.dataset.frankMarkPageId || null;
    var regionId = box.dataset.frankMarkRegionId || null;
    var img = findImageByPageId(box.dataset.frankMarkAnchorPageId);
    if (!img) return;
    var r = img.getBoundingClientRect();
    var br = box.getBoundingClientRect();
    var xNorm = ((br.left + br.width / 2) - r.left) / r.width;
    var yNorm = ((br.top + br.height / 2) - r.top) / r.height;
    window.flutter_inappwebview.callHandler('onOverlayEditAction', {
      action: 'edit_translation',
      pageId: pageId,
      regionId: regionId,
      xNorm: Math.max(0, Math.min(1, xNorm)),
      yNorm: Math.max(0, Math.min(1, yNorm))
    });
  }, true);
  window.addEventListener('resize', scheduleFeedbackMarksRender);
  document.addEventListener('scroll', scheduleFeedbackMarksRender, true);
  window.setInterval(function() {
    if (editMode && feedbackMarksEnabled && feedbackMarks.length > 0) {
      scheduleFeedbackMarksRender();
    }
  }, 350);

  var backBtn = document.getElementById('__frankBack');
  var autoBtn = document.getElementById('__frankAuto');
  var pipeBtn = document.getElementById('__frankPipeline');
  var transBtn = document.getElementById('__frankTranslate');
  var feedbackBtn = document.getElementById('__frankFeedback');
  var saveEditsBtn = document.getElementById('__frankSaveEdits');
  var cancelEditsBtn = document.getElementById('__frankCancelEdits');
  var toggleOrigBtn = document.getElementById('__frankToggleOrig');
  var reloadBtn = document.getElementById('__frankReload');
  var clearCacheBtn = document.getElementById('__frankClearCache');
  var copyDbgBtn = document.getElementById('__frankCopyDbg');
  if (backBtn) backBtn.style.cssText = btnStyle;
  if (autoBtn) autoBtn.style.cssText = btnStyle;
  if (pipeBtn) pipeBtn.style.cssText = btnStyle + 'display:none;';
  if (transBtn) transBtn.style.cssText = btnStyle;
  if (toggleOrigBtn) toggleOrigBtn.style.cssText = btnStyle;
  if (feedbackBtn) feedbackBtn.style.cssText = btnStyle;
  if (saveEditsBtn) saveEditsBtn.style.cssText = btnStyle + 'display:none;';
  if (cancelEditsBtn) cancelEditsBtn.style.cssText = btnStyle + 'display:none;';
  if (reloadBtn) reloadBtn.style.cssText = btnStyle;
  if (clearCacheBtn) clearCacheBtn.style.cssText = btnStyle;
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
  if (autoBtn) autoBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'toggle_auto');
  });
  if (pipeBtn) pipeBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'toggle_pipeline');
  });
  if (transBtn) transBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'translate');
  });
  if (feedbackBtn) feedbackBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'toggle_feedback_mode');
  });
  if (saveEditsBtn) saveEditsBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'save_feedback_edits');
  });
  if (cancelEditsBtn) cancelEditsBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'cancel_feedback_edits');
  });
  if (toggleOrigBtn) toggleOrigBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'toggle_original');
  });
  if (reloadBtn) reloadBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'reload');
  });
  if (clearCacheBtn) clearCacheBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'clear_cache');
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
  // Toggle original/translated for visible images.
  // mode: 'kindle' toggles all translated imgs, 'webtoon' toggles only viewport-visible ones.
  window.__frankToggleOriginal = function(mode) {
    var imgs = document.querySelectorAll('img[data-frank-translated]');
    var toggled = 0;
    for (var i = 0; i < imgs.length; i++) {
      var img = imgs[i];
      if (!img.dataset.frankOriginalSrc) continue;
      if (mode === 'webtoon') {
        var r = img.getBoundingClientRect();
        var inView = r.bottom > 0 && r.top < window.innerHeight &&
                     r.right > 0 && r.left < window.innerWidth &&
                     r.width > 40 && r.height > 40;
        if (!inView) continue;
      }
      if (img.dataset.frankTranslated === 'true') {
        img.dataset.frankTranslatedSrc = img.dataset.frankTranslatedSrc || img.src;
        img.src = img.dataset.frankOriginalSrc;
        img.dataset.frankTranslated = 'false';
      } else {
        var ts = img.dataset.frankTranslatedSrc;
        if (ts) {
          img.src = ts;
          img.dataset.frankTranslated = 'true';
        }
      }
      toggled++;
    }
    return toggled;
  };
  window.__frankSetFeedbackState = function(enabled, visibleControl) {
    var btn = document.getElementById('__frankFeedback');
    if (!btn) return;
    btn.style.display = visibleControl ? '' : 'none';
    btn.textContent = 'Feedback: ' + (enabled ? 'ON' : 'OFF');
    btn.style.borderColor = enabled ? '#81c784' : 'rgba(255,255,255,0.3)';
    btn.style.color = enabled ? '#81c784' : '#fff';
  };
  window.__frankSetFeedbackActions = function(showActions, dirtyCount) {
    var saveBtn = document.getElementById('__frankSaveEdits');
    var cancelBtn = document.getElementById('__frankCancelEdits');
    if (!saveBtn || !cancelBtn) return;
    if (!showActions) {
      saveBtn.style.display = 'none';
      cancelBtn.style.display = 'none';
      return;
    }
    saveBtn.style.display = '';
    cancelBtn.style.display = '';
    var n = Number(dirtyCount || 0);
    saveBtn.textContent = n > 0 ? ('Save (' + n + ')') : 'Save';
    saveBtn.style.borderColor = n > 0 ? '#4caf50' : 'rgba(255,255,255,0.3)';
    saveBtn.style.color = n > 0 ? '#4caf50' : '#fff';
    cancelBtn.textContent = n > 0 ? ('Cancel (' + n + ')') : 'Cancel';
    cancelBtn.style.borderColor = n > 0 ? '#ef9a9a' : 'rgba(255,255,255,0.3)';
    cancelBtn.style.color = n > 0 ? '#ef9a9a' : '#fff';
  };
  window.__frankSetEditMode = function(enabled) {
    editMode = !!enabled;
    scheduleFeedbackMarksRender();
  };
  window.__frankSetFeedbackMarks = function(payload) {
    if (!payload || typeof payload !== 'object') {
      feedbackMarksEnabled = false;
      feedbackMarks = [];
      scheduleFeedbackMarksRender();
      return;
    }
    feedbackMarksEnabled = !!payload.enabled;
    feedbackMarks = Array.isArray(payload.marks) ? payload.marks : [];
    scheduleFeedbackMarksRender();
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
            _translateVisiblePages(force: true);
            break;
          case 'toggle_feedback_mode':
            _toggleOverlayEditMode();
            break;
          case 'save_feedback_edits':
            _saveFeedbackEdits();
            break;
          case 'cancel_feedback_edits':
            _cancelFeedbackEdits();
            break;
          case 'toggle_original':
            _toggleOriginalTranslated();
            break;
          case 'reload':
            _reloadPage();
            break;
          case 'clear_cache':
            _clearCache();
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

    controller.addJavaScriptHandler(
      handlerName: 'onOverlayEditAction',
      callback: (args) {
        if (args.isEmpty) return null;
        final raw = args[0];
        if (raw is! Map) return null;
        final action = raw['action'] as String?;
        final pageId = raw['pageId'] as String?;
        final regionId = raw['regionId'] as String?;
        final xNorm = (raw['xNorm'] as num?)?.toDouble();
        final yNorm = (raw['yNorm'] as num?)?.toDouble();
        if (action == null || xNorm == null || yNorm == null) return null;
        _handleOverlayEditAction(
          action: action,
          pageId: pageId,
          regionId: regionId,
          xNorm: xNorm,
          yNorm: yNorm,
        );
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

    // Persist per-title pipeline preference
    final asin = _currentAsin;
    if (asin != null && asin.isNotEmpty) {
      SharedPreferences.getInstance().then((prefs) {
        prefs.setString('kindle_pipeline_$asin', _kindlePipeline);
      });
    }

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

  /// Toggle between original and translated images in the WebView.
  void _toggleOriginalTranslated() {
    final controller = _webController;
    if (controller == null) return;
    final mode = _jsBridge.activeStrategy?.siteName == 'kindle'
        ? 'kindle'
        : 'webtoon';
    controller.evaluateJavascript(
      source:
          "if(window.__frankToggleOriginal) window.__frankToggleOriginal('$mode');",
    );
    _updateInPageStatus(
      'Toggled original/translated',
      clearAfter: const Duration(seconds: 1),
    );
  }

  /// Full reload of the WebView page.
  void _reloadPage() {
    _webController?.evaluateJavascript(source: 'location.reload();');
  }

  Future<void> _clearCache() async {
    _updateInPageStatus('Clearing cache...');
    final cache = ref.read(cacheServiceProvider);
    final count = await cache.clearAll();
    // Also clear in-memory job state so pages get re-submitted
    ref.read(jobsProvider.notifier).clearAll();
    _updateInPageStatus(
      'Cleared $count cached pages',
      clearAfter: const Duration(seconds: 3),
    );
  }

  void _toggleOverlayEditMode() {
    if (_overlayEditMode && _dirtyMetadataPageIds.isNotEmpty) {
      _updateInPageStatus(
        'Save or Cancel edits before leaving feedback mode',
        clearAfter: const Duration(seconds: 3),
      );
      return;
    }
    _overlayEditMode = !_overlayEditMode;
    _syncEditModeButtonState();
    _syncFeedbackActionButtons();
    _syncEditModeToPage();
    _syncFeedbackMarksOverlay();
    _updateInPageStatus(
      _overlayEditMode ? 'Feedback mode ON' : 'Feedback mode OFF',
      clearAfter: const Duration(seconds: 2),
    );
  }

  void _syncEditModeButtonState() {
    final controller = _webController;
    if (controller == null) return;
    final isSupported =
        _jsBridge.activeStrategy?.siteName == 'kindle' ||
        _jsBridge.activeStrategy?.siteName == 'webtoon';
    controller.evaluateJavascript(
      source:
          'if(window.__frankSetFeedbackState) window.__frankSetFeedbackState(${_overlayEditMode ? 'true' : 'false'}, ${isSupported ? 'true' : 'false'});',
    );
  }

  void _syncEditModeToPage() {
    final controller = _webController;
    if (controller == null) return;
    controller.evaluateJavascript(
      source:
          'if(window.__frankSetEditMode) window.__frankSetEditMode(${_overlayEditMode ? 'true' : 'false'});',
    );
    _syncFeedbackMarksOverlay();
  }

  void _syncFeedbackActionButtons() {
    final controller = _webController;
    if (controller == null) return;
    final showActions = _overlayEditMode;
    final dirtyCount = _dirtyMetadataPageIds.length;
    controller.evaluateJavascript(
      source:
          'if(window.__frankSetFeedbackActions) window.__frankSetFeedbackActions(${showActions ? 'true' : 'false'}, $dirtyCount);',
    );
  }

  List<Map<String, Object?>> _extractFeedbackMarks({
    required String metadataPageId,
    required String anchorPageId,
    required double xOffset,
    required double xScale,
  }) {
    final entry = _metadataByPageId[metadataPageId];
    if (entry == null) return const <Map<String, Object?>>[];
    final metadata = entry['metadata'];
    if (metadata is! Map) return const <Map<String, Object?>>[];
    final regionsRaw = metadata['regions'];
    if (regionsRaw is! List) return const <Map<String, Object?>>[];

    final marks = <Map<String, Object?>>[];
    for (var i = 0; i < regionsRaw.length; i++) {
      final regionRaw = regionsRaw[i];
      if (regionRaw is! Map) continue;
      final region = Map<String, dynamic>.from(regionRaw);
      final userRaw = region['user'];
      final user = userRaw is Map
          ? Map<String, dynamic>.from(userRaw)
          : <String, dynamic>{};

      final manual = ((user['manual_translation'] as String?) ?? '').trim();
      final String? type = manual.isNotEmpty ? 'manual_translation' : null;
      final bool marked = type != null;

      final norm = region['bbox_norm'];
      if (norm is! List || norm.length != 4) continue;
      final x1 = (norm[0] as num?)?.toDouble();
      final y1 = (norm[1] as num?)?.toDouble();
      final x2 = (norm[2] as num?)?.toDouble();
      final y2 = (norm[3] as num?)?.toDouble();
      if (x1 == null || y1 == null || x2 == null || y2 == null) continue;

      final localX1 = x1.clamp(0.0, 1.0).toDouble();
      final localY1 = y1.clamp(0.0, 1.0).toDouble();
      final localX2 = x2.clamp(0.0, 1.0).toDouble();
      final localY2 = y2.clamp(0.0, 1.0).toDouble();
      if (localX2 <= localX1 || localY2 <= localY1) continue;

      final globalX1 = (xOffset + (localX1 * xScale))
          .clamp(0.0, 1.0)
          .toDouble();
      final globalX2 = (xOffset + (localX2 * xScale))
          .clamp(0.0, 1.0)
          .toDouble();
      if (globalX2 <= globalX1) continue;

      final regionId = (region['id'] as String?) ?? 'idx-$i';
      final regionKind = (region['kind'] as String?) ?? 'bubble';
      final ocrText = (region['ocr_text'] as String?) ?? '';
      marks.add({
        'anchorPageId': anchorPageId,
        'pageId': metadataPageId,
        'regionId': regionId,
        'type': type,
        'marked': marked,
        'regionKind': regionKind,
        'ocrText': ocrText,
        'x': globalX1,
        'y': localY1,
        'w': globalX2 - globalX1,
        'h': localY2 - localY1,
      });
    }
    return marks;
  }

  List<Map<String, Object?>> _collectFeedbackMarksForCurrentView() {
    final site = _jsBridge.activeStrategy?.siteName;
    if (site == 'kindle') {
      final currentPageId = _currentKindlePageId;
      if (currentPageId == null || currentPageId.isEmpty) {
        return const <Map<String, Object?>>[];
      }
      if (currentPageId.endsWith('-spread')) {
        final leftId = '$currentPageId-L';
        final rightId = '$currentPageId-R';
        _ensureMetadataForPage(leftId);
        _ensureMetadataForPage(rightId);
        final marks = <Map<String, Object?>>[];
        marks.addAll(
          _extractFeedbackMarks(
            metadataPageId: leftId,
            anchorPageId: currentPageId,
            xOffset: 0.0,
            xScale: 0.5,
          ),
        );
        marks.addAll(
          _extractFeedbackMarks(
            metadataPageId: rightId,
            anchorPageId: currentPageId,
            xOffset: 0.5,
            xScale: 0.5,
          ),
        );
        return marks;
      }
      _ensureMetadataForPage(currentPageId);
      return _extractFeedbackMarks(
        metadataPageId: currentPageId,
        anchorPageId: currentPageId,
        xOffset: 0.0,
        xScale: 1.0,
      );
    }

    if (site == 'webtoon') {
      final marks = <Map<String, Object?>>[];
      for (final pageId in _metadataByPageId.keys) {
        if (!pageId.startsWith('wt-')) continue;
        marks.addAll(
          _extractFeedbackMarks(
            metadataPageId: pageId,
            anchorPageId: pageId,
            xOffset: 0.0,
            xScale: 1.0,
          ),
        );
      }
      return marks;
    }
    return const <Map<String, Object?>>[];
  }

  void _syncFeedbackMarksOverlay() {
    final controller = _webController;
    if (controller == null) return;
    final rawMarks = _overlayEditMode
        ? _collectFeedbackMarksForCurrentView()
        : const <Map<String, Object?>>[];
    // Sort: unmarked detections first (underneath), marked regions last (on top)
    if (rawMarks.isNotEmpty) {
      rawMarks.sort((a, b) {
        final aMarked = a['marked'] == true ? 1 : 0;
        final bMarked = b['marked'] == true ? 1 : 0;
        return aMarked.compareTo(bMarked);
      });
    }
    final payload = <String, Object?>{
      'enabled': _overlayEditMode,
      'marks': rawMarks,
    };
    controller.evaluateJavascript(
      source:
          'if(window.__frankSetFeedbackMarks) window.__frankSetFeedbackMarks(${jsonEncode(payload)});',
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
    _setKindleOverlayPending(false, reason: 'cancel_jobs');
    _kindlePrefetch.dispose();
    _recreateKindlePrefetchForSettings();
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

  /// Trigger Kindle prefetch via background webview (Linux-only).
  /// On non-Linux platforms, this is a no-op.
  void _triggerKindlePrefetch(int pageIndex, String navIntent) {
    if (!Platform.isLinux) return;
    if (_jsBridge.activeStrategy?.siteName != 'kindle') return;
    if (!ref.read(settingsProvider).autoTranslate) return;
    _syncKindlePrefetchConfig();
    if (_kindlePrefetchWindow <= 0) {
      if (!_kindlePrefetch.disposed) {
        _kindlePrefetch.dispose();
      }
      return;
    }
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

  void _ensureMetadataForPage(String pageId) {
    if (_metadataByPageId.containsKey(pageId)) return;
    unawaited(_loadMetadataForPage(pageId));
  }

  Map<String, dynamic> _deepCopyMap(Map<String, dynamic> value) {
    return Map<String, dynamic>.from(
      jsonDecode(jsonEncode(value)) as Map<String, dynamic>,
    );
  }

  Future<void> _loadMetadataForPage(
    String pageId, {
    bool force = false,
    int retryCount = 0,
  }) async {
    if (!force &&
        (_metadataByPageId.containsKey(pageId) ||
            _metadataLoadingPageIds.contains(pageId))) {
      return;
    }
    if (force && _metadataLoadingPageIds.contains(pageId)) return;
    final job = ref.read(jobsProvider)[pageId];
    if (job == null) {
      return;
    }
    if (!job.isComplete) {
      return;
    }
    final sourceHash = job.sourceHash;
    final pipeline = job.pipeline;
    if (sourceHash == null || sourceHash.isEmpty || pipeline == null) {
      debugPrint(
        '[Feedback] Missing hash/pipeline for $pageId '
        '(hash=${sourceHash?.substring(0, 12) ?? 'null'}, pipeline=$pipeline)',
      );
      return;
    }
    _metadataLoadingPageIds.add(pageId);
    try {
      // Server is ground truth — always fetch from server first.
      // Local SQLite cache is only a fallback for network errors.
      final api = ref.read(apiServiceProvider);
      final settings = ref.read(settingsProvider);
      final cache = ref.read(cacheServiceProvider);
      bool serverOk = false;
      try {
        final resp = await api.getCacheMetadataByHash(
          settings: settings,
          pipeline: pipeline,
          sourceHash: sourceHash,
        );
        final metadata = resp['metadata'];
        if (metadata is! Map) {
          // Server returned response but no usable metadata — expire local
          await cache.updateMetadata(sourceHash, pipeline, null);
          return;
        }
        final regions = metadata['regions'];
        debugPrint(
          '[Feedback] Server metadata for $pageId: '
          '${regions is List ? regions.length : 0} regions',
        );
        _metadataByPageId[pageId] = {
          'pipeline': pipeline,
          'sourceHash': sourceHash,
          'contentHash': resp['content_hash'],
          'renderHash': resp['render_hash'],
          'metadata': Map<String, dynamic>.from(metadata),
        };
        if (force) {
          _metadataOriginalByPageId.remove(pageId);
          _dirtyMetadataPageIds.remove(pageId);
          _syncFeedbackActionButtons();
        }
        _syncFeedbackMarksOverlay();
        serverOk = true;

        // Persist to local cache for future sessions
        try {
          await cache.updateMetadata(sourceHash, pipeline, jsonEncode(resp));
        } catch (_) {}
      } on ApiException catch (e) {
        if (e.statusCode == 404) {
          // Server doesn't have this page — expire stale local metadata
          debugPrint(
            '[Feedback] Server 404 for $pageId — expiring stale local metadata',
          );
          await cache.updateMetadata(sourceHash, pipeline, null);
        } else {
          rethrow;
        }
      }

      if (!serverOk) return;
    } catch (e) {
      debugPrint(
        '[Feedback] Failed to load metadata for $pageId (attempt ${retryCount + 1}): $e',
      );
      // Retry once after a delay — server cache may still be flushing after
      // the worker stored the result (race between Redis notify and disk I/O).
      if (retryCount < 1 && mounted) {
        Future.delayed(const Duration(seconds: 2), () {
          if (!mounted) return;
          _loadMetadataForPage(
            pageId,
            force: force,
            retryCount: retryCount + 1,
          );
        });
      }
    } finally {
      _metadataLoadingPageIds.remove(pageId);
    }
  }

  Future<void> _handleOverlayEditAction({
    required String action,
    required String? pageId,
    required String? regionId,
    required double xNorm,
    required double yNorm,
  }) async {
    if (!_overlayEditMode) return;
    var targetPageId = pageId;
    if (targetPageId == null || targetPageId.isEmpty) {
      targetPageId = _currentKindlePageId;
    }
    if (targetPageId == null || targetPageId.isEmpty) return;

    // Kindle spread overlays are stitched from L/R jobs.
    if (targetPageId.endsWith('-spread')) {
      final isRightHalf = xNorm >= 0.5;
      final halfPageId = '$targetPageId-${isRightHalf ? 'R' : 'L'}';
      final localX = isRightHalf ? ((xNorm - 0.5) * 2.0) : (xNorm * 2.0);
      await _applyMetadataActionToPage(
        pageId: halfPageId,
        action: action,
        regionId: regionId,
        xNorm: localX.clamp(0.0, 1.0),
        yNorm: yNorm.clamp(0.0, 1.0),
      );
      return;
    }

    await _applyMetadataActionToPage(
      pageId: targetPageId,
      action: action,
      regionId: regionId,
      xNorm: xNorm.clamp(0.0, 1.0),
      yNorm: yNorm.clamp(0.0, 1.0),
    );
  }

  int _findRegionById(List<dynamic> regions, String regionId) {
    for (var i = 0; i < regions.length; i++) {
      final r = regions[i];
      if (r is! Map) continue;
      if ((r['id'] as String?) == regionId) return i;
    }
    return -1;
  }

  int _findRegionAt(List<dynamic> regions, double xNorm, double yNorm) {
    for (var i = 0; i < regions.length; i++) {
      final r = regions[i];
      if (r is! Map) continue;
      final norm = r['bbox_norm'];
      if (norm is! List || norm.length != 4) continue;
      final x1 = (norm[0] as num?)?.toDouble() ?? -1;
      final y1 = (norm[1] as num?)?.toDouble() ?? -1;
      final x2 = (norm[2] as num?)?.toDouble() ?? -1;
      final y2 = (norm[3] as num?)?.toDouble() ?? -1;
      if (xNorm >= x1 && xNorm <= x2 && yNorm >= y1 && yNorm <= y2) {
        return i;
      }
    }
    return -1;
  }

  Future<void> _applyMetadataActionToPage({
    required String pageId,
    required String action,
    required String? regionId,
    required double xNorm,
    required double yNorm,
  }) async {
    await _loadMetadataForPage(pageId);
    final entry = _metadataByPageId[pageId];
    if (entry == null) {
      _updateInPageStatus(
        'No metadata for $pageId',
        clearAfter: const Duration(seconds: 2),
      );
      return;
    }
    final metadata = entry['metadata'];
    if (metadata is! Map<String, dynamic>) return;
    _metadataOriginalByPageId.putIfAbsent(pageId, () => _deepCopyMap(metadata));
    final regionsRaw = metadata['regions'];
    final regions = (regionsRaw is List) ? regionsRaw : <dynamic>[];
    if (metadata['regions'] is! List) {
      metadata['regions'] = regions;
    }

    final idx = (regionId != null && regionId.isNotEmpty)
        ? _findRegionById(regions, regionId)
        : _findRegionAt(regions, xNorm, yNorm);
    Map<String, dynamic>? region =
        (idx >= 0 && regions[idx] is Map<String, dynamic>)
        ? Map<String, dynamic>.from(regions[idx] as Map<String, dynamic>)
        : null;

    if (action == 'undo_mark') {
      if (region == null) {
        _updateInPageStatus(
          'No existing mark at click',
          clearAfter: const Duration(seconds: 2),
        );
        return;
      }
      final user = Map<String, dynamic>.from(
        (region['user'] as Map?)?.cast<String, dynamic>() ??
            <String, dynamic>{
              'manual_translation': '',
            },
      );
      user['manual_translation'] = '';
      region['user'] = user;
      if (idx >= 0) {
        regions[idx] = region;
      }
    } else if (action == 'edit_translation') {
      if (region == null) {
        _updateInPageStatus(
          'No text region at click',
          clearAfter: const Duration(seconds: 2),
        );
        return;
      }
      final user = Map<String, dynamic>.from(
        (region['user'] as Map?)?.cast<String, dynamic>() ??
            <String, dynamic>{
              'manual_translation': '',
            },
      );

      var initial = (user['manual_translation'] as String?) ?? '';
      if (initial.isEmpty) {
        final transformed = region['transformed'];
        if (transformed is Map) {
          initial = (transformed['value'] as String?) ?? '';
        }
      }
      final edited = await _showTranslationEditDialog(initial);
      if (edited == null) return;
      user['manual_translation'] = edited.trim();

      region['user'] = user;
      if (idx >= 0) {
        regions[idx] = region;
      }
    }

    // Stage edit locally until user explicitly saves.
    _metadataByPageId[pageId] = entry;
    _metadataByPageId[pageId]!['metadata'] = metadata;
    _dirtyMetadataPageIds.add(pageId);
    _syncFeedbackActionButtons();
    _syncFeedbackMarksOverlay();
    _updateInPageStatus(
      'Edit staged. Use Save or Cancel.',
      clearAfter: const Duration(seconds: 2),
    );
  }

  Future<String?> _showTranslationEditDialog(String initial) async {
    final controller = TextEditingController(text: initial);
    final value = await showDialog<String>(
      context: context,
      builder: (ctx) {
        return AlertDialog(
          title: const Text('Edit Translation'),
          content: TextField(
            controller: controller,
            maxLines: 5,
            autofocus: true,
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(ctx).pop(),
              child: const Text('Cancel'),
            ),
            TextButton(
              onPressed: () => Navigator.of(ctx).pop(controller.text),
              child: const Text('Save'),
            ),
          ],
        );
      },
    );
    controller.dispose();
    return value;
  }

  Future<void> _saveFeedbackEdits() async {
    if (_dirtyMetadataPageIds.isEmpty) {
      _updateInPageStatus(
        'No pending edits',
        clearAfter: const Duration(seconds: 2),
      );
      return;
    }
    final api = ref.read(apiServiceProvider);
    final settings = ref.read(settingsProvider);
    final dirtyPages = _dirtyMetadataPageIds.toList();
    var failed = 0;
    final savedImages = <String, Uint8List>{};

    _updateInPageStatus('Saving ${dirtyPages.length} page(s)...');

    for (final pageId in dirtyPages) {
      final entry = _metadataByPageId[pageId];
      if (entry == null) {
        failed++;
        continue;
      }
      final pipeline = entry['pipeline'] as String?;
      final sourceHash = entry['sourceHash'] as String?;
      var contentHash = entry['contentHash'] as String?;
      final metadata = entry['metadata'] as Map<String, dynamic>?;
      if (pipeline == null ||
          sourceHash == null ||
          sourceHash.isEmpty ||
          metadata == null) {
        failed++;
        continue;
      }
      try {
        final freshImage = await _patchAndRerender(
          api: api,
          settings: settings,
          pageId: pageId,
          pipeline: pipeline,
          sourceHash: sourceHash,
          metadata: metadata,
          contentHash: contentHash,
        );
        if (freshImage != null) {
          savedImages[pageId] = freshImage;
        }
        _invalidateEditedPageJobs(pageId);
        await _loadMetadataForPage(pageId, force: true);
        _dirtyMetadataPageIds.remove(pageId);
        _metadataOriginalByPageId.remove(pageId);
      } catch (e) {
        failed++;
        debugPrint('[Feedback] Save failed for $pageId: $e');
      }
    }

    // Exit feedback mode after save
    _overlayEditMode = false;
    _syncEditModeButtonState();
    _syncFeedbackActionButtons();
    _syncEditModeToPage();
    _syncFeedbackMarksOverlay();
    if (failed == 0) {
      _updateInPageStatus(
        'Feedback saved',
        clearAfter: const Duration(seconds: 2),
      );
    } else {
      _updateInPageStatus(
        'Saved with $failed failure(s)',
        clearAfter: const Duration(seconds: 3),
      );
    }
    // Apply fresh rendered overlays directly — don't re-capture the page,
    // because the WebView is showing the old translated image whose hash
    // won't match the original source hash in the cache.
    final appliedSpreads = <String>{};
    for (final entry in savedImages.entries) {
      final pid = entry.key;
      // For spread halves (L/R), stitch and apply as spread.
      if (pid.endsWith('-L') || pid.endsWith('-R')) {
        final base = pid.substring(0, pid.length - 2);
        if (appliedSpreads.contains(base)) continue;
        final leftImg = savedImages['$base-L'];
        final rightImg = savedImages['$base-R'];
        if (leftImg != null && rightImg != null) {
          appliedSpreads.add(base);
          _applySpreadOverlay(base, leftImg, rightImg);
          continue;
        }
      }
      _applyOverlay(pid, entry.value);
    }
  }

  /// PATCH metadata, wait for rerender, fetch fresh image.
  /// Handles 409 Conflict (stale content hash) by reloading metadata from
  /// server, re-applying user edits, and retrying once.
  /// Handles rerender failure by attempting to re-fetch image (covers the
  /// case where v2 cache completed but Redis notification was lost).
  Future<Uint8List?> _patchAndRerender({
    required ApiService api,
    required dynamic settings,
    required String pageId,
    required String pipeline,
    required String sourceHash,
    required Map<String, dynamic> metadata,
    required String? contentHash,
    int attempt = 0,
  }) async {
    Map<String, dynamic> patchResp;
    try {
      patchResp = await api.patchCacheMetadataByHash(
        settings: settings,
        pipeline: pipeline,
        sourceHash: sourceHash,
        metadata: metadata,
        baseContentHash: contentHash,
      );
    } on ApiConflictException {
      if (attempt > 0) rethrow; // Only retry once

      debugPrint(
        '[Feedback] 409 Conflict for $pageId — '
        'reloading metadata and retrying',
      );

      // Reload fresh metadata from server to get current content hash
      final freshResp = await api.getCacheMetadataByHash(
        settings: settings,
        pipeline: pipeline,
        sourceHash: sourceHash,
      );
      final freshMeta = freshResp['metadata'] as Map<String, dynamic>?;
      final freshContentHash = freshResp['content_hash'] as String?;
      if (freshMeta == null) {
        throw Exception('Could not reload metadata for retry');
      }

      // Re-apply user edits on top of the fresh server metadata
      final mergedMeta = _mergeUserEdits(freshMeta, metadata);

      // Update local state with merged metadata
      _metadataByPageId[pageId] = {
        'pipeline': pipeline,
        'sourceHash': sourceHash,
        'contentHash': freshContentHash,
        'metadata': mergedMeta,
      };

      return _patchAndRerender(
        api: api,
        settings: settings,
        pageId: pageId,
        pipeline: pipeline,
        sourceHash: sourceHash,
        metadata: mergedMeta,
        contentHash: freshContentHash,
        attempt: 1,
      );
    }

    final rerenderJobId = patchResp['job_id'] as String?;
    if (rerenderJobId == null || rerenderJobId.isEmpty) {
      throw Exception('No rerender job_id in PATCH response');
    }

    try {
      await _waitForJobCompletion(rerenderJobId);
    } catch (e) {
      // Rerender failed or timed out — try fetching the image anyway.
      // The v2 cache may have completed even if Redis notification was lost,
      // or a previous render might still be usable.
      debugPrint(
        '[Feedback] Rerender $rerenderJobId failed ($e) — '
        'attempting image recovery',
      );
      try {
        final recovered = await _refreshLocalEditedCache(
          pageId: pageId,
          pipeline: pipeline,
          sourceHash: sourceHash,
        );
        if (recovered != null) {
          debugPrint('[Feedback] Recovered image for $pageId after failure');
          return recovered;
        }
      } catch (_) {}
      rethrow; // Surface the original error
    }

    return _refreshLocalEditedCache(
      pageId: pageId,
      pipeline: pipeline,
      sourceHash: sourceHash,
    );
  }

  /// Merge user edits from the local metadata onto fresh server metadata.
  /// Preserves user overrides (manual_translation) while
  /// picking up any server-side changes (new regions, updated transforms).
  Map<String, dynamic> _mergeUserEdits(
    Map<String, dynamic> serverMeta,
    Map<String, dynamic> localMeta,
  ) {
    final merged = Map<String, dynamic>.from(serverMeta);
    final serverRegions = serverMeta['regions'];
    final localRegions = localMeta['regions'];
    if (serverRegions is! List || localRegions is! List) return merged;

    // Index local regions by id for quick lookup
    final localById = <String, Map<String, dynamic>>{};
    for (final r in localRegions) {
      if (r is Map<String, dynamic>) {
        final id = r['id'] as String?;
        if (id != null) localById[id] = r;
      }
    }

    final mergedRegions = <Map<String, dynamic>>[];
    for (final sr in serverRegions) {
      if (sr is! Map<String, dynamic>) {
        mergedRegions.add(Map<String, dynamic>.from(sr));
        continue;
      }
      final id = sr['id'] as String?;
      final local = id != null ? localById[id] : null;
      if (local != null) {
        // Overlay user section from local edits onto server region
        final mergedRegion = Map<String, dynamic>.from(sr);
        final localUser = local['user'];
        if (localUser is Map) {
          mergedRegion['user'] = Map<String, dynamic>.from(localUser);
        }
        mergedRegions.add(mergedRegion);
      } else {
        mergedRegions.add(Map<String, dynamic>.from(sr));
      }
    }
    merged['regions'] = mergedRegions;
    return merged;
  }

  Future<Uint8List?> _refreshLocalEditedCache({
    required String pageId,
    required String pipeline,
    required String sourceHash,
  }) async {
    final api = ref.read(apiServiceProvider);
    final settings = ref.read(settingsProvider);
    final cache = ref.read(cacheServiceProvider);
    final fresh = await api.getCacheImageByHash(
      settings: settings,
      pipeline: pipeline,
      sourceHash: sourceHash,
    );
    final job = ref.read(jobsProvider)[pageId];
    await cache.store(
      hash: sourceHash,
      pipeline: pipeline,
      imageBytes: fresh,
      title: job?.title,
      chapter: job?.chapter,
      pageNumber: job?.pageNumber,
    );
    return fresh;
  }

  void _invalidateEditedPageJobs(String pageId) {
    final notifier = ref.read(jobsProvider.notifier);
    if (pageId.endsWith('-L') || pageId.endsWith('-R')) {
      final base = pageId.substring(0, pageId.length - 2);
      final left = '$base-L';
      final right = '$base-R';
      notifier.removeJob(left);
      notifier.removeJob(right);
      return;
    }
    notifier.removeJob(pageId);
  }

  void _cancelFeedbackEdits() {
    if (_dirtyMetadataPageIds.isEmpty) {
      _updateInPageStatus(
        'No pending edits',
        clearAfter: const Duration(seconds: 2),
      );
      return;
    }
    for (final pageId in _dirtyMetadataPageIds.toList()) {
      final original = _metadataOriginalByPageId[pageId];
      final current = _metadataByPageId[pageId];
      if (original != null && current != null) {
        current['metadata'] = _deepCopyMap(original);
      } else {
        _metadataByPageId.remove(pageId);
      }
      _metadataOriginalByPageId.remove(pageId);
    }
    _dirtyMetadataPageIds.clear();
    // Exit feedback mode after cancel
    _overlayEditMode = false;
    _syncEditModeButtonState();
    _syncFeedbackActionButtons();
    _syncEditModeToPage();
    _syncFeedbackMarksOverlay();
    _updateInPageStatus(
      'Feedback edits canceled',
      clearAfter: const Duration(seconds: 2),
    );
  }

  Future<void> _waitForJobCompletion(String jobId) async {
    final api = ref.read(apiServiceProvider);
    final settings = ref.read(settingsProvider);
    for (var i = 0; i < 60; i++) {
      final status = await api.getJobStatus(settings: settings, jobId: jobId);
      final s = status['status'] as String? ?? '';
      if (s == 'completed') return;
      if (s == 'failed') {
        throw Exception(status['error'] ?? 'rerender failed');
      }
      await Future.delayed(const Duration(milliseconds: 500));
    }
    throw Exception('rerender timeout');
  }

  void _refreshCurrentPageFromCache() {
    if (!mounted) return;
    if (_currentKindlePageId != null && _lastKindlePageInfo != null) {
      _capturePageImage(_currentKindlePageId!, _lastKindlePageInfo!);
      return;
    }
    if (_jsBridge.activeStrategy?.siteName == 'webtoon') {
      final visible = _detectedWebtoonPages.entries
          .where((e) => e.value['pageId'] is String)
          .toList();
      if (visible.isNotEmpty) {
        final last = visible.last.value;
        final pageId = last['pageId'] as String?;
        if (pageId != null) {
          _capturePageImage(pageId, last);
        }
      }
    }
  }

  /// Handle a page captured by the background webview prefetch manager.
  Future<void> _handleBgPrefetchedPage(
    Uint8List imageBytes,
    String pageMode,
  ) async {
    final sw = Stopwatch()..start();
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
      sw.stop();
      debugPrint(
        '[Perf] bg-prefetch cached hash=$hash ms=${sw.elapsedMilliseconds}',
      );
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
    sw.stop();
    debugPrint(
      '[Perf] bg-prefetch submitted id=$prefetchId mode=$pageMode '
      'bytes=${imageBytes.length} ms=${sw.elapsedMilliseconds}',
    );
  }

  /// Manual translate: submit the next batch of detected pages.
  /// When [force] is true, bypass all caches and reprocess from scratch.
  Future<void> _translateVisiblePages({bool force = false}) async {
    if (_detectedWebtoonPages.isEmpty) {
      // Non-webtoon: fall back to screenshot capture
      _captureAndTranslate(force: force);
      return;
    }
    // Force-submit the next batch of webtoon pages
    _submitNextBatch(force: force);
  }

  /// Push a status message into the in-page toolbar.
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
