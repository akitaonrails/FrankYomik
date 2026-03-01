import 'dart:async';
import 'dart:convert';
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

final jobsProvider = StateNotifierProvider<JobsNotifier, Map<String, PageJob>>((
  ref,
) {
  return JobsNotifier(ref);
});

class JobsNotifier extends StateNotifier<Map<String, PageJob>> {
  final Ref _ref;
  Timer? _pollTimer;

  /// Tracks background metadata-backfill jobs: jobId → {hash, pipeline}.
  final Map<String, ({String hash, String pipeline})> _backfillJobs = {};

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
  /// When [force] is true, bypass all local and server caches and reprocess
  /// from scratch.
  Future<void> submitPage({
    required String pageId,
    required Uint8List imageBytes,
    String? pipeline,
    String? title,
    String? chapter,
    String? pageNumber,
    String? sourceUrl,
    String priority = 'high',
    bool force = false,
  }) async {
    // Check local cache first (hash-based — works for re-visits)
    final effectivePipeline = pipeline ?? _settings.pipeline;
    final hash = await _cache.hashImage(imageBytes);
    if (!force) {
      final cached = await _cache.lookupByHash(hash, effectivePipeline);
      if (cached != null) {
        state = {
          ...state,
          pageId: PageJob(
            pageId: pageId,
            title: title,
            chapter: chapter,
            pageNumber: pageNumber,
            pipeline: effectivePipeline,
            status: PageJobStatus.completed,
            translatedImage: cached,
            cached: true,
            sourceHash: hash,
          ),
        };
        // Backfill metadata for pages cached before metadata storage existed.
        unawaited(_backfillMetadataIfMissing(
          hash: hash,
          pipeline: effectivePipeline,
          imageBytes: imageBytes,
          title: title,
          chapter: chapter,
          pageNumber: pageNumber,
          sourceUrl: sourceUrl,
          priority: 'low',
        ));
        return;
      }

      // Also check by metadata (title/chapter/page)
      if (title != null && chapter != null && pageNumber != null) {
        final metaCached = await _cache.lookupByMetadata(
          effectivePipeline,
          title,
          chapter,
          pageNumber,
        );
        if (metaCached != null) {
          state = {
            ...state,
            pageId: PageJob(
              pageId: pageId,
              title: title,
              chapter: chapter,
              pageNumber: pageNumber,
              pipeline: effectivePipeline,
              status: PageJobStatus.completed,
              translatedImage: metaCached,
              cached: true,
              sourceHash: hash,
            ),
          };
          // Backfill metadata for pages cached before metadata storage existed.
          unawaited(_backfillMetadataIfMissing(
            hash: hash,
            pipeline: effectivePipeline,
            imageBytes: imageBytes,
            title: title,
            chapter: chapter,
            pageNumber: pageNumber,
            sourceUrl: sourceUrl,
            priority: 'low',
          ));
          return;
        }
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
      sourceHash: hash,
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
        force: force,
      );

      final jobId = response['job_id'] as String;
      final isCached = response['cached'] == true;
      job.jobId = jobId;
      job.metaUrl = response['meta_url'] as String?;
      job.sourceHash = (response['source_hash'] as String?) ?? hash;
      job.contentHash = response['content_hash'] as String?;
      job.renderHash = response['render_hash'] as String?;

      if (isCached) {
        // Server had it cached — download immediately
        final imageUrl = response['image_url'] as String?;
        if (imageUrl != null) {
          job.status = PageJobStatus.processing;
          job.stage = 'downloading';
          state = {...state};

          final img = await _api.getJobImage(
            settings: _settings,
            imageUrl: imageUrl,
          );
          job.translatedImage = img;
          job.status = PageJobStatus.completed;
          job.cached = true;
          job.imageUrl = imageUrl;
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
          unawaited(_fetchAndCacheMetadata(hash, effectivePipeline));
        }
      } else {
        // Subscribe for updates
        _ws.subscribeToJobs([jobId]);
        _startPollingFallback();
      }

      state = {...state};
    } catch (e) {
      debugPrint('[Jobs] Submit error for $pageId: $e');
      job.status = PageJobStatus.failed;
      job.error = e.toString();
      state = {...state};
    }
  }

  void _handleWsMessage(Map<String, dynamic> msg) {
    final type = msg['type'] as String?;
    final jobId = msg['job_id'] as String?;
    if (type == null || jobId == null) return;

    // Check if this is a metadata-backfill job (no matching PageJob expected)
    final backfill = _backfillJobs.remove(jobId);
    if (backfill != null && type == 'job_complete') {
      final status = msg['status'] as String?;
      if (status == 'completed') {
        unawaited(_fetchAndCacheMetadata(backfill.hash, backfill.pipeline));
      }
      return;
    }

    // Find the PageJob with this jobId
    final entry = state.entries
        .where((e) => e.value.jobId == jobId)
        .firstOrNull;
    if (entry == null) return;

    final job = entry.value;

    if (type == 'job_progress') {
      job.status = PageJobStatus.processing;
      job.stage = msg['stage'] as String?;
      job.detail = msg['detail'] as String?;
      job.percent = (msg['percent'] as num?)?.toInt() ?? 0;
      state = {...state};
    } else if (type == 'job_complete') {
      final status = msg['status'] as String?;
      if (status == 'completed') {
        job.imageUrl = msg['image_url'] as String?;
        job.metaUrl = msg['meta_url'] as String?;
        job.sourceHash = msg['source_hash'] as String? ?? job.sourceHash;
        job.contentHash = msg['content_hash'] as String?;
        job.renderHash = msg['render_hash'] as String?;
        job.cached = msg['cached'] == true;
        _downloadTranslatedImage(job);
      } else {
        debugPrint('[Jobs] ${entry.key} failed: ${msg['error']}');
        job.status = PageJobStatus.failed;
        job.error = msg['error'] as String? ?? 'Unknown error';
        state = {...state};
      }
    }
  }

