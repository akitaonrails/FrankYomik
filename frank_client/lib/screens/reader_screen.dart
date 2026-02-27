import 'dart:convert';
import 'dart:ui' as ui;
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:http/http.dart' as http;
import '../providers/jobs_provider.dart';
import '../providers/settings_provider.dart';
import '../services/image_capture_service.dart';
import '../webview/dom_inspector.dart';
import '../webview/js_bridge.dart';
import '../webview/overlay_controller.dart';
import '../webview/platform/app_webview.dart';
import '../webview/platform/app_webview_controller.dart';
import '../webview/strategies/kindle_strategy.dart';
import '../widgets/progress_indicator.dart';
import 'inspector_screen.dart';

/// Overlay entry for a Kindle translated page.
class _KindleOverlay {
  final String pageId;
  final Uint8List imageBytes;
  final ui.Rect rect;
  bool visible;

  _KindleOverlay({
    required this.pageId,
    required this.imageBytes,
    required this.rect,
  }) : visible = true;
}

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
  bool _inspectorMode = false;
  bool _showOverlay = true;

  /// Whether the floating toolbar is visible.
  bool _toolbarVisible = false;

  /// Active Kindle overlays keyed by pageId.
  final Map<String, _KindleOverlay> _kindleOverlays = {};

  /// Last-known reader content rect from JS detection (CSS pixels).
  ui.Rect? _readerRect;

  /// Last-known device pixel ratio from JS detection.
  double _devicePixelRatio = 1.0;

  /// Last-known stack size for detecting layout changes.
  Size? _stackSize;

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
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    for (final sub in _completionListeners.values) {
      sub.close();
    }
    _completionListeners.clear();
    super.dispose();
  }

  void _showToolbar() {
    if (_toolbarVisible) return;
    setState(() => _toolbarVisible = true);
    Future.delayed(const Duration(seconds: 4), () {
      if (mounted && _toolbarVisible) {
        setState(() => _toolbarVisible = false);
      }
    });
  }

  @override
  void didChangeMetrics() {
    // Window resize or orientation change — invalidate Kindle overlays
    if (_kindleOverlays.isNotEmpty) {
      _clearKindleOverlays('resize');
    }
  }

  @override
  Widget build(BuildContext context) {
    final jobs = ref.watch(jobsProvider);
    final latestActive = jobs.values
        .where((j) => j.isActive)
        .toList()
      ..sort((a, b) => b.percent.compareTo(a.percent));
    final topPadding = MediaQuery.of(context).padding.top;

    return Scaffold(
      body: LayoutBuilder(
        builder: (context, constraints) {
          final newSize =
              Size(constraints.maxWidth, constraints.maxHeight);
          _checkStackSizeChanged(newSize);
          return Stack(
            children: [
              // WebView fills entire screen
              AppWebView(
                initialUrl: widget.initialUrl,
                userAgent:
                    'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
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
                  // Reset webtoon batch state on new page
                  _detectedWebtoonPages.clear();
                  _batchSubmittedUpTo = -1;
                  _batchInProgress = false;
                  // Cancel all completion listeners from previous page
                  for (final sub in _completionListeners.values) {
                    sub.close();
                  }
                  _completionListeners.clear();
                  _jsBridge.onUrlChanged(controller, urlStr);
                  _injectDesktopViewportFit(controller);
                  // Sync auto-translate button state after toolbar injection
                  Future.delayed(const Duration(milliseconds: 500), () {
                    _syncAutoButtonState();
                  });
                  if (_inspectorMode) {
                    _inspector.inject(controller);
                    _injectKindleDiagnosticIfNeeded(controller);
                  }
                },
                onUpdateVisitedHistory: (controller, url, isReload) {
                  final urlStr = url ?? '';
                  setState(() => _currentUrl = urlStr);
                  _jsBridge.onUrlChanged(controller, urlStr);
                },
              ),
              // Kindle overlay widgets
              if (_showOverlay)
                ..._kindleOverlays.values
                    .where((o) => o.visible)
                    .map(_buildKindleOverlay),
              // Hover zone at top to reveal toolbar (desktop)
              Positioned(
                top: 0,
                left: 0,
                right: 0,
                height: 32,
                child: MouseRegion(
                  onEnter: (_) => _showToolbar(),
                  child: GestureDetector(
                    onTap: _showToolbar,
                    behavior: HitTestBehavior.translucent,
                  ),
                ),
              ),
              // Floating toolbar
              if (_toolbarVisible)
                Positioned(
                  top: topPadding,
                  left: 0,
                  right: 0,
                  child: Material(
                    elevation: 4,
                    color: Theme.of(context)
                        .colorScheme
                        .surface
                        .withAlpha(230),
                    child: SafeArea(
                      bottom: false,
                      child: Row(
                        children: [
                          IconButton(
                            icon: const Icon(Icons.arrow_back),
                            tooltip: 'Back',
                            onPressed: () => Navigator.pop(context),
                          ),
                          Expanded(
                            child: Text(
                              _currentUrl.isEmpty
                                  ? widget.initialUrl
                                  : _currentUrl,
                              style: const TextStyle(fontSize: 12),
                              overflow: TextOverflow.ellipsis,
                            ),
                          ),
                          IconButton(
                            icon: Icon(_showOverlay
                                ? Icons.visibility
                                : Icons.visibility_off),
                            tooltip: _showOverlay
                                ? 'Hide translations'
                                : 'Show translations',
                            onPressed: () {
                              setState(() {
                                _showOverlay = !_showOverlay;
                                for (final o in _kindleOverlays.values) {
                                  o.visible = _showOverlay;
                                }
                              });
                            },
                          ),
                          IconButton(
                            icon: const Icon(Icons.translate),
                            tooltip: 'Translate current page',
                            onPressed: _captureAndTranslate,
                          ),
                          IconButton(
                            icon: Icon(_inspectorMode
                                ? Icons.bug_report
                                : Icons.pest_control),
                            tooltip: _inspectorMode
                                ? 'Disable inspector'
                                : 'Enable inspector',
                            onPressed: _toggleInspector,
                          ),
                          if (_inspectorMode)
                            IconButton(
                              icon: const Icon(Icons.list),
                              tooltip: 'Inspector logs',
                              onPressed: () => Navigator.push(
                                context,
                                MaterialPageRoute(
                                  builder: (_) =>
                                      InspectorScreen(inspector: _inspector),
                                ),
                              ),
                            ),
                        ],
                      ),
                    ),
                  ),
                ),
              // Progress chip overlay
              if (latestActive.isNotEmpty)
                Positioned(
                  bottom: 16,
                  left: 16,
                  right: 16,
                  child: Center(
                    child: TranslationProgressChip(
                        job: latestActive.first),
                  ),
                ),
            ],
          );
        },
      ),
    );
  }

  Widget _buildKindleOverlay(_KindleOverlay overlay) {
    return Positioned(
      left: overlay.rect.left,
      top: overlay.rect.top,
      width: overlay.rect.width,
      height: overlay.rect.height,
      child: GestureDetector(
        onTap: () {
          setState(() {
            overlay.visible = !overlay.visible;
          });
        },
        child: Image.memory(
          overlay.imageBytes,
          fit: BoxFit.fill,
          gaplessPlayback: true,
        ),
      ),
    );
  }

  /// Clear all Kindle overlays and reset reader rect.
  void _clearKindleOverlays(String reason) {
    final count = _kindleOverlays.length;
    setState(() {
      _kindleOverlays.clear();
      _readerRect = null;
    });
    _logKindle('kindle_clear', {
      'reason': reason,
      'clearedCount': count,
    });
  }

  /// Detect layout size changes (e.g., ConnectionBanner appearing/disappearing).
  void _checkStackSizeChanged(Size newSize) {
    final oldSize = _stackSize;
    _stackSize = newSize;
    if (oldSize == null) return;
    final dw = (newSize.width - oldSize.width).abs();
    final dh = (newSize.height - oldSize.height).abs();
    if ((dw > 10 || dh > 10) && _kindleOverlays.isNotEmpty) {
      _logKindle('kindle_resize', {
        'oldSize': '${oldSize.width.toInt()}x${oldSize.height.toInt()}',
        'newSize': '${newSize.width.toInt()}x${newSize.height.toInt()}',
      });
      // Schedule clear after build to avoid setState during build
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && _kindleOverlays.isNotEmpty) {
          _clearKindleOverlays('resize');
        }
      });
    }
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
    debugPrint('[Reader] _onPageDetected: $pageInfo');
    final pageId = pageInfo['pageId'] as String?;
    if (pageId == null) return;

    // Update reader rect from detection data
    final rectData = pageInfo['readerRect'];
    if (rectData is Map) {
      final x = (rectData['x'] as num?)?.toDouble() ?? 0;
      final y = (rectData['y'] as num?)?.toDouble() ?? 0;
      final w = (rectData['width'] as num?)?.toDouble() ?? 0;
      final h = (rectData['height'] as num?)?.toDouble() ?? 0;
      if (w > 0 && h > 0) {
        _readerRect = ui.Rect.fromLTWH(x, y, w, h);
      }
    }
    final dpr = (pageInfo['devicePixelRatio'] as num?)?.toDouble();
    if (dpr != null && dpr > 0) {
      _devicePixelRatio = dpr;
    }

    _logKindle('kindle_detect', {
      'pageId': pageId,
      'pageMode': pageInfo['pageMode'],
      'readerRect': pageInfo['readerRect']?.toString(),
      'devicePixelRatio': _devicePixelRatio,
      'stackSize': _stackSize != null
          ? '${_stackSize!.width.toInt()}x${_stackSize!.height.toInt()}'
          : null,
    });

    // Clear stale Kindle overlays on page change
    if (_kindleOverlays.isNotEmpty) {
      final staleKeys = _kindleOverlays.keys
          .where((k) => k != pageId && !_isRelatedSpreadPage(k, pageId))
          .toList();
      if (staleKeys.isNotEmpty) {
        final count = staleKeys.length;
        setState(() {
          for (final k in staleKeys) {
            _kindleOverlays.remove(k);
          }
        });
        _logKindle('kindle_clear', {
          'reason': 'page_change',
          'clearedCount': count,
        });
      }
    }

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
        debugPrint('[Reader] Auto-translate OFF, detected $pageId');
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
      String spreadPageId, Map<String, dynamic> pageInfo) {
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
      _applySpreadOverlay(spreadPageId, leftJob.translatedImage!,
          rightJob.translatedImage!);
      return;
    }

    // If not yet submitted, capture and split
    if (!jobs.containsKey(leftId) && !jobs.containsKey(rightId)) {
      _capturePageImage(spreadPageId, pageInfo);
    }
  }

  /// Check if two page IDs are related spread pages (e.g., 'kindle-5-spread-L' relates to 'kindle-5-spread').
  bool _isRelatedSpreadPage(String existingId, String newId) {
    return existingId.startsWith(newId) || newId.startsWith(existingId);
  }

  Future<void> _capturePageImage(
      String pageId, Map<String, dynamic> pageInfo) async {
    debugPrint('[Reader] _capturePageImage pageId=$pageId type=${pageInfo['type']}');
    final controller = _webController;
    if (controller == null) return;

    Uint8List? imageBytes;

    final type = pageInfo['type'] as String?;
    if (type == 'screenshot') {
      // Kindle: screenshot capture + crop to reader bounds
      final screenshot = await _capture.takeScreenshot(controller);
      if (screenshot == null) return;

      // Get reader rect from pageInfo or stored state
      ui.Rect? cropRect;
      final rectData = pageInfo['readerRect'];
      if (rectData is Map) {
        final x = (rectData['x'] as num?)?.toDouble() ?? 0;
        final y = (rectData['y'] as num?)?.toDouble() ?? 0;
        final w = (rectData['width'] as num?)?.toDouble() ?? 0;
        final h = (rectData['height'] as num?)?.toDouble() ?? 0;
        if (w > 0 && h > 0) {
          cropRect = ui.Rect.fromLTWH(x, y, w, h);
        }
      }
      cropRect ??= _readerRect;

      if (cropRect != null) {
        imageBytes =
            ImageCaptureService.cropToRect(screenshot, cropRect, _devicePixelRatio);
      } else {
        imageBytes = screenshot;
      }

      _logKindle('kindle_capture', {
        'pageId': pageId,
        'pageMode': pageInfo['pageMode'],
        'screenshotSize': '${screenshot.length} bytes',
        'cropRect': cropRect?.toString(),
        'croppedSize': imageBytes != null ? '${imageBytes.length} bytes' : null,
      });

      if (imageBytes == null) return;

      // Handle spread mode: split and submit two jobs
      final pageMode = pageInfo['pageMode'] as String?;
      if (pageMode == 'spread') {
        final halves = ImageCaptureService.splitSpread(imageBytes);
        if (halves == null) return;

        final leftId = '$pageId-L';
        final rightId = '$pageId-R';

        _logKindle('kindle_split', {
          'spreadPageId': pageId,
          'leftSize': '${halves.$1.length} bytes',
          'rightSize': '${halves.$2.length} bytes',
        });

        final meta = _jsBridge.parseCurrentUrl(_currentUrl);

        // Submit left and right halves as separate jobs
        final spreadPipeline = _jsBridge.activeStrategy?.defaultPipeline;
        await ref.read(jobsProvider.notifier).submitPage(
              pageId: leftId,
              imageBytes: halves.$1,
              pipeline: spreadPipeline,
              title: meta?.title,
              chapter: meta?.chapter,
              pageNumber: '${pageInfo['index']}-L',
              sourceUrl: _currentUrl,
            );
        await ref.read(jobsProvider.notifier).submitPage(
              pageId: rightId,
              imageBytes: halves.$2,
              pipeline: spreadPipeline,
              title: meta?.title,
              chapter: meta?.chapter,
              pageNumber: '${pageInfo['index']}-R',
              sourceUrl: _currentUrl,
            );

        _watchForSpreadCompletion(pageId, leftId, rightId);
        return;
      }
    } else {
      // Webtoon: download image directly from src URL
      final src = pageInfo['src'] as String?;
      if (src != null && src.isNotEmpty) {
        debugPrint('[Reader] Downloading image for $pageId from $src');
        try {
          final response = await http.get(
            Uri.parse(src),
            headers: {'Referer': _currentUrl},
          );
          if (response.statusCode == 200 && response.bodyBytes.isNotEmpty) {
            imageBytes = response.bodyBytes;
            debugPrint('[Reader] Downloaded ${imageBytes!.length} bytes for $pageId');
          } else {
            debugPrint('[Reader] Download failed for $pageId: HTTP ${response.statusCode}');
          }
        } catch (e) {
          debugPrint('[Reader] Download error for $pageId: $e');
        }
      }
    }

    if (imageBytes == null || imageBytes.isEmpty) {
      debugPrint('[Reader] No image captured for $pageId');
      return;
    }

    // Use site-specific pipeline (webtoon for Korean, user setting for Kindle)
    final pipeline = _jsBridge.activeStrategy?.defaultPipeline;
    final srcForLog = (pageInfo['src'] as String?)?.substring(0,
        ((pageInfo['src'] as String?)?.length ?? 0).clamp(0, 60));
    debugPrint('[Reader] Submitting $pageId (${imageBytes.length} bytes, '
        'pipeline=${pipeline ?? 'default'}, src=$srcForLog)');
    _updateInPageStatus('Submitting $pageId...');

    // Extract metadata from URL
    final meta = _jsBridge.parseCurrentUrl(_currentUrl);

    // For webtoon, use the image index as page number (not the URL-derived '0')
    final pageNumber = pageInfo['index']?.toString() ?? meta?.pageNumber;
    await ref.read(jobsProvider.notifier).submitPage(
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

      if (toSubmit.isEmpty) {
        debugPrint('[Reader] No new pages to batch-submit');
        return;
      }

      debugPrint('[Reader] Batch submitting ${toSubmit.length} pages in parallel: $toSubmit');
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
      final hasMore = sortedIndices.any((idx) =>
          idx > _batchSubmittedUpTo &&
          !ref.read(jobsProvider).containsKey('wt-$idx'));
      if (hasMore) {
        debugPrint('[Reader] More pages available, scheduling next batch');
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
      debugPrint('[Reader] Job $pageId already complete, applying overlay immediately');
      _updateInPageStatus('$pageId done (cached)!');
      if (_showOverlay) {
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
        debugPrint('[Reader] Job $pageId completed, applying overlay');
        _updateInPageStatus('$pageId done!');
        // Cancel this listener — job is done
        _completionListeners[pageId]?.close();
        _completionListeners.remove(pageId);
        if (_showOverlay) {
          _applyOverlay(pageId, job.translatedImage!);
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
      String spreadPageId, String leftId, String rightId) {
    ref.listenManual(jobsProvider, (previous, next) {
      final leftJob = next[leftId];
      final rightJob = next[rightId];
      if (leftJob != null &&
          leftJob.isComplete &&
          leftJob.translatedImage != null &&
          rightJob != null &&
          rightJob.isComplete &&
          rightJob.translatedImage != null &&
          _showOverlay) {
        _applySpreadOverlay(
            spreadPageId, leftJob.translatedImage!, rightJob.translatedImage!);
      }
    });
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
      debugPrint('[Reader] Applying overlay for $pageId (index=$index), '
          'translatedBytes=${imageBytes.length}, '
          'src=${originalSrc?.substring(0, (originalSrc?.length ?? 0).clamp(0, 80))}');
      if (originalSrc != null && originalSrc.isNotEmpty) {
        final ok = await _overlay.replaceImageBySrc(controller, originalSrc, imageBytes);
        debugPrint('[Reader] replaceImageBySrc for $pageId returned $ok');
      } else {
        debugPrint('[Reader] WARNING: No src found for $pageId (index=$index), '
            'detected pages: ${_detectedWebtoonPages.keys.toList()}');
      }
    } else if (_jsBridge.activeStrategy?.siteName == 'kindle') {
      // Kindle: Flutter overlay positioned over the reader area
      final rect = _readerRect;
      if (rect == null) return;

      setState(() {
        _kindleOverlays[pageId] = _KindleOverlay(
          pageId: pageId,
          imageBytes: imageBytes,
          rect: rect,
        );
      });
      _logKindle('kindle_overlay', {
        'pageId': pageId,
        'overlayRect': rect.toString(),
        'overlayCount': _kindleOverlays.length,
      });
    }
  }

  /// Apply overlay for a 2-page spread (left and right halves).
  void _applySpreadOverlay(
      String spreadPageId, Uint8List leftImage, Uint8List rightImage) {
    final rect = _readerRect;
    if (rect == null) return;

    final halfWidth = rect.width / 2;

    setState(() {
      _kindleOverlays['$spreadPageId-L'] = _KindleOverlay(
        pageId: '$spreadPageId-L',
        imageBytes: leftImage,
        rect: ui.Rect.fromLTWH(rect.left, rect.top, halfWidth, rect.height),
      );
      _kindleOverlays['$spreadPageId-R'] = _KindleOverlay(
        pageId: '$spreadPageId-R',
        imageBytes: rightImage,
        rect: ui.Rect.fromLTWH(
            rect.left + halfWidth, rect.top, rect.width - halfWidth, rect.height),
      );
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

    await ref.read(jobsProvider.notifier).submitPage(
          pageId: pageId,
          imageBytes: imageBytes,
          pipeline: _jsBridge.activeStrategy?.defaultPipeline,
          title: meta?.title,
          chapter: meta?.chapter,
          pageNumber: meta?.pageNumber,
          sourceUrl: _currentUrl,
        );

    _updateInPageStatus('Queued $pageId');
    _watchForCompletion(pageId);
  }

  void _toggleInspector() {
    setState(() => _inspectorMode = !_inspectorMode);
    final controller = _webController;
    if (controller == null) return;

    if (_inspectorMode) {
      _inspector.inject(controller);
      _overlay.enableTapMode(controller);
      _injectKindleDiagnosticIfNeeded(controller);
    } else {
      _overlay.disableTapMode(controller);
    }
  }

  void _injectKindleDiagnosticIfNeeded(AppWebViewController controller) {
    if (_jsBridge.activeStrategy?.siteName == 'kindle') {
      controller.evaluateJavascript(
          source: KindleStrategy.diagnosticScript);
    }
  }

  /// Inject CSS that caps image width on wide landscape viewports,
  /// and an in-page floating toolbar with translate/back/progress controls.
  void _injectDesktopViewportFit(AppWebViewController controller) {
    controller.evaluateJavascript(source: '''
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
  var bar = document.createElement('div');
  bar.id = '__frankBar';
  bar.innerHTML =
    '<button id="__frankBack" title="Back">&#x2190;</button>' +
    '<button id="__frankAuto" title="Toggle auto-translate">Auto: ON</button>' +
    '<button id="__frankTranslate" title="Translate visible pages">&#x1F30D; Translate</button>' +
    '<span id="__frankStatus"></span>';
  bar.style.cssText =
    'position:fixed; bottom:12px; right:12px; z-index:999999;' +
    'display:flex; align-items:center; gap:6px;' +
    'background:rgba(30,30,30,0.85); color:#fff; padding:6px 10px;' +
    'border-radius:8px; font:13px/1.3 sans-serif; box-shadow:0 2px 8px rgba(0,0,0,0.4);' +
    'user-select:none; -webkit-user-select:none;';

  var btnStyle =
    'background:none; border:1px solid rgba(255,255,255,0.3); color:#fff;' +
    'border-radius:4px; padding:4px 8px; cursor:pointer; font:inherit;';

  document.body.appendChild(bar);

  var backBtn = document.getElementById('__frankBack');
  var autoBtn = document.getElementById('__frankAuto');
  var transBtn = document.getElementById('__frankTranslate');
  backBtn.style.cssText = btnStyle;
  autoBtn.style.cssText = btnStyle;
  transBtn.style.cssText = btnStyle;

  backBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'back');
  });
  autoBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'toggle_auto');
  });
  transBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    window.flutter_inappwebview.callHandler('onToolbarAction', 'translate');
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
})();
''');
  }

  /// Register the toolbar action handler on the WebView controller.
  void _registerToolbarHandler(AppWebViewController controller) {
    controller.addJavaScriptHandler(
      handlerName: 'onToolbarAction',
      callback: (args) {
        final action = args.isNotEmpty ? args[0] as String? : null;
        debugPrint('[Reader] Toolbar action: $action');
        switch (action) {
          case 'back':
            Navigator.pop(context);
            break;
          case 'toggle_auto':
            _toggleAutoTranslate();
            break;
          case 'translate':
            _translateVisiblePages();
            break;
        }
        return null;
      },
    );
  }

  /// Toggle auto-translate and update the in-page button state.
  void _toggleAutoTranslate() {
    final settings = ref.read(settingsProvider);
    final newValue = !settings.autoTranslate;
    ref.read(settingsProvider.notifier).update(
          settings.copyWith(autoTranslate: newValue),
        );
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
        source: 'if(window.__frankSetAutoState) window.__frankSetAutoState($on);');
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
        source: "if(window.__frankSetStatus) window.__frankSetStatus('$escaped');");
  }
}
