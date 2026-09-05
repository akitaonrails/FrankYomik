import 'dart:async';
import 'dart:io';
import 'dart:math';
import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:http/http.dart' as http;
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
  /// Translated pages are full PNGs — a few MB each. A long Kindle session
  /// would otherwise hold every page it ever rendered, so only the most recent
  /// ones keep their bytes; the job records themselves stay (they are tiny and
  /// carry the status the UI reports).
  static const _maxRetainedImages = 20;

  /// Page size varies by an order of magnitude — a manga page is small, a
  /// full-resolution prose page with furigana is not — so the real limit is
  /// bytes rather than pages. Whichever bound is reached first wins.
  static const _maxRetainedImageBytes = 48 * 1024 * 1024;

  /// Records for pages the reader left long ago are dropped outright. Kindle
  /// pageIds carry a per-session counter, so an evicted page never comes back
  /// under the same id; a webtoon page that scrolls back into view is
  /// re-submitted and served from the server cache.
  static const _maxRetainedJobs = 200;

  final Ref _ref;
  Timer? _pollTimer;
  final Set<String> _downloadingJobIds = {};

  JobsNotifier(this._ref) : super({}) {
    // Listen for WebSocket messages
    final ws = _ref.read(wsServiceProvider);
    ws.onMessage = _handleWsMessage;
  }

  ServerSettings get _settings => _ref.read(settingsProvider);
  ApiService get _api => _ref.read(apiServiceProvider);
  WebSocketService get _ws => _ref.read(wsServiceProvider);
  CacheService get _cache => _ref.read(cacheServiceProvider);

  Future<T> _withRetry<T>(
    Future<T> Function() action, {
    int maxAttempts = 3,
  }) async {
    for (var attempt = 1; attempt <= maxAttempts; attempt++) {
      try {
        return await action();
      } on TimeoutException {
        if (attempt == maxAttempts) rethrow;
      } on ApiException catch (e) {
        if (!e.retryable || attempt == maxAttempts) rethrow;
      } on SocketException {
        if (attempt == maxAttempts) rethrow;
      } on http.ClientException {
        if (attempt == maxAttempts) rethrow;
      }
      debugPrint('[Jobs] Retry attempt $attempt/$maxAttempts after error');
      await Future.delayed(Duration(seconds: min(1 << attempt, 4)));
    }
    throw StateError('unreachable');
  }

  /// Submit a page for translation.
  Future<void> submitPage({
    required String pageId,
    required Uint8List imageBytes,
    String? pipeline,
    String? title,
    String? chapter,
    String? pageNumber,
    String? sourceUrl,
    String? sourceSite,
    String? latestGroup,
    String? latestToken,
    int? latestSeq,
    String priority = 'high',
    String? targetLanguage,
  }) async {
    // Check local cache first (hash-based — works for re-visits)
    final effectivePipeline = pipeline ?? _settings.pipeline;
    final effectiveLang = targetLanguage ?? _settings.targetLanguage;
    // Use composite pipeline for cache keys when target lang is not English
    final cachePipeline = effectiveLang == 'en'
        ? effectivePipeline
        : '${effectivePipeline}_$effectiveLang';
    final hash = await _cache.hashImage(imageBytes);
    final cached = await _cache.lookupByHash(hash, cachePipeline);
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
      return;
    }

    // Also check by metadata (title/chapter/page) — but only if the
    // source hash matches.  The Kindle reader loads different resolution
    // images depending on window size; a metadata hit from a smaller
    // screenshot would serve a worse translation for the larger image.
    if (title != null && chapter != null && pageNumber != null) {
      final metaHash = await _cache.lookupHashByMetadata(
        cachePipeline,
        title,
        chapter,
        pageNumber,
      );
      if (metaHash != null && metaHash == hash) {
        final metaCached = await _cache.lookupByHash(hash, cachePipeline);
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
      final response = await _withRetry(
        () => _api.submitJob(
          settings: _settings,
          imageBytes: imageBytes,
          pipeline: effectivePipeline,
          title: title,
          chapter: chapter,
          pageNumber: pageNumber,
          sourceUrl: sourceUrl,
          sourceSite: sourceSite,
          latestGroup: latestGroup,
          latestToken: latestToken,
          latestSeq: latestSeq,
          priority: priority,
          targetLanguage: targetLanguage,
        ),
      );

      final jobId = response['job_id'] as String;
      final isCached = response['cached'] == true;
      debugPrint('[Jobs] $pageId → jobId=$jobId cached=$isCached');
      job.jobId = jobId;
      job.submittedAt = DateTime.now();
      job.sourceHash = (response['source_hash'] as String?) ?? hash;

      if (isCached) {
        // Server had it cached — download immediately
        final imageUrl = response['image_url'] as String?;
        var cacheDownloaded = false;
        if (imageUrl != null) {
          job.status = PageJobStatus.processing;
          job.stage = 'downloading';
          state = {...state};

          try {
            final img = await _withRetry(
              () => _api.getJobImage(settings: _settings, imageUrl: imageUrl),
            );
            job.translatedImage = img;
            job.status = PageJobStatus.completed;
            job.cached = true;
            job.imageUrl = imageUrl;
            state = {...state};
            _evictStaleJobs();
            cacheDownloaded = true;

            // Save to local cache
            await _cache.store(
              hash: hash,
              pipeline: cachePipeline,
              imageBytes: img,
              title: title,
              chapter: chapter,
              pageNumber: pageNumber,
            );
            _releaseSourceImage(job);
          } catch (e) {
            debugPrint(
              '[Jobs] Cache download failed for $pageId, resubmitting: $e',
            );
          }
        }
        if (!cacheDownloaded) {
          // Cache download failed or no URL — resubmit bypassing server cache
          final response2 = await _withRetry(
            () => _api.submitJob(
              settings: _settings,
              imageBytes: imageBytes,
              pipeline: effectivePipeline,
              title: title,
              chapter: chapter,
              pageNumber: pageNumber,
              sourceUrl: sourceUrl,
              sourceSite: sourceSite,
              latestGroup: latestGroup,
              latestToken: latestToken,
              latestSeq: latestSeq,
              priority: priority,
              targetLanguage: targetLanguage,
              force: true,
            ),
          );
          final jobId2 = response2['job_id'] as String;
          debugPrint('[Jobs] $pageId resubmitted → jobId=$jobId2');
          job.jobId = jobId2;
          job.submittedAt = DateTime.now();
          job.status = PageJobStatus.queued;
          job.stage = null;
          _ws.subscribeToJobs([jobId2]);
          _startPollingFallback();
        }
      } else {
        // Subscribe for updates
        debugPrint('[Jobs] $pageId subscribing WS=${_ws.isConnected} polling');
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
    if (type == 'job_complete') {
      debugPrint('[Jobs] WS complete jobId=$jobId status=${msg['status']}');
    }
    if (type == null || jobId == null) return;

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
        job.sourceHash = msg['source_hash'] as String? ?? job.sourceHash;
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

    // Deduplicate concurrent downloads for the same job
    if (job.jobId != null) {
      if (_downloadingJobIds.contains(job.jobId)) return;
      _downloadingJobIds.add(job.jobId!);
    }

    try {
      final img = await _withRetry(
        () => _api.getJobImage(settings: _settings, imageUrl: job.imageUrl!),
      );
      job.translatedImage = img;
      job.status = PageJobStatus.completed;
      state = {...state};
      _evictStaleJobs();

      // Save to local cache
      if (job.originalImage != null) {
        final hash = await _cache.hashImage(job.originalImage!);
        final effectivePipeline = job.pipeline ?? _settings.pipeline;
        final lang = _settings.targetLanguage;
        final cp = lang == 'en'
            ? effectivePipeline
            : '${effectivePipeline}_$lang';
        await _cache.store(
          hash: hash,
          pipeline: cp,
          imageBytes: img,
          title: job.title,
          chapter: job.chapter,
          pageNumber: job.pageNumber,
        );
        _releaseSourceImage(job);
      }
    } catch (e) {
      job.status = PageJobStatus.failed;
      job.error = 'Download failed: $e';
      state = {...state};
    } finally {
      if (job.jobId != null) {
        _downloadingJobIds.remove(job.jobId);
      }
    }
  }

  /// Fallback polling for active jobs when WebSocket is unavailable.
  /// Always restarts the timer so new batch submissions refresh the cycle.
  void _startPollingFallback() {
    _pollTimer?.cancel();
    _pollTimer = Timer.periodic(const Duration(seconds: 3), (_) async {
      final activeJobs = state.values
          .where((j) => j.isActive && j.jobId != null)
          .toList();
      debugPrint(
        '[Jobs] poll tick active=${activeJobs.length} WS=${_ws.isConnected}',
      );
      if (activeJobs.isEmpty) {
        _pollTimer?.cancel();
        _pollTimer = null;
        return;
      }

      // Re-subscribe on each tick to recover from dead-then-reconnected WS
      final activeJobIds = activeJobs.map((j) => j.jobId!).toList();
      _ws.subscribeToJobs(activeJobIds);

      // Expire stale jobs that have been active too long
      for (final job in activeJobs) {
        if (job.isStale) {
          debugPrint('[Jobs] ${job.pageId} timed out after 5 min');
          job.status = PageJobStatus.failed;
          job.error = 'Job timed out';
          state = {...state};
          continue;
        }
      }

      for (final job in activeJobs.where((j) => j.isActive)) {
        try {
          final status = await _api.getJobStatus(
            settings: _settings,
            jobId: job.jobId!,
          );
          final jobStatus = status['status'] as String?;
          debugPrint('[Jobs] poll ${job.jobId!.substring(4, 20)} → $jobStatus');
          if (jobStatus == 'completed') {
            job.imageUrl =
                status['image_url'] as String? ??
                '/api/v1/jobs/${job.jobId}/image';
            job.sourceHash = status['source_hash'] as String? ?? job.sourceHash;
            _downloadTranslatedImage(job);
          } else if (jobStatus == 'failed') {
            debugPrint('[Jobs] ${job.pageId} failed');
            job.status = PageJobStatus.failed;
            job.error = status['error'] as String? ?? 'Failed';
            state = {...state};
          }
        } catch (e) {
          debugPrint('[Jobs] Poll status failed for ${job.jobId}: $e');
        }
      }
    });
  }

  /// The captured source is only needed until its render reaches the cache.
  void _releaseSourceImage(PageJob job) {
    job.originalImage = null;
  }

  /// Release the memory held by pages the reader has moved well past.
  void _evictStaleJobs() {
    state = pruneJobs(
      state,
      onDropped: (job) {
        if (job.jobId != null) _ws.unsubscribeFromJobs([job.jobId!]);
      },
    );
  }

  /// Drop page bytes the reader is unlikely to need again.
  ///
  /// Insertion order is reading order, so the tail of the map is what is worth
  /// keeping: the newest renders keep their bytes until either bound is
  /// reached, older completed pages keep only their record, and the oldest
  /// records beyond [maxJobs] are dropped entirely. Jobs still in flight are
  /// never touched.
  @visibleForTesting
  static Map<String, PageJob> pruneJobs(
    Map<String, PageJob> jobs, {
    int maxImages = _maxRetainedImages,
    int maxBytes = _maxRetainedImageBytes,
    int maxJobs = _maxRetainedJobs,
    void Function(PageJob job)? onDropped,
  }) {
    final entries = jobs.entries.toList();
    final dropRecordsBefore = entries.length - maxJobs;

    var keptImages = 0;
    var keptBytes = 0;
    final release = <PageJob>[];
    for (final entry in entries.reversed) {
      final job = entry.value;
      final image = job.translatedImage;
      if (image == null) continue;
      final withinCount = keptImages < maxImages;
      final withinBudget = keptBytes + image.length <= maxBytes;
      // The page in front of the reader is kept whatever it costs; a single
      // oversized render must not evict itself and leave nothing to show.
      if (keptImages == 0 || (withinCount && withinBudget)) {
        keptImages++;
        keptBytes += image.length;
      } else {
        release.add(job);
      }
    }
    if (release.isEmpty && dropRecordsBefore <= 0) return jobs;

    for (final job in release) {
      job.translatedImage = null;
      job.originalImage = null;
    }

    if (dropRecordsBefore <= 0) return {...jobs};

    final next = <String, PageJob>{};
    for (var i = 0; i < entries.length; i++) {
      final job = entries[i].value;
      final settled = job.isComplete || job.isFailed;
      if (settled && i < dropRecordsBefore) {
        onDropped?.call(job);
        continue;
      }
      next[entries[i].key] = job;
    }
    return next;
  }

  /// Clear all tracked jobs (used when wiping the local cache).
  void clearAll() {
    for (final job in state.values) {
      if (job.jobId != null) {
        _ws.unsubscribeFromJobs([job.jobId!]);
      }
    }
    state = {};
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
