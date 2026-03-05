/// Server connection settings.
class ServerSettings {
  final String serverUrl;
  final String authToken;
  final String pipeline;
  final bool autoTranslate;
  final String targetLanguage;

  const ServerSettings({
    this.serverUrl = 'https://localhost:8080',
    this.authToken = 'mysecrettoken',
    this.pipeline = 'manga_translate',
    this.autoTranslate = true,
    this.targetLanguage = 'en',
  });

  ServerSettings copyWith({
    String? serverUrl,
    String? authToken,
    String? pipeline,
    bool? autoTranslate,
    String? targetLanguage,
  }) {
    return ServerSettings(
      serverUrl: serverUrl ?? this.serverUrl,
      authToken: authToken ?? this.authToken,
      pipeline: pipeline ?? this.pipeline,
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
