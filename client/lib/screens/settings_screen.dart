import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../models/server_settings.dart';
import '../providers/connection_provider.dart';
import '../providers/jobs_provider.dart';
import '../providers/settings_provider.dart';

class SettingsScreen extends ConsumerStatefulWidget {
  const SettingsScreen({super.key});

  @override
  ConsumerState<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends ConsumerState<SettingsScreen> {
  late TextEditingController _urlController;
  late TextEditingController _tokenController;
  late String _pipeline;
  late bool _autoTranslate;
  late String _targetLanguage;

  @override
  void initState() {
    super.initState();
    final s = ref.read(settingsProvider);
    _urlController = TextEditingController(text: s.serverUrl);
    _tokenController = TextEditingController(text: s.authToken);
    _pipeline = s.pipeline;
    _autoTranslate = s.autoTranslate;
    _targetLanguage = s.targetLanguage;
  }

  @override
  void dispose() {
    _urlController.dispose();
    _tokenController.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    final settings = ServerSettings(
      serverUrl: _urlController.text.trim(),
      authToken: _tokenController.text.trim(),
      pipeline: _pipeline,
      autoTranslate: _autoTranslate,
      targetLanguage: _targetLanguage,
      isLoaded: true,
    );
    await ref.read(settingsProvider.notifier).update(settings);

    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Settings saved')),
      );
    }
  }

  Future<void> _clearCache() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Clear Cache'),
        content: const Text(
            'Delete all locally cached translations? Pages will be re-translated on next visit.'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: const Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(ctx, true),
              child: const Text('Clear')),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;

    final cache = ref.read(cacheServiceProvider);
    final count = await cache.clearAll();
    ref.read(jobsProvider.notifier).clearAll();

    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Cleared $count cached pages')),
      );
    }
  }

  Future<void> _testConnection() async {
    await _save();
    await ref.read(connectionProvider.notifier).connect();
    final status = ref.read(connectionProvider);

    if (!mounted) return;
    final msg = switch (status) {
      ConnectionStatus.connected => 'Connected successfully',
      ConnectionStatus.error => 'Connection failed — check URL and token',
      _ => 'Connecting...',
    };
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(msg)));
  }

  @override
  Widget build(BuildContext context) {
    final connStatus = ref.watch(connectionProvider);

    return Scaffold(
      appBar: AppBar(title: const Text('Settings')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          TextField(
            controller: _urlController,
            decoration: const InputDecoration(
              labelText: 'Server URL',
              hintText: 'http://localhost:8080',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _tokenController,
            obscureText: true,
            decoration: const InputDecoration(
              labelText: 'Auth Token',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 16),
          DropdownButtonFormField<String>(
            initialValue: _pipeline,
            decoration: const InputDecoration(
              labelText: 'Kindle Pipeline (Japanese)',
              helperText: 'Webtoon always uses Korean pipeline',
              border: OutlineInputBorder(),
            ),
            items: const [
              DropdownMenuItem(
                  value: 'manga_furigana',
                  child: Text('Furigana (add reading aids)')),
              DropdownMenuItem(
                  value: 'manga_translate',
                  child: Text('Translate')),
            ],
            onChanged: (v) => setState(() => _pipeline = v ?? _pipeline),
          ),
          const SizedBox(height: 16),
          DropdownButtonFormField<String>(
            initialValue: _targetLanguage,
            decoration: const InputDecoration(
              labelText: 'Target Language',
              helperText: 'Language for translation output',
              border: OutlineInputBorder(),
            ),
            items: ServerSettings.targetLanguages.entries
                .map((e) => DropdownMenuItem(
                      value: e.key,
                      child: Text(e.value),
                    ))
                .toList(),
            onChanged: (v) =>
                setState(() => _targetLanguage = v ?? _targetLanguage),
          ),
          SwitchListTile(
            title: const Text('Auto-translate'),
            subtitle: const Text('Intercept and translate pages automatically'),
            value: _autoTranslate,
            onChanged: (v) => setState(() => _autoTranslate = v),
          ),
          const SizedBox(height: 24),
          Row(
            children: [
              Expanded(
                child: FilledButton.icon(
                  onPressed: _save,
                  icon: const Icon(Icons.save),
                  label: const Text('Save'),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: OutlinedButton.icon(
                  onPressed: _testConnection,
                  icon: const Icon(Icons.wifi_tethering),
                  label: const Text('Test'),
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          Center(
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(
                  connStatus == ConnectionStatus.connected
                      ? Icons.check_circle
                      : Icons.circle_outlined,
                  size: 14,
                  color: connStatus == ConnectionStatus.connected
                      ? Colors.green
                      : Colors.grey,
                ),
                const SizedBox(width: 6),
                Text(
                  connStatus.name,
                  style: TextStyle(
                    color: connStatus == ConnectionStatus.connected
                        ? Colors.green
                        : Colors.grey,
                  ),
                ),
              ],
            ),
          ),
          const Divider(height: 32),
          OutlinedButton.icon(
            onPressed: _clearCache,
            icon: const Icon(Icons.delete_outline),
            label: const Text('Clear Translation Cache'),
            style: OutlinedButton.styleFrom(
              foregroundColor: Colors.red,
            ),
          ),
        ],
      ),
    );
  }
}