  Future<void> _downloadTranslatedImage(PageJob job) async {
    if (job.imageUrl == null) return;

    try {
      final img = await _api.getJobImage(
        settings: _settings,
        imageUrl: job.imageUrl!,
      );
      job.translatedImage = img;
      job.status = PageJobStatus.completed;
      state = {...state};

      // Save to local cache
      if (job.originalImage != null) {
        final hash = await _cache.hashImage(job.originalImage!);
        final effectivePipeline = job.pipeline ?? _settings.pipeline;
        await _cache.store(
          hash: hash,
          pipeline: effectivePipeline,
          imageBytes: img,
          title: job.title,
          chapter: job.chapter,
          pageNumber: job.pageNumber,
        );
        unawaited(_fetchAndCacheMetadata(hash, effectivePipeline));
      }
    } catch (e) {
      job.status = PageJobStatus.failed;
      job.error = 'Download failed: $e';
      state = {...state};
    }
  }

  /// Check if metadata exists locally; if not, resubmit to server in background
  /// so the worker produces metadata. Called for local cache hits on pages that
  /// were cached before metadata storage was introduced.
  Future<void> _backfillMetadataIfMissing({
    required String hash,
    required String pipeline,
    required Uint8List imageBytes,
    String? title,
    String? chapter,
    String? pageNumber,
    String? sourceUrl,
    String priority = 'low',
  }) async {
    // First try the fast path: metadata already in SQLite
    final localJson = await _cache.lookupMetadataByHash(hash, pipeline);
    if (localJson != null) return;

    // Try fetching from server (covers pages processed after Redis bridge fix)
    try {
      final resp = await _api.getCacheMetadataByHash(
        settings: _settings,
        pipeline: pipeline,
        sourceHash: hash,
      );
      final metadataJson = jsonEncode(resp);
      await _cache.updateMetadata(hash, pipeline, metadataJson);
      return;
    } catch (_) {
      // Server doesn't have it either — need to reprocess
    }

    // Resubmit to server so the worker produces fresh metadata.
    try {
      final response = await _api.submitJob(
        settings: _settings,
        imageBytes: imageBytes,
        pipeline: pipeline,
        title: title,
        chapter: chapter,
        pageNumber: pageNumber,
        sourceUrl: sourceUrl,
        priority: priority,
      );
      final jobId = response['job_id'] as String;
      final isCached = response['cached'] == true;

      if (isCached) {
        // Server had it cached (dedup hit) — metadata should be available now
        unawaited(_fetchAndCacheMetadata(hash, pipeline));
      } else {
        // Track for WS/polling completion
        _backfillJobs[jobId] = (hash: hash, pipeline: pipeline);
        _ws.subscribeToJobs([jobId]);
        _startPollingFallback();
      }
    } catch (_) {}
  }

  /// Fetch metadata from server and persist in local SQLite cache.
  /// Non-fatal: logs and returns on failure.
  Future<void> _fetchAndCacheMetadata(String hash, String pipeline) async {
    try {
      final resp = await _api.getCacheMetadataByHash(
        settings: _settings,
        pipeline: pipeline,
        sourceHash: hash,
      );
      final metadataJson = jsonEncode(resp);
      await _cache.updateMetadata(hash, pipeline, metadataJson);
    } catch (_) {
    }
  }

  /// Fallback polling for active jobs when WebSocket is unavailable.
  void _startPollingFallback() {
    // Don't restart if already polling — restarting resets the 3s countdown
    if (_pollTimer != null) return;
    _pollTimer = Timer.periodic(const Duration(seconds: 3), (_) async {
      final activeJobs = state.values
          .where((j) => j.isActive && j.jobId != null)
          .toList();
      if (activeJobs.isEmpty && _backfillJobs.isEmpty) {
        _pollTimer?.cancel();
        _pollTimer = null;
        return;
      }

      for (final job in activeJobs) {
        try {
          final status = await _api.getJobStatus(
            settings: _settings,
            jobId: job.jobId!,
          );
          final jobStatus = status['status'] as String?;
          if (jobStatus == 'completed') {
            job.imageUrl =
                status['image_url'] as String? ??
                '/api/v1/jobs/${job.jobId}/image';
            job.metaUrl = status['meta_url'] as String?;
            job.sourceHash = status['source_hash'] as String? ?? job.sourceHash;
            job.contentHash = status['content_hash'] as String?;
            job.renderHash = status['render_hash'] as String?;
            _downloadTranslatedImage(job);
          } else if (jobStatus == 'failed') {
            debugPrint('[Jobs] ${job.pageId} failed');
            job.status = PageJobStatus.failed;
            job.error = status['error'] as String? ?? 'Failed';
            state = {...state};
          }
        } catch (_) {}
      }

      // Poll backfill jobs for metadata completion
      for (final entry in _backfillJobs.entries.toList()) {
        try {
          final status = await _api.getJobStatus(
            settings: _settings,
            jobId: entry.key,
          );
          final jobStatus = status['status'] as String?;
          if (jobStatus == 'completed') {
            final bf = _backfillJobs.remove(entry.key);
            if (bf != null) {
              unawaited(_fetchAndCacheMetadata(bf.hash, bf.pipeline));
            }
          } else if (jobStatus == 'failed') {
            _backfillJobs.remove(entry.key);
          }
        } catch (_) {}
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
