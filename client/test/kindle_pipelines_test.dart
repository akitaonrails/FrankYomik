import 'package:flutter_test/flutter_test.dart';
import 'package:frank_client/utils/kindle_pipelines.dart';

void main() {
  group('KindlePipelines', () {
    test('cycles furigana, English, then book', () {
      expect(KindlePipelines.next('manga_furigana'), 'manga_translate');
      expect(KindlePipelines.next('manga_translate'), 'book_furigana');
      expect(KindlePipelines.next('book_furigana'), 'manga_furigana');
    });

    test('an unrecognised setting restarts the cycle', () {
      // A volume saved by an older build, or a webtoon default, must not
      // strand the toolbar button.
      expect(KindlePipelines.next('webtoon'), 'manga_furigana');
      expect(KindlePipelines.next(''), 'manga_furigana');
    });

    test('every pipeline has a button label', () {
      for (final pipeline in KindlePipelines.all) {
        expect(KindlePipelines.labelFor(pipeline), isNotEmpty);
      }
      expect(KindlePipelines.labelFor('book_furigana'), 'Book');
    });

    test('falls back to a sane label for an unknown pipeline', () {
      expect(KindlePipelines.labelFor('nonsense'), 'Furigana');
    });

    test('only the book pipeline is prose', () {
      expect(KindlePipelines.isBook('book_furigana'), isTrue);
      expect(KindlePipelines.isBook('manga_furigana'), isFalse);
      expect(KindlePipelines.isBook('manga_translate'), isFalse);
    });

    test('matches the pipelines the server accepts', () {
      // server/worker/job.py VALID_PIPELINES, minus webtoon which is not a
      // Kindle pipeline.
      expect(KindlePipelines.all,
          containsAll(['manga_furigana', 'manga_translate', 'book_furigana']));
    });
  });
}
