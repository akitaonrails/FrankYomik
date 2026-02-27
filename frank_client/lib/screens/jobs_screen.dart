import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../models/page_job.dart';
import '../providers/jobs_provider.dart';
import '../widgets/page_status_badge.dart';

class JobsScreen extends ConsumerWidget {
  const JobsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final jobs = ref.watch(jobsProvider);
    final sorted = jobs.values.toList()
      ..sort((a, b) {
        // Active jobs first, then completed, then failed
        final aWeight = a.isActive ? 0 : (a.isComplete ? 1 : 2);
        final bWeight = b.isActive ? 0 : (b.isComplete ? 1 : 2);
        return aWeight.compareTo(bWeight);
      });

    return Scaffold(
      appBar: AppBar(
        title: Text('Jobs (${jobs.length})'),
        actions: [
          if (jobs.isNotEmpty)
            IconButton(
              icon: const Icon(Icons.delete_sweep),
              tooltip: 'Clear completed',
              onPressed: () {
                final completed = jobs.entries
                    .where(
                        (e) => e.value.isComplete || e.value.isFailed)
                    .map((e) => e.key)
                    .toList();
                for (final id in completed) {
                  ref.read(jobsProvider.notifier).removeJob(id);
                }
              },
            ),
        ],
      ),
      body: sorted.isEmpty
          ? const Center(child: Text('No jobs yet'))
          : ListView.builder(
              itemCount: sorted.length,
              itemBuilder: (ctx, i) => _JobTile(job: sorted[i]),
            ),
    );
  }
}

class _JobTile extends StatelessWidget {
  final PageJob job;

  const _JobTile({required this.job});

  @override
  Widget build(BuildContext context) {
    return ListTile(
      leading: PageStatusBadge(status: job.status),
      title: Text(
        job.title ?? job.pageId,
        overflow: TextOverflow.ellipsis,
      ),
      subtitle: Text(_subtitle()),
      trailing: job.translatedImage != null
          ? const Icon(Icons.image, color: Colors.green)
          : null,
      onTap: job.translatedImage != null ? () => _showImage(context) : null,
    );
  }

  String _subtitle() {
    final parts = <String>[];
    if (job.chapter != null) parts.add('Ch.${job.chapter}');
    if (job.pageNumber != null) parts.add('P.${job.pageNumber}');
    if (job.stage != null) parts.add(job.stage!);
    if (job.detail != null && job.detail!.isNotEmpty) {
      parts.add(job.detail!);
    }
    if (job.cached) parts.add('cached');
    if (job.error != null) parts.add(job.error!);
    return parts.join(' | ');
  }

  void _showImage(BuildContext context) {
    if (job.translatedImage == null) return;
    showDialog(
      context: context,
      builder: (ctx) => Dialog(
        child: InteractiveViewer(
          child: Image.memory(job.translatedImage!),
        ),
      ),
    );
  }
}
