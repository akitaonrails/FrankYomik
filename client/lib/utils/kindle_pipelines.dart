/// The pipelines a Kindle page can be sent through.
///
/// Manga and prose both arrive as page images from the Kindle reader, so
/// nothing about the page itself says which pipeline it needs. The reader
/// picks, and the choice is remembered per volume.
class KindlePipelines {
  const KindlePipelines._();

  static const mangaFurigana = 'manga_furigana';
  static const mangaTranslate = 'manga_translate';
  static const bookFurigana = 'book_furigana';

  /// Cycle order for the in-page toolbar button.
  static const all = <String>[mangaFurigana, mangaTranslate, bookFurigana];

  static const _labels = <String, String>{
    mangaFurigana: 'Furigana',
    mangaTranslate: 'English',
    bookFurigana: 'Book',
  };

  /// The next pipeline in the cycle. An unknown value starts over, so a
  /// setting saved by an older build cannot strand the button.
  static String next(String current) {
    final index = all.indexOf(current);
    return index < 0 ? all.first : all[(index + 1) % all.length];
  }

  static String labelFor(String pipeline) => _labels[pipeline] ?? 'Furigana';

  /// Whether [pipeline] annotates a rasterised prose page rather than manga.
  static bool isBook(String pipeline) => pipeline == bookFurigana;

  /// The page mode to actually use, given what detection saw.
  ///
  /// A wide page image is two manga pages side by side — but a novel is
  /// typeset to the window, so on a landscape screen a single prose page is
  /// wide too. Splitting one cuts its columns down the middle, annotates each
  /// half as if it were a page, and stitches something that matches no page.
  static String pageModeFor(String pipeline, String? detected) {
    if (isBook(pipeline)) return 'single';
    return detected == 'spread' ? 'spread' : 'single';
  }

  /// The page id without the marker detection adds for a spread.
  static String pageIdFor(String pipeline, String pageId) =>
      isBook(pipeline) ? pageId.replaceFirst(RegExp(r'-spread$'), '') : pageId;
}
