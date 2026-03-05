/// Server connection settings.
class ServerSettings {
  final String serverUrl;
  final String authToken;
  final String pipeline;
  final int prefetchPages;
  final bool autoTranslate;
  final String targetLanguage;

  const ServerSettings({
    this.serverUrl = 'http://localhost:8080',
    this.authToken = '',
    this.pipeline = 'manga_translate',
    this.prefetchPages = 2,
    this.autoTranslate = true,
    this.targetLanguage = 'en',
  });

  ServerSettings copyWith({
    String? serverUrl,
    String? authToken,
    String? pipeline,
    int? prefetchPages,
    bool? autoTranslate,
    String? targetLanguage,
  }) {
    return ServerSettings(
      serverUrl: serverUrl ?? this.serverUrl,
      authToken: authToken ?? this.authToken,
      pipeline: pipeline ?? this.pipeline,
      prefetchPages: prefetchPages ?? this.prefetchPages,
      autoTranslate: autoTranslate ?? this.autoTranslate,
      targetLanguage: targetLanguage ?? this.targetLanguage,
    );
  }

  bool get isConfigured => authToken.isNotEmpty;

  Uri get baseUri => Uri.parse(serverUrl);

  static const pipelines = ['manga_translate', 'manga_furigana', 'webtoon'];

  static const targetLanguages = {
    'en': 'English',
    'pt-br': 'Brazilian Portuguese',
  };
}
