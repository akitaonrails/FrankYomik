import 'dart:typed_data';

/// Tracks a page through its translation lifecycle.
class PageJob {
  final String pageId;
  final String? title;
  final String? chapter;
  final String? pageNumber;
  final String? sourceUrl;
  final String? pipeline;
  final Uint8List? originalImage;

  String? jobId;
  PageJobStatus status;
  String? stage;
  String? detail;
  int percent;
  Uint8List? translatedImage;
  String? imageUrl;
  String? metaUrl;
  String? sourceHash;
  String? contentHash;
  String? renderHash;
  Map<String, dynamic>? metadata;
  String? error;
  bool cached;

  PageJob({
    required this.pageId,
    this.title,
    this.chapter,
    this.pageNumber,
    this.sourceUrl,
    this.pipeline,
    this.originalImage,
    this.jobId,
    this.status = PageJobStatus.pending,
    this.stage,
    this.detail,
    this.percent = 0,
    this.translatedImage,
    this.imageUrl,
    this.metaUrl,
    this.sourceHash,
    this.contentHash,
    this.renderHash,
    this.metadata,
    this.error,
    this.cached = false,
  });

  bool get isComplete => status == PageJobStatus.completed;
  bool get isFailed => status == PageJobStatus.failed;
  bool get isActive =>
      status == PageJobStatus.queued || status == PageJobStatus.processing;
}

enum PageJobStatus { pending, queued, processing, completed, failed }
