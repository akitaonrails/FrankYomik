import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../providers/connection_provider.dart';

class ConnectionBanner extends ConsumerWidget {
  const ConnectionBanner({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final status = ref.watch(connectionProvider);

    if (status == ConnectionStatus.connected) return const SizedBox.shrink();

    final (color, icon, text) = switch (status) {
      ConnectionStatus.connecting => (
          Colors.amber,
          Icons.sync,
          'Connecting...'
        ),
      ConnectionStatus.error => (
          Colors.red,
          Icons.error_outline,
          'Connection failed'
        ),
      ConnectionStatus.disconnected => (
          Colors.grey,
          Icons.cloud_off,
          'Disconnected'
        ),
      _ => (Colors.grey, Icons.cloud_off, 'Unknown'),
    };

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      color: color.withAlpha(30),
      child: Row(
        children: [
          Icon(icon, size: 16, color: color),
          const SizedBox(width: 8),
          Text(text, style: TextStyle(color: color, fontSize: 13)),
          const Spacer(),
          TextButton(
            onPressed: () =>
                ref.read(connectionProvider.notifier).connect(),
            child: Text('Retry', style: TextStyle(color: color)),
          ),
        ],
      ),
    );
  }
}
