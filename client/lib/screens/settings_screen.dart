import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../models/server_settings.dart';
import '../providers/connection_provider.dart';
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
  late int _prefetchPages;
  late bool _autoTranslate;

  @override
  void initState() {
    super.initState();
    final s = ref.read(settingsProvider);
    _urlController = TextEditingController(text: s.serverUrl);
    _tokenController = TextEditingController(text: s.authToken);
    _pipeline = s.pipeline;
    _prefetchPages = s.prefetchPages;
    _autoTranslate = s.autoTranslate;
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
      prefetchPages: _prefetchPages,
      autoTranslate: _autoTranslate,
    );
    await ref.read(settingsProvider.notifier).update(settings);

    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Settings saved')),
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
              helperText: 'Webtoon always uses Korean→English pipeline',
              border: OutlineInputBorder(),
            ),
            items: const [
              DropdownMenuItem(
                  value: 'manga_furigana',
                  child: Text('Furigana (add reading aids)')),
              DropdownMenuItem(
                  value: 'manga_translate',
                  child: Text('Translate (Japanese→English)')),
            ],
            onChanged: (v) => setState(() => _pipeline = v ?? _pipeline),
          ),
          const SizedBox(height: 16),
          Row(
            children: [
              Expanded(
                child: Text('Prefetch pages: $_prefetchPages'),
              ),
              Slider(
                value: _prefetchPages.toDouble(),
                min: 0,
                max: 5,
                divisions: 5,
                label: '$_prefetchPages',
                onChanged: (v) =>
                    setState(() => _prefetchPages = v.toInt()),
              ),
            ],
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
        ],
      ),
    );
  }
}
