import 'dart:convert';
import 'dart:typed_data';
import 'package:http/http.dart' as http;
import '../models/server_settings.dart';

/// REST client for the Frank API server.
class ApiService {
  final http.Client _client = http.Client();

  Map<String, String> _headers(ServerSettings settings) => {
    'Authorization': 'Bearer ${settings.authToken}',
  };

  /// Submit an image for translation. Returns job response map.
  Future<Map<String, dynamic>> submitJob({
    required ServerSettings settings,
    required Uint8List imageBytes,
    String? pipeline,
    String? title,
    String? chapter,
    String? pageNumber,
    String? sourceUrl,
    String priority = 'high',
    bool force = false,
  }) async {
    final uri = Uri.parse('${settings.serverUrl}/api/v1/jobs');
    final request = http.MultipartRequest('POST', uri)
      ..headers.addAll(_headers(settings))
      ..fields['pipeline'] = pipeline ?? settings.pipeline
      ..fields['priority'] = priority
      ..files.add(
        http.MultipartFile.fromBytes('image', imageBytes, filename: 'page.png'),
      );

    if (title != null) request.fields['title'] = title;
    if (chapter != null) request.fields['chapter'] = chapter;
    if (pageNumber != null) request.fields['page_number'] = pageNumber;
    if (sourceUrl != null) request.fields['source_url'] = sourceUrl;
    if (force) request.fields['force'] = 'true';

    final response = await _client.send(request);
    final body = await response.stream.bytesToString();

    if (response.statusCode != 201) {
      throw ApiException('Submit failed (${response.statusCode}): $body');
    }
    return jsonDecode(body) as Map<String, dynamic>;
  }

  /// Poll job status.
  Future<Map<String, dynamic>> getJobStatus({
    required ServerSettings settings,
    required String jobId,
  }) async {
    final uri = Uri.parse('${settings.serverUrl}/api/v1/jobs/$jobId');
    final response = await _client.get(uri, headers: _headers(settings));

    if (response.statusCode != 200) {
      throw ApiException('Status failed (${response.statusCode})');
    }
    return jsonDecode(response.body) as Map<String, dynamic>;
  }

  /// Fetch cached metadata payload by source hash.
  Future<Map<String, dynamic>> getCacheMetadataByHash({
    required ServerSettings settings,
    required String pipeline,
    required String sourceHash,
  }) async {
    final uri = Uri.parse(
      '${settings.serverUrl}/api/v1/cache/by-hash/$pipeline/$sourceHash/meta',
    );
    final response = await _client.get(uri, headers: _headers(settings));
    if (response.statusCode != 200) {
      throw ApiException(
        'Meta fetch failed (${response.statusCode})',
        statusCode: response.statusCode,
      );
    }
    return jsonDecode(response.body) as Map<String, dynamic>;
  }

  /// Update cached metadata and enqueue rerender.
  Future<Map<String, dynamic>> patchCacheMetadataByHash({
    required ServerSettings settings,
    required String pipeline,
    required String sourceHash,
    required Map<String, dynamic> metadata,
    String? baseContentHash,
    String priority = 'high',
  }) async {
    final uri = Uri.parse(
      '${settings.serverUrl}/api/v1/cache/by-hash/$pipeline/$sourceHash/meta',
    );
    final payload = <String, dynamic>{
      'metadata': metadata,
      'priority': priority,
      if (baseContentHash != null && baseContentHash.isNotEmpty)
        'base_content_hash': baseContentHash,
    };
    final response = await _client.patch(
      uri,
      headers: {..._headers(settings), 'Content-Type': 'application/json'},
      body: jsonEncode(payload),
    );
    if (response.statusCode == 409) {
      throw ApiConflictException(
        'Content hash mismatch — metadata was modified concurrently',
      );
    }
    if (response.statusCode != 202) {
      throw ApiException(
        'Meta patch failed (${response.statusCode}): ${response.body}',
        statusCode: response.statusCode,
      );
    }
    return jsonDecode(response.body) as Map<String, dynamic>;
  }

  /// Download the translated image bytes.
  Future<Uint8List> getJobImage({
    required ServerSettings settings,
    required String imageUrl,
  }) async {
    // imageUrl can be relative (/api/v1/...) or absolute
    final uri = imageUrl.startsWith('http')
        ? Uri.parse(imageUrl)
        : Uri.parse('${settings.serverUrl}$imageUrl');
    final response = await _client.get(uri, headers: _headers(settings));

    if (response.statusCode != 200) {
      throw ApiException('Image download failed (${response.statusCode})');
    }
    return response.bodyBytes;
  }

  /// Download cached image bytes by source hash.
  Future<Uint8List> getCacheImageByHash({
    required ServerSettings settings,
    required String pipeline,
    required String sourceHash,
  }) async {
    final uri = Uri.parse(
      '${settings.serverUrl}/api/v1/cache/by-hash/$pipeline/$sourceHash/image',
    );
    final response = await _client.get(uri, headers: _headers(settings));
    if (response.statusCode != 200) {
      throw ApiException(
        'Cache image download failed (${response.statusCode})',
      );
    }
    return response.bodyBytes;
  }

  /// Check server health (no auth required).
  Future<Map<String, dynamic>> getHealth(ServerSettings settings) async {
    final uri = Uri.parse('${settings.serverUrl}/api/v1/health');
    final response = await _client.get(uri);

    if (response.statusCode != 200) {
      throw ApiException('Health check failed (${response.statusCode})');
    }
    return jsonDecode(response.body) as Map<String, dynamic>;
  }

  void dispose() => _client.close();
}

class ApiException implements Exception {
  final String message;
  final int? statusCode;
  ApiException(this.message, {this.statusCode});
  @override
  String toString() => 'ApiException: $message';
}

/// Thrown when a PATCH request hits a 409 Conflict (stale content hash).
class ApiConflictException extends ApiException {
  ApiConflictException(super.message) : super(statusCode: 409);
}
