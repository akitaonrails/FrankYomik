import 'package:flutter/material.dart';
import '../models/page_job.dart';

/// Floating chip showing translation progress at bottom of reader.
class TranslationProgressChip extends StatelessWidget {
  final PageJob job;

  const TranslationProgressChip({super.key, required this.job});

  @override
  Widget build(BuildContext context) {
    final (color, label) = _statusInfo();

    return Card(
      color: color.withAlpha(230),
      elevation: 4,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (job.isActive) ...[
              SizedBox(
                width: 14,
                height: 14,
                child: CircularProgressIndicator(
                  strokeWidth: 2,
                  value: job.percent > 0 ? job.percent / 100 : null,
                  color: Colors.white,
                ),
              ),
              const SizedBox(width: 8),
            ],
            if (job.isComplete)
              const Icon(Icons.check_circle, size: 14, color: Colors.white),
            if (job.isFailed)
              const Icon(Icons.error, size: 14, color: Colors.white),
            if (job.isComplete || job.isFailed) const SizedBox(width: 6),
            Flexible(
              child: Text(
                label,
                style: const TextStyle(color: Colors.white, fontSize: 12),
                overflow: TextOverflow.ellipsis,
              ),
            ),
          ],
        ),
      ),
    );
  }

  (Color, String) _statusInfo() {
    return switch (job.status) {
      PageJobStatus.pending => (Colors.grey, 'Pending...'),
      PageJobStatus.queued => (Colors.blue, 'Queued'),
      PageJobStatus.processing => (
          Colors.amber.shade800,
          _processingLabel(),
        ),
      PageJobStatus.completed =>
        (Colors.green, job.cached ? 'Cached' : 'Done'),
      PageJobStatus.failed => (Colors.red, 'Failed'),
    };
  }

  String _processingLabel() {
    final stage = job.stage ?? 'processing';
    final detail = job.detail;
    final stageLabel = stage.replaceAll('_', ' ');
    if (detail != null && detail.isNotEmpty) {
      return '$stageLabel ($detail)';
    }
    return stageLabel;
  }
}
