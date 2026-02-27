/// Metadata parsed from a URL.
class PageMetadata {
  final String title;
  final String chapter;
  final String pageNumber;
  final String sourceUrl;

  const PageMetadata({
    required this.title,
    required this.chapter,
    required this.pageNumber,
    required this.sourceUrl,
  });
}

/// Base class for site-specific image detection and capture strategies.
abstract class SiteStrategy {
  String get siteName;
  String get urlPattern;

  /// The pipeline to use for this site, or null to use user's setting.
  String? get defaultPipeline => null;

  /// JavaScript to inject for detecting pages and scroll monitoring.
  String get detectionScript;

  /// JavaScript to inject for capturing a specific page image as base64.
  String captureScript(String pageId);

  /// Parse URL to extract title/chapter/page metadata.
  PageMetadata? parseUrl(String url);

  /// Whether this strategy matches the given URL.
  bool matches(String url) => RegExp(urlPattern).hasMatch(url);
}
