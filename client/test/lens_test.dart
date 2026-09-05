import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:frank_client/models/page_job.dart';
import 'package:frank_client/providers/jobs_provider.dart';

/// The lens has two halves: Dart plumbing and an injected JS module. The JS
/// half is covered by test/js/lens_module.test.mjs against a DOM stub; this
/// file runs that suite and pins the Dart-side wiring that it cannot see.
String _read(String path) {
  final file = File(path);
  if (!file.existsSync()) {
    throw StateError('$path not found at ${file.absolute.path}');
  }
  return file.readAsStringSync();
}

void main() {
  group('LensController', () {
    late String source;

    setUpAll(() => source = _read('lib/webview/lens_controller.dart'));

    test('module is injected once per document', () {
      expect(source, contains('if (window.__frankLens) return;'));
    });

    test('translations are handed over without touching the reader image', () {
      // The whole point of lens mode: the original page stays on screen.
      expect(source, contains('target.dataset.frankLensSrc = blobUrl;'));
      expect(source, isNot(contains('target.src = blobUrl;')));
    });

    test('blob urls are revoked when a registration is replaced or dropped', () {
      expect(source, contains('releaseSource(pageId);'));
      expect(source, contains('URL.revokeObjectURL'));
    });

    test('gesture thresholds separate a tap from a peek', () {
      expect(source, contains('var HOLD_MS = 200;'));
      expect(source, contains('var MOVE_CANCEL_PX = 12;'));
    });
  });

  group('reader wiring', () {
    late String reader;

    setUpAll(() => reader = _read('lib/screens/reader_screen.dart'));

    test('lens mode is the default reading mode', () {
      expect(reader, contains('bool _lensMode = true;'));
    });

    test('lens mode short-circuits the full-page swap', () {
      final kindle = reader.indexOf('_applyKindleOverlayBytes');
      final lensBranch = reader.indexOf('if (_lensMode) {', kindle);
      final swap = reader.indexOf('_overlay.replaceVisibleKindlePage', kindle);
      expect(lensBranch, greaterThan(-1));
      expect(
        lensBranch,
        lessThan(swap),
        reason: 'lens registration must pre-empt the src swap',
      );
    });

    test('offers the three documented magnifications', () {
      expect(
        reader,
        contains('_lensZoomSteps = <double>[1.5, 2.0, 3.0]'),
      );
    });

    test('page turns retarget the lens before the next translation lands', () {
      expect(reader, contains('_lens.setActivePage(controller, pageId)'));
    });
  });

  group('job retention', () {
    PageJob completed(String pageId, {bool withImage = true}) => PageJob(
      pageId: pageId,
      status: PageJobStatus.completed,
      translatedImage: withImage ? Uint8List(1024) : null,
      originalImage: withImage ? Uint8List(1024) : null,
    );

    Map<String, PageJob> mapOf(Iterable<PageJob> jobs) => {
      for (final job in jobs) job.pageId: job,
    };

    test('keeps everything while under the retention limit', () {
      final jobs = mapOf([for (var i = 0; i < 5; i++) completed('kindle-$i')]);
      final pruned = JobsNotifier.pruneJobs(jobs);
      expect(pruned, same(jobs));
      expect(pruned.values.every((j) => j.translatedImage != null), isTrue);
    });

    test('drops the bytes of pages the reader has moved past', () {
      final jobs = mapOf([for (var i = 0; i < 6; i++) completed('kindle-$i')]);

      final pruned = JobsNotifier.pruneJobs(jobs, maxImages: 2, maxJobs: 100);

      expect(pruned.length, 6, reason: 'records are cheap; keep them');
      expect(pruned['kindle-0']!.translatedImage, isNull);
      expect(pruned['kindle-0']!.originalImage, isNull);
      expect(pruned['kindle-4']!.translatedImage, isNotNull);
      expect(pruned['kindle-5']!.translatedImage, isNotNull);
    });

    test('drops the oldest records once the map itself grows too long', () {
      final jobs = mapOf([for (var i = 0; i < 6; i++) completed('kindle-$i')]);
      final dropped = <String>[];

      final pruned = JobsNotifier.pruneJobs(
        jobs,
        maxImages: 2,
        maxJobs: 4,
        onDropped: (job) => dropped.add(job.pageId),
      );

      expect(pruned.keys, ['kindle-2', 'kindle-3', 'kindle-4', 'kindle-5']);
      expect(dropped, ['kindle-0', 'kindle-1']);
    });

    test('never evicts a job that is still in flight', () {
      final inFlight = PageJob(
        pageId: 'kindle-0',
        status: PageJobStatus.processing,
      );
      final jobs = mapOf([
        inFlight,
        for (var i = 1; i < 6; i++) completed('kindle-$i'),
      ]);

      final pruned = JobsNotifier.pruneJobs(jobs, maxImages: 1, maxJobs: 2);

      expect(pruned.containsKey('kindle-0'), isTrue);
    });
  });

  group('webtoon prefetch', () {
    late String reader;

    setUpAll(() => reader = _read('lib/screens/reader_screen.dart'));

    test('keeps a batch of pages queued ahead of the reader', () {
      expect(reader, contains('_batchSize = 8'));
      expect(reader, contains('_prefetchThreshold = 4'));
    });
  });

  group('lens module behavior (node)', () {
    test('DOM-stub suite passes', () {
      final probe = Process.runSync('sh', ['-c', 'command -v node']);
      if (probe.exitCode != 0) {
        markTestSkipped('node not installed');
        return;
      }
      final result = Process.runSync('node', [
        '--test',
        'test/js/lens_module.test.mjs',
      ]);
      expect(
        result.exitCode,
        0,
        reason: '${result.stdout}\n${result.stderr}',
      );
    }, timeout: const Timeout(Duration(minutes: 2)));
  });
}
