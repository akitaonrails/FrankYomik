import 'package:flutter/material.dart';
import '../models/page_job.dart';

/// Small icon badge showing per-page translation status.
class PageStatusBadge extends StatelessWidget {
  final PageJobStatus status;
  final double size;

  const PageStatusBadge({super.key, required this.status, this.size = 20});

  @override
  Widget build(BuildContext context) {
    final (icon, color) = switch (status) {
      PageJobStatus.pending => (Icons.hourglass_empty, Colors.grey),
      PageJobStatus.queued => (Icons.cloud_upload, Colors.blue),
      PageJobStatus.processing => (Icons.sync, Colors.amber),
      PageJobStatus.completed => (Icons.check_circle, Colors.green),
      PageJobStatus.failed => (Icons.error, Colors.red),
    };

    return Icon(icon, size: size, color: color);
  }
}
