/// Configuration for a supported site.
class SiteConfig {
  final String name;
  final String displayName;
  final String homeUrl;
  final String urlPattern;
  final String defaultPipeline;

  const SiteConfig({
    required this.name,
    required this.displayName,
    required this.homeUrl,
    required this.urlPattern,
    required this.defaultPipeline,
  });

  static const sites = [
    SiteConfig(
      name: 'kindle',
      displayName: 'Kindle (Amazon JP)',
      homeUrl: 'https://read.amazon.co.jp',
      urlPattern: r'read\.amazon\.co\.jp',
      defaultPipeline: 'manga_translate',
    ),
    SiteConfig(
      name: 'webtoon',
      displayName: 'Webtoon',
      homeUrl: 'https://www.webtoons.com',
      urlPattern: r'webtoons?\.com',
      defaultPipeline: 'webtoon',
    ),
  ];
}
