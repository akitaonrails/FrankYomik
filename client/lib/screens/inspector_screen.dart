import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../webview/dom_inspector.dart';

class InspectorScreen extends StatefulWidget {
  final DomInspector inspector;

  const InspectorScreen({super.key, required this.inspector});

  @override
  State<InspectorScreen> createState() => _InspectorScreenState();
}

class _InspectorScreenState extends State<InspectorScreen> {
  String _filter = '';

  static const _kindleFilterPrefix = 'kindle_';

  @override
  Widget build(BuildContext context) {
    final logs = widget.inspector.logs;
    final filtered = _filter.isEmpty
        ? logs
        : _filter == _kindleFilterPrefix
            ? logs
                .where((l) =>
                    (l['type'] as String? ?? '').startsWith(_kindleFilterPrefix))
                .toList()
            : logs
                .where((l) =>
                    l['type'] == _filter ||
                    (l.toString()
                        .toLowerCase()
                        .contains(_filter.toLowerCase())))
                .toList();

    return Scaffold(
      appBar: AppBar(
        title: Text('Inspector (${logs.length} entries)'),
        actions: [
          IconButton(
            icon: const Icon(Icons.copy),
            tooltip: 'Copy as JSON',
            onPressed: () {
              Clipboard.setData(
                ClipboardData(
                    text: const JsonEncoder.withIndent('  ').convert(logs)),
              );
              ScaffoldMessenger.of(context).showSnackBar(
                const SnackBar(content: Text('Copied to clipboard')),
              );
            },
          ),
          IconButton(
            icon: const Icon(Icons.delete),
            tooltip: 'Clear logs',
            onPressed: () {
              widget.inspector.clear();
              setState(() {});
            },
          ),
        ],
      ),
      body: Column(
        children: [
          // Filter chips
          Padding(
            padding: const EdgeInsets.all(8),
            child: Wrap(
              spacing: 8,
              children: [
                _chip('All', ''),
                _chip('Images', 'image'),
                _chip('Canvas', 'canvas'),
                _chip('Taps', 'tap'),
                _chip('Summary', 'summary'),
                _chip('Kindle', _kindleFilterPrefix),
              ],
            ),
          ),
          const Divider(height: 1),
          // Log list
          Expanded(
            child: filtered.isEmpty
                ? const Center(
                    child: Text('No logs yet.\nNavigate and interact '
                        'with the page to see entries.'))
                : ListView.builder(
                    itemCount: filtered.length,
                    itemBuilder: (ctx, i) =>
                        _LogTile(entry: filtered[i]),
                  ),
          ),
        ],
      ),
    );
  }

  Widget _chip(String label, String value) {
    final selected = _filter == value;
    return FilterChip(
      label: Text(label),
      selected: selected,
      onSelected: (_) => setState(() => _filter = value),
    );
  }
}

class _LogTile extends StatelessWidget {
  final Map<String, dynamic> entry;

  const _LogTile({required this.entry});

  @override
  Widget build(BuildContext context) {
    final type = entry['type'] ?? 'unknown';
    final isKindle = (type as String).startsWith('kindle_');
    final icon = switch (type) {
      'image' => Icons.image,
      'canvas' => Icons.crop_square,
      'tap' => Icons.touch_app,
      'summary' => Icons.info_outline,
      _ when isKindle => Icons.menu_book,
      _ => Icons.description,
    };

    String subtitle;
    if (type == 'image') {
      subtitle =
          '${entry['src'] ?? ''}\n${entry['naturalWidth']}x${entry['naturalHeight']}';
      if (entry['classes'] != null && (entry['classes'] as String).isNotEmpty) {
        subtitle += '\nclass="${entry['classes']}"';
      }
    } else if (type == 'tap') {
      subtitle = '<${entry['tag']}> ${entry['classes'] ?? ''}\n'
          '${entry['rect'] ?? ''}';
    } else if (type == 'summary') {
      subtitle =
          'imgs:${entry['images']} canvas:${entry['canvases']} iframes:${entry['iframes']}';
    } else if (isKindle) {
      subtitle = _kindleSubtitle(type, entry);
    } else {
      subtitle = entry.toString();
    }

    return ListTile(
      dense: true,
      leading: Icon(icon, size: 20),
      title: Text(type, style: const TextStyle(fontWeight: FontWeight.bold)),
      subtitle:
          Text(subtitle, style: const TextStyle(fontSize: 11), maxLines: 4),
      onTap: () => _showDetail(context),
    );
  }

  String _kindleSubtitle(String type, Map<String, dynamic> entry) {
    return switch (type) {
      'kindle_detect' =>
        'page:${entry['pageId']} mode:${entry['pageMode']}\n'
            'rect:${entry['readerRect']} dpr:${entry['devicePixelRatio']}',
      'kindle_capture' =>
        'page:${entry['pageId']} mode:${entry['pageMode']}\n'
            'screenshot:${entry['screenshotSize']} crop:${entry['cropRect']}',
      'kindle_split' =>
        'spread:${entry['spreadPageId']}\n'
            'L:${entry['leftSize']} R:${entry['rightSize']}',
      'kindle_overlay' =>
        'page:${entry['pageId']} count:${entry['overlayCount']}\n'
            'rect:${entry['overlayRect']}',
      'kindle_clear' =>
        'reason:${entry['reason']} cleared:${entry['clearedCount']}',
      'kindle_resize' =>
        'old:${entry['oldSize']} new:${entry['newSize']}',
      'kindle_dom' =>
        'viewport:${entry['viewport']}\n'
            'canvases:${(entry['canvases'] as List?)?.length ?? 0} '
            'spread:${entry['spreadDetected']}',
      _ => entry.toString(),
    };
  }

  void _showDetail(BuildContext context) {
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(entry['type'] ?? 'Detail'),
        content: SingleChildScrollView(
          child: SelectableText(
            const JsonEncoder.withIndent('  ').convert(entry),
            style: const TextStyle(fontSize: 12, fontFamily: 'monospace'),
          ),
        ),
        actions: [
          TextButton(
            onPressed: () {
              Clipboard.setData(ClipboardData(
                  text: const JsonEncoder.withIndent('  ').convert(entry)));
              Navigator.pop(ctx);
            },
            child: const Text('Copy'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('Close'),
          ),
        ],
      ),
    );
  }
}
