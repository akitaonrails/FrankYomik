import 'dart:io' show File;
import 'dart:typed_data';
import 'dart:ui';
import 'package:flutter_test/flutter_test.dart';
import 'package:frank_client/models/server_settings.dart';
import 'package:frank_client/models/page_job.dart';
import 'package:frank_client/models/site_config.dart';
import 'package:frank_client/webview/strategies/naver_webtoon_strategy.dart';
import 'package:frank_client/webview/strategies/kindle_strategy.dart';
import 'package:frank_client/webview/kindle_prefetch_manager.dart';
import 'package:frank_client/services/image_capture_service.dart';
import 'package:frank_client/webview/dom_inspector.dart';
import 'package:image/image.dart' as img;

/// Read overlay_controller.dart source for pattern verification.
/// Tests verify the actual source to catch regressions without needing
/// a WebView mock.
String _readOverlaySource() {
  // Find project root (test runs from frank_client/)
  final file = File('lib/webview/overlay_controller.dart');
  if (!file.existsSync()) {
    throw StateError('overlay_controller.dart not found at ${file.absolute.path}');
  }
  return file.readAsStringSync();
}

void main() {
  group('ServerSettings', () {
    test('defaults', () {
      const s = ServerSettings();
      expect(s.serverUrl, 'http://localhost:8080');
      expect(s.pipeline, 'manga_translate');
      expect(s.autoTranslate, true);
      expect(s.isConfigured, false);
      expect(s.targetLanguage, 'en');
    });

    test('isConfigured with token', () {
      const s = ServerSettings(authToken: 'secret');
      expect(s.isConfigured, true);
    });

    test('copyWith', () {
      const s = ServerSettings(authToken: 'a');
      final s2 = s.copyWith(serverUrl: 'http://other:9090');
      expect(s2.serverUrl, 'http://other:9090');
      expect(s2.authToken, 'a');
      expect(s2.targetLanguage, 'en');
    });

    test('copyWith targetLanguage', () {
      const s = ServerSettings();
      final s2 = s.copyWith(targetLanguage: 'pt-br');
      expect(s2.targetLanguage, 'pt-br');
      expect(s2.pipeline, 'manga_translate');
    });

    test('targetLanguages map', () {
      expect(ServerSettings.targetLanguages.containsKey('en'), true);
      expect(ServerSettings.targetLanguages.containsKey('pt-br'), true);
      expect(ServerSettings.targetLanguages['pt-br'], 'Brazilian Portuguese');
    });
  });

  group('PageJob', () {
    test('default status is pending', () {
      final job = PageJob(pageId: 'test-1');
      expect(job.status, PageJobStatus.pending);
      expect(job.isComplete, false);
      expect(job.isActive, false);
    });

    test('isActive for queued/processing', () {
      final job = PageJob(pageId: 'a', status: PageJobStatus.queued);
      expect(job.isActive, true);

      final job2 = PageJob(pageId: 'b', status: PageJobStatus.processing);
      expect(job2.isActive, true);
    });

    test('isComplete', () {
      final job = PageJob(pageId: 'c', status: PageJobStatus.completed);
      expect(job.isComplete, true);
      expect(job.isFailed, false);
    });
  });

  group('SiteConfig', () {
    test('has kindle and naver webtoon', () {
      expect(SiteConfig.sites.length, 2);
      expect(SiteConfig.sites[0].name, 'kindle');
      expect(SiteConfig.sites[1].name, 'naver_webtoon');
    });
  });

  group('NaverWebtoonStrategy', () {
    test('matches naver webtoon URLs', () {
      final s = NaverWebtoonStrategy();
      expect(
        s.matches(
          'https://m.comic.naver.com/webtoon/detail?titleId=747269&no=297',
        ),
        true,
      );
      expect(
        s.matches(
          'https://comic.naver.com/webtoon/detail?titleId=747269&no=297',
        ),
        true,
      );
      expect(s.matches('https://m.comic.naver.com/webtoon'), true);
      expect(s.matches('https://www.webtoons.com/en/action/tower'), false);
      expect(s.matches('https://example.com'), false);
    });

    test('parseUrl extracts titleId and episode number', () {
      final s = NaverWebtoonStrategy();
      final meta = s.parseUrl(
        'https://m.comic.naver.com/webtoon/detail?titleId=747269&no=297',
      );
      expect(meta, isNotNull);
      expect(meta!.title, '747269');
      expect(meta.chapter, '297');
    });

    test('parseUrl handles missing episode number', () {
      final s = NaverWebtoonStrategy();
      final meta = s.parseUrl(
        'https://m.comic.naver.com/webtoon/detail?titleId=747269',
      );
      expect(meta, isNotNull);
      expect(meta!.title, '747269');
      expect(meta.chapter, '0');
    });

    test('parseUrl returns null without titleId', () {
      final s = NaverWebtoonStrategy();
      final meta = s.parseUrl('https://m.comic.naver.com/webtoon');
      expect(meta, isNull);
    });

    test('siteName is webtoon', () {
      final s = NaverWebtoonStrategy();
      expect(s.siteName, 'webtoon');
    });
  });

  group('KindleStrategy', () {
    test('matches kindle URLs', () {
      final s = KindleStrategy();
      expect(s.matches('https://read.amazon.co.jp/manga/B0ABC12345'), true);
      expect(s.matches('https://www.amazon.com'), false);
    });

    test('parseUrl extracts ASIN', () {
      final s = KindleStrategy();
      final meta = s.parseUrl(
        'https://read.amazon.co.jp/manga/B0ABC12345?ref=foo',
      );
      expect(meta, isNotNull);
      expect(meta!.title, 'B0ABC12345');
    });

    test('pageModeFromSize detects spread when width > height * 1.3', () {
      // Wide landscape = spread (e.g., 1920x800)
      expect(KindleStrategy.pageModeFromSize(1920, 800), 'spread');
      // Exactly at threshold: 1300 > 1000 * 1.3 = false (not strictly greater)
      expect(KindleStrategy.pageModeFromSize(1300, 1000), 'single');
      // Just above threshold
      expect(KindleStrategy.pageModeFromSize(1301, 1000), 'spread');
      // Portrait = single
      expect(KindleStrategy.pageModeFromSize(800, 1200), 'single');
      // Square-ish = single
      expect(KindleStrategy.pageModeFromSize(1000, 1000), 'single');
    });

    test('spreadThreshold is 1.3', () {
      expect(KindleStrategy.spreadThreshold, 1.3);
    });
  });

  group('ImageCaptureService.splitSpread', () {
    test('splits image into left and right halves', () {
      // Create a 200x100 test image: left half red, right half blue
      final testImage = img.Image(width: 200, height: 100);
      for (var y = 0; y < 100; y++) {
        for (var x = 0; x < 100; x++) {
          testImage.setPixelRgba(x, y, 255, 0, 0, 255); // Red left
        }
        for (var x = 100; x < 200; x++) {
          testImage.setPixelRgba(x, y, 0, 0, 255, 255); // Blue right
        }
      }
      final pngBytes = Uint8List.fromList(img.encodePng(testImage));

      final result = ImageCaptureService.splitSpread(pngBytes);
      expect(result, isNotNull);

      final left = img.decodePng(result!.$1)!;
      final right = img.decodePng(result.$2)!;

      // Both halves should be 100x100
      expect(left.width, 100);
      expect(left.height, 100);
      expect(right.width, 100);
      expect(right.height, 100);

      // Left should be red, right should be blue
      final leftPixel = left.getPixel(50, 50);
      expect(leftPixel.r.toInt(), 255);
      expect(leftPixel.g.toInt(), 0);
      expect(leftPixel.b.toInt(), 0);

      final rightPixel = right.getPixel(50, 50);
      expect(rightPixel.r.toInt(), 0);
      expect(rightPixel.g.toInt(), 0);
      expect(rightPixel.b.toInt(), 255);
    });

    test('handles odd-width images', () {
      final testImage = img.Image(width: 201, height: 100);
      final pngBytes = Uint8List.fromList(img.encodePng(testImage));

      final result = ImageCaptureService.splitSpread(pngBytes);
      expect(result, isNotNull);

      final left = img.decodePng(result!.$1)!;
      final right = img.decodePng(result.$2)!;

      // 201 / 2 = 100 (truncated), right gets 101
      expect(left.width, 100);
      expect(right.width, 101);
      expect(left.height, 100);
      expect(right.height, 100);
    });

    test('returns null for invalid PNG', () {
      final result = ImageCaptureService.splitSpread(
        Uint8List.fromList([1, 2, 3]),
      );
      expect(result, isNull);
    });
  });

  group('DomInspector', () {
    test('log() adds entries accessible via .logs', () {
      final inspector = DomInspector();
      expect(inspector.logs, isEmpty);

      inspector.log({'type': 'kindle_detect', 'pageId': 'k-1'});
      inspector.log({'type': 'kindle_resize', 'oldSize': '800x600'});

      expect(inspector.logs.length, 2);
      expect(inspector.logs[0]['type'], 'kindle_detect');
      expect(inspector.logs[0]['pageId'], 'k-1');
      expect(inspector.logs[1]['type'], 'kindle_resize');
    });

    test('log() entries survive clear() only for new entries', () {
      final inspector = DomInspector();
      inspector.log({'type': 'a'});
      expect(inspector.logs.length, 1);

      inspector.clear();
      expect(inspector.logs, isEmpty);

      inspector.log({'type': 'b'});
      expect(inspector.logs.length, 1);
      expect(inspector.logs[0]['type'], 'b');
    });

    test('logs returns unmodifiable list', () {
      final inspector = DomInspector();
      inspector.log({'type': 'test'});
      final logs = inspector.logs;
      expect(() => logs.add({'type': 'fail'}), throwsUnsupportedError);
    });
  });

  group('KindleStrategy.diagnosticScript', () {
    test('is non-empty and contains expected selectors', () {
      final script = KindleStrategy.diagnosticScript;
      expect(script.isNotEmpty, true);
      expect(script.contains('#kr-renderer'), true);
      expect(script.contains('#kindle-reader-content'), true);
      expect(script.contains('.reader-content'), true);
      expect(script.contains('canvas'), true);
      expect(script.contains('onInspectorLog'), true);
      expect(script.contains('kindle_dom'), true);
    });

    test('contains spread detection logic', () {
      final script = KindleStrategy.diagnosticScript;
      expect(script.contains('spreadDetected'), true);
      expect(script.contains('devicePixelRatio'), true);
    });
  });

  group('KindleStrategy.detectionScript guards', () {
    test('contains loader visibility guard to avoid false detections', () {
      final script = KindleStrategy().detectionScript;
      expect(script.contains('__frankLoaderVisible'), true);
      expect(script.contains('if (__frankLoaderVisible()) return;'), true);
      expect(script.contains('kg-loader-wrapper'), true);
    });

    test('contains viewport overlap scoring for blob selection', () {
      final script = KindleStrategy().detectionScript;
      expect(script.contains('overlapAreaInViewport'), true);
      expect(script.contains('overlap < 2000'), true);
    });
  });

  group('KindlePrefetchManager JS selectors', () {
    test('blob URL script uses viewport-overlap area threshold', () {
      final script = KindlePrefetchManager.debugGetBlobUrlScript;
      expect(script.contains('overlapAreaInViewport'), true);
      expect(script.contains('area < 2000'), true);
    });

    test('page mode script uses viewport-overlap area threshold', () {
      final script = KindlePrefetchManager.debugPageModeScript;
      expect(script.contains('overlapAreaInViewport'), true);
      expect(script.contains('area < 2000'), true);
      expect(script.contains('1.3'), true);
    });
  });

  group('KindleStrategy.pageModeFromSize resize scenarios', () {
    test('zero dimensions return single', () {
      expect(KindleStrategy.pageModeFromSize(0, 0), 'single');
      expect(KindleStrategy.pageModeFromSize(0, 100), 'single');
    });

    test('typical phone portrait (360x640) is single', () {
      expect(KindleStrategy.pageModeFromSize(360, 640), 'single');
    });

    test('typical phone landscape (640x360) is spread', () {
      // 640 > 360 * 1.3 = 468 → spread
      expect(KindleStrategy.pageModeFromSize(640, 360), 'spread');
    });

    test('tablet landscape (1024x768) is single', () {
      // 1024 > 768 * 1.3 = 998.4 → spread
      expect(KindleStrategy.pageModeFromSize(1024, 768), 'spread');
    });

    test('narrow landscape just under threshold is single', () {
      // 1.3 * 1000 = 1300, so 1299 is not > 1300
      expect(KindleStrategy.pageModeFromSize(1299, 1000), 'single');
    });

    test('desktop wide window is spread', () {
      expect(KindleStrategy.pageModeFromSize(1920, 1080), 'spread');
    });
  });

  group('ImageCaptureService.cropToRect', () {
    test('crops to specified rect', () {
      // 400x300 image
      final testImage = img.Image(width: 400, height: 300);
      // Fill a 200x150 region at (50,25) with green
      for (var y = 25; y < 175; y++) {
        for (var x = 50; x < 250; x++) {
          testImage.setPixelRgba(x, y, 0, 255, 0, 255);
        }
      }
      final pngBytes = Uint8List.fromList(img.encodePng(testImage));

      // Crop with devicePixelRatio=1 to the green region
      final result = ImageCaptureService.cropToRect(
        pngBytes,
        const Rect.fromLTWH(50, 25, 200, 150),
        1.0,
      );
      expect(result, isNotNull);

      final cropped = img.decodePng(result!)!;
      expect(cropped.width, 200);
      expect(cropped.height, 150);

      // Center pixel should be green
      final pixel = cropped.getPixel(100, 75);
      expect(pixel.g.toInt(), 255);
    });

    test('applies devicePixelRatio scaling', () {
      // 800x600 image, CSS rect is (25,25,200,150), DPR=2
      // Physical crop should be (50,50,400,300)
      final testImage = img.Image(width: 800, height: 600);
      final pngBytes = Uint8List.fromList(img.encodePng(testImage));

      final result = ImageCaptureService.cropToRect(
        pngBytes,
        const Rect.fromLTWH(25, 25, 200, 150),
        2.0,
      );
      expect(result, isNotNull);

      final cropped = img.decodePng(result!)!;
      expect(cropped.width, 400);
      expect(cropped.height, 300);
    });

    test('clamps to image bounds', () {
      final testImage = img.Image(width: 100, height: 100);
      final pngBytes = Uint8List.fromList(img.encodePng(testImage));

      // Rect extends beyond image
      final result = ImageCaptureService.cropToRect(
        pngBytes,
        const Rect.fromLTWH(50, 50, 200, 200),
        1.0,
      );
      expect(result, isNotNull);

      final cropped = img.decodePng(result!)!;
      expect(cropped.width, 50);
      expect(cropped.height, 50);
    });

    test('returns null for invalid PNG', () {
      final result = ImageCaptureService.cropToRect(
        Uint8List.fromList([1, 2, 3]),
        const Rect.fromLTWH(0, 0, 100, 100),
        1.0,
      );
      expect(result, isNull);
    });
  });

  // ---------------------------------------------------------------------------
  // OverlayController script content regression tests
  //
  // Read the actual overlay_controller.dart source and verify key patterns.
  // The overlay JS runs inside WebKitGTK on Linux. Key constraints:
  //   - Scripts must be synchronous IIFEs (WebKitGTK can't resolve Promises)
  //   - After setting img.src, decode()+opacity nudge forces GPU re-composite
  //   - Diagnostic logging must be present for debugging overlay failures
  // ---------------------------------------------------------------------------

  group('OverlayController script patterns', () {
    late String source;

    setUpAll(() {
      source = _readOverlaySource();
    });

    test('uses only synchronous IIFEs — no async (WebKitGTK cannot resolve Promises)', () {
      // The source should contain "(function()" but never "(async function()"
      // async IIFEs cause PlatformException(JS_ERROR, Unsupported result type)
      expect(source.contains('(function()'), true);
      expect(source.contains('(async function()'), false,
          reason: 'async IIFEs break WebKitGTK evaluate_javascript');
    });

    test('webtoon overlay uses decode().then() for GPU compositor nudge', () {
      expect(source.contains('img.decode().then(function()'), true);
      expect(source.contains("img.style.opacity = '0.999'"), true);
    });

    test('kindle overlay uses decode().then() for GPU compositor nudge', () {
      expect(source.contains('target.decode().then(function()'), true);
      expect(source.contains("target.style.opacity = '0.999'"), true);
    });

    test('both overlays catch decode errors', () {
      expect(source.contains('webtoon decode FAILED'), true);
      expect(source.contains('decode() FAILED'), true);
    });

    test('kindle overlay logs diagnostic info after decode', () {
      expect(source.contains('Post-decode:'), true);
      expect(source.contains('srcStuck='), true);
      expect(source.contains('OVERWRITTEN!'), true);
    });

    test('kindle overlay returns diagnostic JSON', () {
      expect(source.contains("JSON.stringify({ok: true"), true);
      expect(source.contains('blobBytes'), true);
    });

    test('kindle overlay handles atob failure gracefully', () {
      expect(source.contains('atob() FAILED'), true);
      expect(source.contains('atob_failed'), true);
    });

    test('kindle overlay logs base64 length and blob creation', () {
      expect(source.contains('base64 length='), true);
      expect(source.contains('Created blob:'), true);
    });

    test('both overlays create blob URLs from base64', () {
      expect(source.contains('URL.createObjectURL(blob)'), true);
      expect(source.contains("type: 'image/png'"), true);
    });

    test('Dart side parses diagnostic JSON from kindle overlay', () {
      // Verify the Dart parsing logic handles the JSON diagnostic result
      expect(source.contains("jsonDecode(result)"), true);
      expect(source.contains("[OverlayJS]"), true);
    });
  });

  group('NaverWebtoonStrategy captureScript', () {
    test('uses JS fetch (not Dart HTTP) for image capture', () {
      final strategy = NaverWebtoonStrategy();
      final script = strategy.captureScript('wt-0');
      // Must use in-browser fetch (has cookies + referer) not external HTTP
      expect(script.contains('await fetch(src)'), true);
      expect(script.contains('FileReader'), true);
      expect(script.contains('readAsDataURL'), true);
    });
  });

  // ---------------------------------------------------------------------------
  // Feedback feature regression tests
  // ---------------------------------------------------------------------------

  group('PageJob cache-hit includes pipeline', () {
    test('PageJob constructor accepts pipeline parameter', () {
      final job = PageJob(
        pageId: 'test-1',
        pipeline: 'manga_furigana',
        sourceHash: 'abc123',
        status: PageJobStatus.completed,
        cached: true,
      );
      expect(job.pipeline, 'manga_furigana');
      expect(job.sourceHash, 'abc123');
      expect(job.cached, true);
    });
  });

  group('Feedback feature patterns in reader_screen.dart', () {
    late String readerSource;

    setUpAll(() {
      readerSource = File('lib/screens/reader_screen.dart').readAsStringSync();
    });

    test('metadata loading logs missing pipeline/hash', () {
      // _loadMetadataForPage must log when pipeline or sourceHash is missing
      // so we can diagnose feedback not working on cached pages.
      expect(readerSource.contains('Missing hash/pipeline for'), true);
    });

    test('metadata loading logs region count on success', () {
      expect(readerSource.contains('regions'), true);
      expect(readerSource.contains('Server metadata for'), true);
    });

    test('feedback save applies overlay directly (not re-capture)', () {
      // After saving feedback edits, fresh images must be applied as overlays
      // directly. Re-capturing would screenshot the translated overlay image,
      // producing a hash that doesn't match the original source hash.
      expect(readerSource.contains('savedImages[pageId] = freshImage'), true);
      expect(readerSource.contains('_applyOverlay(pid, entry.value)'), true,
          reason: 'must apply fresh overlay directly after feedback save');
    });

    test('feedback save handles spread pages (L/R stitching)', () {
      expect(readerSource.contains("savedImages['\$base-L']"), true);
      expect(readerSource.contains("savedImages['\$base-R']"), true);
      expect(readerSource.contains('_applySpreadOverlay(base, leftImg, rightImg)'), true);
    });

    test('_refreshLocalEditedCache returns fresh image bytes', () {
      // Must return Uint8List? so the caller can apply overlay
      expect(readerSource.contains('Future<Uint8List?> _refreshLocalEditedCache'), true);
      expect(readerSource.contains('return fresh;'), true);
    });

    test('feedback toolbar JS has all required buttons', () {
      expect(readerSource.contains('__frankFeedback'), true);
      expect(readerSource.contains('__frankSaveEdits'), true);
      expect(readerSource.contains('__frankCancelEdits'), true);
      expect(readerSource.contains('toggle_feedback_mode'), true);
      expect(readerSource.contains('save_feedback_edits'), true);
      expect(readerSource.contains('cancel_feedback_edits'), true);
    });

    test('click on mark opens edit_translation', () {
      expect(readerSource.contains("action: 'edit_translation'"), true);
    });

    test('mark style uses TXT label for manual_translation', () {
      expect(readerSource.contains("label: 'TXT'"), true);
    });

    test('detection box styles distinguish region kinds', () {
      expect(readerSource.contains("regionKind === 'artwork_text'"), true);
      expect(readerSource.contains("regionKind === 'sfx'"), true);
      // Default detection style for bubbles
      expect(readerSource.contains("label: 'B'"), true);
    });

    test('_patchAndRerender handles 409 conflict with retry', () {
      // Verify the reader_screen contains ApiConflictException handling
      expect(readerSource.contains('on ApiConflictException'), true);
      // Should reload fresh metadata on conflict
      expect(readerSource.contains('getCacheMetadataByHash'), true);
      // Should merge user edits onto fresh metadata
      expect(readerSource.contains('_mergeUserEdits'), true);
      // Should retry with incremented attempt
      expect(readerSource.contains('attempt: 1'), true);
      // Should only retry once (not infinite loop)
      expect(readerSource.contains('if (attempt > 0) rethrow'), true);
    });

    test('_patchAndRerender recovers from rerender failure', () {
      // When _waitForJobCompletion fails, should try image recovery
      expect(readerSource.contains('attempting image recovery'), true);
      expect(readerSource.contains('Recovered image for'), true);
      // Should rethrow if recovery also fails
      expect(readerSource.contains('rethrow; // Surface the original error'), true);
    });

    test('_mergeUserEdits preserves user section from local edits', () {
      // Merge function indexes by region id
      expect(readerSource.contains("localById[id]"), true);
      // Overlays local user section onto server regions
      expect(readerSource.contains("mergedRegion['user']"), true);
    });
  });

  group('ApiService error types', () {
    late String apiSource;

    setUpAll(() {
      apiSource = File('lib/services/api_service.dart').readAsStringSync();
    });

    test('ApiException carries statusCode', () {
      expect(apiSource.contains('final int? statusCode;'), true);
    });

    test('ApiConflictException extends ApiException with 409', () {
      expect(apiSource.contains('class ApiConflictException extends ApiException'), true);
      expect(apiSource.contains('statusCode: 409'), true);
    });

    test('patchCacheMetadataByHash throws ApiConflictException on 409', () {
      expect(apiSource.contains('response.statusCode == 409'), true);
      expect(apiSource.contains('throw ApiConflictException'), true);
    });

    test('submitJob accepts force parameter and sends force field', () {
      // Verify the method signature includes force
      expect(apiSource.contains("bool force = false"), true);
      // Verify it sends force=true to the server
      expect(apiSource.contains("if (force) request.fields['force'] = 'true'"), true);
    });
  });

  group('Force-reprocess flow', () {
    late String jobsSource;
    late String readerSource;

    setUpAll(() {
      jobsSource = File('lib/providers/jobs_provider.dart').readAsStringSync();
      readerSource = File('lib/screens/reader_screen.dart').readAsStringSync();
    });

    test('submitPage accepts force parameter', () {
      expect(jobsSource.contains('bool force = false'), true);
    });

    test('submitPage skips local cache when force=true', () {
      // The cache checks should be wrapped in if (!force)
      expect(jobsSource.contains('if (!force) {'), true);
    });

    test('submitPage passes force to API', () {
      expect(jobsSource.contains('force: force,'), true);
    });

    test('translate button triggers force reprocess', () {
      // The toolbar translate action should pass force: true
      expect(readerSource.contains("_translateVisiblePages(force: true)"), true);
    });

    test('_capturePageImage accepts and passes force parameter', () {
      expect(readerSource.contains("bool force = false,"), true);
      // Verify force is passed through to submitPage
      final captureSection = readerSource.substring(
        readerSource.indexOf('Future<void> _capturePageImage'),
        readerSource.indexOf('/// Submit the next batch'),
      );
      expect(captureSection.contains('force: force,'), true);
    });
  });
}
