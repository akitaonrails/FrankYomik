import 'dart:async';
import 'dart:typed_data';
import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../models/page_job.dart';
import '../models/server_settings.dart';
import '../services/api_service.dart';
import '../services/cache_service.dart';
import '../services/websocket_service.dart';
import 'connection_provider.dart';
import 'settings_provider.dart';

final cacheServiceProvider = Provider<CacheService>((ref) {
  final cache = CacheService();
  cache.init();
  ref.onDispose(() => cache.dispose());
  return cache;
});

final jobsProvider =
    StateNotifierProvider<JobsNotifier, Map<String, PageJob>>((ref) {
  return JobsNotifier(ref);
});

class JobsNotifier extends StateNotifier<Map<String, PageJob>> {
  final Ref _ref;
  Timer? _pollTimer;

  JobsNotifier(this._ref) : super({}) {
    // Listen for WebSocket messages
    final ws = _ref.read(wsServiceProvider);
    ws.onMessage = _handleWsMessage;
  }

  ServerSettings get _settings => _ref.read(settingsProvider);
  ApiService get _api => _ref.read(apiServiceProvider);
  WebSocketService get _ws => _ref.read(wsServiceProvider);
  CacheService get _cache => _ref.read(cacheServiceProvider);

  /// Submit a page for translation.
  Future<void> submitPage({
    required String pageId,
    required Uint8List imageBytes,
    String? pipeline,
    String? title,
    String? chapter,
    String? pageNumber,
    String? sourceUrl,
    String priority = 'high',
  }) async {
    // Check local cache first
    final effectivePipeline = pipeline ?? _settings.pipeline;
    final hash = _cache.hashImage(imageBytes);
    final cached = await _cache.lookupByHash(hash, effectivePipeline);
    if (cached != null) {
      state = {
        ...state,
        pageId: PageJob(
          pageId: pageId,
          title: title,
          chapter: chapter,
          pageNumber: pageNumber,
          status: PageJobStatus.completed,
          translatedImage: cached,
          cached: true,
        ),
      };
      return;
    }

    // Also check by metadata
    if (title != null && chapter != null && pageNumber != null) {
      final metaCached = await _cache.lookupByMetadata(
          effectivePipeline, title, chapter, pageNumber);
      if (metaCached != null) {
        state = {
          ...state,
          pageId: PageJob(
            pageId: pageId,
            title: title,
            chapter: chapter,
            pageNumber: pageNumber,
            status: PageJobStatus.completed,
            translatedImage: metaCached,
            cached: true,
          ),
        };
        return;
      }
    }

    // Create pending job
    final job = PageJob(
      pageId: pageId,
      title: title,
      chapter: chapter,
      pageNumber: pageNumber,
      sourceUrl: sourceUrl,
      pipeline: effectivePipeline,
      originalImage: imageBytes,
      status: PageJobStatus.queued,
    );
    state = {...state, pageId: job};

    try {
      final response = await _api.submitJob(
        settings: _settings,
        imageBytes: imageBytes,
        pipeline: effectivePipeline,
        title: title,
        chapter: chapter,
        pageNumber: pageNumber,
        sourceUrl: sourceUrl,
        priority: priority,
      );

      final jobId = response['job_id'] as String;
      final isCached = response['cached'] == true;
      job.jobId = jobId;

      if (isCached) {
        // Server had it cached — download immediately
        final imageUrl = response['image_url'] as String?;
        if (imageUrl != null) {
          job.status = PageJobStatus.processing;
          job.stage = 'downloading';
          state = {...state};

          final img = await _api.getJobImage(
              settings: _settings, imageUrl: imageUrl);
          job.translatedImage = img;
          job.status = PageJobStatus.completed;
          job.cached = true;
          state = {...state};

          // Save to local cache
          await _cache.store(
            hash: hash,
            pipeline: effectivePipeline,
            imageBytes: img,
            title: title,
            chapter: chapter,
            pageNumber: pageNumber,
          );
        }
      } else {
        // Subscribe for updates
        _ws.subscribeToJobs([jobId]);
        _startPollingFallback();
      }

      state = {...state};
    } catch (e) {
      job.status = PageJobStatus.failed;
      job.error = e.toString();
      state = {...state};
    }
  }

