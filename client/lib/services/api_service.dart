import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:http/io_client.dart';
import '../models/server_settings.dart';

/// REST client for the Frank API server.
///
/// Uses the global HttpOverrides for IPv6-preferring connections.
/// See [IPv6PreferringHttpOverrides] in main.dart.
class ApiService {
  final http.Client _client = IOClient();

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
    String? targetLanguage,
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

    request.fields['target_lang'] = targetLanguage ?? settings.targetLanguage;
    if (title != null) request.fields['title'] = title;
    if (chapter != null) request.fields['chapter'] = chapter;
    if (pageNumber != null) request.fields['page_number'] = pageNumber;
    if (sourceUrl != null) request.fields['source_url'] = sourceUrl;
    if (force) request.fields['force'] = 'true';

    final result = await (() async {
      final response = await _client.send(request);
      final body = await response.stream.bytesToString();
      return (response.statusCode, body);
    })()
        .timeout(const Duration(seconds: 30));

    if (result.$1 != 201) {
      throw ApiException(
        'Submit failed (${result.$1}): ${result.$2}',
        statusCode: result.$1,
        retryable: result.$1 >= 500,
      );
    }
    return jsonDecode(result.$2) as Map<String, dynamic>;
  }

  /// Poll job status.
  Future<Map<String, dynamic>> getJobStatus({
    required ServerSettings settings,
    required String jobId,
  }) async {
    final uri = Uri.parse('${settings.serverUrl}/api/v1/jobs/$jobId');
    final response = await _client.get(uri, headers: _headers(settings)).timeout(
      const Duration(seconds: 10),
    );

    if (response.statusCode != 200) {
      throw ApiException(
        'Status failed (${response.statusCode})',
        statusCode: response.statusCode,
        retryable: response.statusCode >= 500,
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
    final response = await _client.get(uri, headers: _headers(settings)).timeout(
      const Duration(seconds: 45),
    );

    if (response.statusCode != 200) {
      throw ApiException(
        'Image download failed (${response.statusCode})',
        statusCode: response.statusCode,
        retryable: response.statusCode >= 500,
      );
    }
    return response.bodyBytes;
  }

  /// Check server health (no auth required).
  Future<Map<String, dynamic>> getHealth(ServerSettings settings) async {
    final uri = Uri.parse('${settings.serverUrl}/api/v1/health');
    debugPrint('[API] getHealth: $uri');
    final response = await _client.get(uri).timeout(
      const Duration(seconds: 10),
    );

    debugPrint('[API] getHealth response: ${response.statusCode}');
    if (response.statusCode != 200) {
      throw ApiException(
        'Health check failed (${response.statusCode})',
        statusCode: response.statusCode,
        retryable: response.statusCode >= 500,
      );
    }
    return jsonDecode(response.body) as Map<String, dynamic>;
  }

  void dispose() => _client.close();
}

class ApiException implements Exception {
  final String message;
  final int? statusCode;
  final bool retryable;
  ApiException(this.message, {this.statusCode, this.retryable = false});
  @override
  String toString() => 'ApiException: $message';
}
