import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

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