  void _handleWsMessage(Map<String, dynamic> msg) {
    final type = msg['type'] as String?;
    final jobId = msg['job_id'] as String?;
    debugPrint('[Jobs] WS message: type=$type, jobId=$jobId');
    if (type == null || jobId == null) return;

    // Find the PageJob with this jobId
    final entry = state.entries.where((e) => e.value.jobId == jobId).firstOrNull;
    if (entry == null) {
      debugPrint('[Jobs] WS: No matching job for $jobId');
      return;
    }

    final job = entry.value;
    debugPrint('[Jobs] WS: Matched ${entry.key} (jobId=$jobId)');

    if (type == 'job_progress') {
      job.status = PageJobStatus.processing;
      job.stage = msg['stage'] as String?;
      job.detail = msg['detail'] as String?;
      job.percent = (msg['percent'] as num?)?.toInt() ?? 0;
      state = {...state};
    } else if (type == 'job_complete') {
      final status = msg['status'] as String?;
      debugPrint('[Jobs] WS: Job ${entry.key} complete, status=$status');
      if (status == 'completed') {
        job.imageUrl = msg['image_url'] as String?;
        job.cached = msg['cached'] == true;
        _downloadTranslatedImage(job);
      } else {
        job.status = PageJobStatus.failed;
        job.error = msg['error'] as String? ?? 'Unknown error';
        state = {...state};
      }
    }
  }

  Future<void> _downloadTranslatedImage(PageJob job) async {
    if (job.imageUrl == null) return;

    try {
      debugPrint('[Jobs] Downloading translated image for ${job.pageId} from ${job.imageUrl}');
      final img =
          await _api.getJobImage(settings: _settings, imageUrl: job.imageUrl!);
      debugPrint('[Jobs] Downloaded ${img.length} bytes for ${job.pageId}, applying to state');
      job.translatedImage = img;
      job.status = PageJobStatus.completed;
      state = {...state};

      // Save to local cache
      if (job.originalImage != null) {
        final hash = _cache.hashImage(job.originalImage!);
        await _cache.store(
          hash: hash,
          pipeline: job.pipeline ?? _settings.pipeline,
          imageBytes: img,
          title: job.title,
          chapter: job.chapter,
          pageNumber: job.pageNumber,
        );
      }
    } catch (e) {
      job.status = PageJobStatus.failed;
      job.error = 'Download failed: $e';
      state = {...state};
    }
  }

  /// Fallback polling for active jobs when WebSocket is unavailable.
  void _startPollingFallback() {
    // Don't restart if already polling — restarting resets the 3s countdown
    if (_pollTimer != null) return;
    _pollTimer = Timer.periodic(const Duration(seconds: 3), (_) async {
      final activeJobs =
          state.values.where((j) => j.isActive && j.jobId != null).toList();
      if (activeJobs.isEmpty) {
        _pollTimer?.cancel();
        _pollTimer = null;
        return;
      }

      for (final job in activeJobs) {
        try {
          final status = await _api.getJobStatus(
              settings: _settings, jobId: job.jobId!);
          final jobStatus = status['status'] as String?;
          if (jobStatus == 'completed') {
            debugPrint('[Jobs] Poll: ${job.pageId} (${job.jobId}) completed, downloading image');
            job.imageUrl =
                status['image_url'] as String? ?? '/api/v1/jobs/${job.jobId}/image';
            _downloadTranslatedImage(job);
          } else if (jobStatus == 'failed') {
            debugPrint('[Jobs] Poll: ${job.pageId} (${job.jobId}) failed');
            job.status = PageJobStatus.failed;
            job.error = status['error'] as String? ?? 'Failed';
            state = {...state};
          }
        } catch (e) {
          debugPrint('[Jobs] Poll error for ${job.pageId}: $e');
        }
      }
    });
  }

  void removeJob(String pageId) {
    final updated = Map<String, PageJob>.from(state);
    final job = updated.remove(pageId);
    if (job?.jobId != null) {
      _ws.unsubscribeFromJobs([job!.jobId!]);
    }
    state = updated;
  }

  @override
  void dispose() {
    _pollTimer?.cancel();
    super.dispose();
  }
}
