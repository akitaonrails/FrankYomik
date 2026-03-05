import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../models/server_settings.dart';

final settingsProvider =
    StateNotifierProvider<SettingsNotifier, ServerSettings>((ref) {
  return SettingsNotifier();
});

class SettingsNotifier extends StateNotifier<ServerSettings> {
  SettingsNotifier() : super(const ServerSettings()) {
    _load();
  }

  Future<void> _load() async {
    final prefs = await SharedPreferences.getInstance();
    state = ServerSettings(
      serverUrl: prefs.getString('server_url') ?? 'http://localhost:8080',
      authToken: prefs.getString('auth_token') ?? '',
      pipeline: prefs.getString('pipeline') ?? 'manga_furigana',
      prefetchPages: prefs.getInt('prefetch_pages') ?? 2,
      autoTranslate: prefs.getBool('auto_translate') ?? true,
      targetLanguage: prefs.getString('target_language') ?? 'en',
    );
  }

  Future<void> update(ServerSettings settings) async {
    state = settings;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('server_url', settings.serverUrl);
    await prefs.setString('auth_token', settings.authToken);
    await prefs.setString('pipeline', settings.pipeline);
    await prefs.setInt('prefetch_pages', settings.prefetchPages);
    await prefs.setBool('auto_translate', settings.autoTranslate);
    await prefs.setString('target_language', settings.targetLanguage);
  }
}
