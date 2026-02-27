import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../models/site_config.dart';
import '../providers/connection_provider.dart';
import '../providers/settings_provider.dart';
import '../widgets/connection_banner.dart';
import 'reader_screen.dart';
import 'settings_screen.dart';
import 'jobs_screen.dart';

class HomeScreen extends ConsumerStatefulWidget {
  const HomeScreen({super.key});

  @override
  ConsumerState<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends ConsumerState<HomeScreen> {
  final _urlController = TextEditingController();

  @override
  void initState() {
    super.initState();
    // Auto-connect if settings are configured
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final settings = ref.read(settingsProvider);
      if (settings.isConfigured) {
        ref.read(connectionProvider.notifier).connect();
      }
    });
  }

  @override
  void dispose() {
    _urlController.dispose();
    super.dispose();
  }

  void _openUrl(String url) {
    if (url.isEmpty) return;
    if (!url.startsWith('http')) url = 'https://$url';
    Navigator.push(
      context,
      MaterialPageRoute(builder: (_) => ReaderScreen(initialUrl: url)),
    );
  }

  @override
  Widget build(BuildContext context) {
    final connStatus = ref.watch(connectionProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Frank Manga'),
        actions: [
          IconButton(
            icon: const Icon(Icons.list_alt),
            tooltip: 'Jobs',
            onPressed: () => Navigator.push(
              context,
              MaterialPageRoute(builder: (_) => const JobsScreen()),
            ),
          ),
          IconButton(
            icon: const Icon(Icons.settings),
            tooltip: 'Settings',
            onPressed: () => Navigator.push(
              context,
              MaterialPageRoute(builder: (_) => const SettingsScreen()),
            ),
          ),
        ],
      ),
      body: Column(
        children: [
          const ConnectionBanner(),
          Expanded(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  // URL bar
                  TextField(
                    controller: _urlController,
                    decoration: InputDecoration(
                      hintText: 'Enter URL or pick a site below',
                      prefixIcon: const Icon(Icons.language),
                      suffixIcon: IconButton(
                        icon: const Icon(Icons.arrow_forward),
                        onPressed: () => _openUrl(_urlController.text),
                      ),
                      border: const OutlineInputBorder(),
                    ),
                    onSubmitted: _openUrl,
                  ),
                  const SizedBox(height: 24),
                  Text(
                    'Quick Launch',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                  const SizedBox(height: 12),
                  // Site cards
                  ...SiteConfig.sites.map((site) => Card(
                        child: ListTile(
                          leading: Icon(
                            site.name == 'kindle'
                                ? Icons.menu_book
                                : Icons.web,
                            size: 32,
                          ),
                          title: Text(site.displayName),
                          subtitle: Text(site.homeUrl),
                          trailing: const Icon(Icons.open_in_new),
                          onTap: () => _openUrl(site.homeUrl),
                        ),
                      )),
                  const Spacer(),
                  // Connection status
                  Center(
                    child: Chip(
                      avatar: Icon(
                        connStatus == ConnectionStatus.connected
                            ? Icons.cloud_done
                            : Icons.cloud_off,
                        size: 18,
                      ),
                      label: Text(
                        connStatus == ConnectionStatus.connected
                            ? 'Server connected'
                            : 'Not connected',
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}
