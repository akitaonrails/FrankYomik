import 'dart:convert';
import 'dart:typed_data';
import 'dart:ui' as ui;
import 'package:flutter/material.dart';
import 'package:flutter_inappwebview/flutter_inappwebview.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../providers/jobs_provider.dart';
import '../providers/settings_provider.dart';
import '../services/image_capture_service.dart';
import '../webview/dom_inspector.dart';
import '../webview/js_bridge.dart';
import '../webview/overlay_controller.dart';
import '../webview/strategies/kindle_strategy.dart';
import '../widgets/connection_banner.dart';
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
  InAppWebViewController? _webController;
  final _jsBridge = JsBridge();
  final _inspector = DomInspector();
  final _overlay = OverlayController();
  final _capture = ImageCaptureService();

  String _currentUrl = '';
  bool _inspectorMode = false;
  bool _showOverlay = true;

  /// Active Kindle overlays keyed by pageId.
  final Map<String, _KindleOverlay> _kindleOverlays = {};

  /// Last-known reader content rect from JS detection (CSS pixels).
  ui.Rect? _readerRect;

  /// Last-known device pixel ratio from JS detection.
  double _devicePixelRatio = 1.0;

  /// Last-known stack size for detecting layout changes.
  Size? _stackSize;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
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

    return Scaffold(
      appBar: AppBar(
        titleSpacing: 0,
        title: Text(
          _currentUrl.isEmpty ? widget.initialUrl : _currentUrl,
          style: const TextStyle(fontSize: 13),
          overflow: TextOverflow.ellipsis,
        ),
        actions: [
          // Toggle overlay visibility
          IconButton(
            icon: Icon(_showOverlay ? Icons.visibility : Icons.visibility_off),
            tooltip: _showOverlay ? 'Hide translations' : 'Show translations',
            onPressed: () {
              setState(() {
                _showOverlay = !_showOverlay;
                // Toggle all Kindle overlays visibility
                for (final o in _kindleOverlays.values) {
                  o.visible = _showOverlay;
                }
              });
            },
          ),
          // Translate current page manually
          IconButton(
            icon: const Icon(Icons.translate),
            tooltip: 'Translate current page',
            onPressed: _captureAndTranslate,
          ),
          // Inspector toggle
          IconButton(
            icon: Icon(_inspectorMode ? Icons.bug_report : Icons.pest_control),
            tooltip: _inspectorMode ? 'Disable inspector' : 'Enable inspector',
            onPressed: _toggleInspector,
          ),
          // Open inspector logs
          if (_inspectorMode)
            IconButton(
              icon: const Icon(Icons.list),
              tooltip: 'Inspector logs',
              onPressed: () => Navigator.push(
                context,
                MaterialPageRoute(
                  builder: (_) => InspectorScreen(inspector: _inspector),
                ),
              ),
            ),
        ],
      ),
      body: Column(
        children: [
          const ConnectionBanner(),
          Expanded(
            child: LayoutBuilder(
              builder: (context, constraints) {
                final newSize =
                    Size(constraints.maxWidth, constraints.maxHeight);
                _checkStackSizeChanged(newSize);
                return Stack(
                  children: [
                    InAppWebView(
                      initialUrlRequest:
                          URLRequest(url: WebUri(widget.initialUrl)),
                      initialSettings: InAppWebViewSettings(
                        javaScriptEnabled: true,
                        domStorageEnabled: true,
                        userAgent:
                            'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
                        mixedContentMode:
                            MixedContentMode.MIXED_CONTENT_ALWAYS_ALLOW,
                      ),
                      onWebViewCreated: (controller) {
                        _webController = controller;
                        _jsBridge.attach(controller);
                        _inspector.attach(controller);
                        _jsBridge.onPageDetected = _onPageDetected;
                      },
                      onLoadStop: (controller, url) {
                        final urlStr = url?.toString() ?? '';
                        setState(() => _currentUrl = urlStr);
                        _jsBridge.onUrlChanged(controller, urlStr);
                        if (_inspectorMode) {
                          _inspector.inject(controller);
                          _injectKindleDiagnosticIfNeeded(controller);
                        }
                      },
                      onUpdateVisitedHistory: (controller, url, isReload) {
                        final urlStr = url?.toString() ?? '';
                        setState(() => _currentUrl = urlStr);
                        _jsBridge.onUrlChanged(controller, urlStr);
                      },
                    ),
                    // Kindle overlay widgets
                    if (_showOverlay)
                      ..._kindleOverlays.values
                          .where((o) => o.visible)
                          .map(_buildKindleOverlay),
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
          ),
        ],
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
    if (!settings.autoTranslate) return;

    // For spread pages, check both left and right sub-page jobs
    final pageMode = pageInfo['pageMode'] as String?;
    if (pageMode == 'spread') {
      _handleSpreadDetection(pageId, pageInfo);
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
        await ref.read(jobsProvider.notifier).submitPage(
              pageId: leftId,
              imageBytes: halves.$1,
              title: meta?.title,
              chapter: meta?.chapter,
              pageNumber: '${pageInfo['index']}-L',
              sourceUrl: _currentUrl,
            );
        await ref.read(jobsProvider.notifier).submitPage(
              pageId: rightId,
              imageBytes: halves.$2,
              title: meta?.title,
              chapter: meta?.chapter,
              pageNumber: '${pageInfo['index']}-R',
              sourceUrl: _currentUrl,
            );

        _watchForSpreadCompletion(pageId, leftId, rightId);
        return;
      }
    } else {
      // Webtoon: JS-based capture
      final captureJs = _jsBridge.getCaptureScript(pageId);
      if (captureJs != null) {
        final result =
            await controller.evaluateJavascript(source: captureJs);
        if (result is String) {
          try {
            imageBytes = base64Decode(result);
          } catch (_) {}
        }
      }
    }

    if (imageBytes == null || imageBytes.isEmpty) return;

    // Extract metadata from URL
    final meta = _jsBridge.parseCurrentUrl(_currentUrl);

    await ref.read(jobsProvider.notifier).submitPage(
          pageId: pageId,
          imageBytes: imageBytes,
          title: meta?.title,
          chapter: meta?.chapter,
          pageNumber: meta?.pageNumber ?? pageInfo['index']?.toString(),
          sourceUrl: _currentUrl,
        );

    // Watch for completion to apply overlay
    _watchForCompletion(pageId);
  }

  void _watchForCompletion(String pageId) {
    ref.listenManual(jobsProvider, (previous, next) {
      final job = next[pageId];
      if (job != null &&
          job.isComplete &&
          job.translatedImage != null &&
          _showOverlay) {
        _applyOverlay(pageId, job.translatedImage!);
      }
    });
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
      await _overlay.replaceImage(controller, pageId, imageBytes);
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

    // Take a full screenshot for manual translation
    final imageBytes = await _capture.takeScreenshot(controller);
    if (imageBytes == null) return;

    final meta = _jsBridge.parseCurrentUrl(_currentUrl);
    final pageId = 'manual-${DateTime.now().millisecondsSinceEpoch}';

    await ref.read(jobsProvider.notifier).submitPage(
          pageId: pageId,
          imageBytes: imageBytes,
          title: meta?.title,
          chapter: meta?.chapter,
          pageNumber: meta?.pageNumber,
          sourceUrl: _currentUrl,
        );

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

  void _injectKindleDiagnosticIfNeeded(InAppWebViewController controller) {
    if (_jsBridge.activeStrategy?.siteName == 'kindle') {
      controller.evaluateJavascript(
          source: KindleStrategy.diagnosticScript);
    }
  }
}
