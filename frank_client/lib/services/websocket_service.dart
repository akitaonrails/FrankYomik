import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import '../models/server_settings.dart';

typedef ProgressCallback = void Function(Map<String, dynamic> message);

/// WebSocket client for real-time progress updates from the server.
class WebSocketService {
  WebSocketChannel? _channel;
  StreamSubscription? _subscription;
  Timer? _reconnectTimer;
  int _reconnectAttempts = 0;
  static const _maxReconnectDelay = 30;

  ServerSettings? _settings;
  final Set<String> _subscribedJobs = {};
  ProgressCallback? onMessage;
  VoidCallback? onConnected;
  VoidCallback? onDisconnected;

  bool get isConnected => _channel != null;

  void connect(ServerSettings settings) {
    _settings = settings;
    _reconnectAttempts = 0;
    _doConnect();
  }

  void _doConnect() {
    final settings = _settings;
    if (settings == null || !settings.isConfigured) return;

    final wsUrl = settings.serverUrl
        .replaceFirst('http://', 'ws://')
        .replaceFirst('https://', 'wss://');
    final uri =
        Uri.parse('$wsUrl/api/v1/ws?token=${settings.authToken}');

    try {
      _channel = WebSocketChannel.connect(uri);
      _subscription = _channel!.stream.listen(
        _onData,
        onError: _onError,
        onDone: _onDone,
      );
      _reconnectAttempts = 0;
      onConnected?.call();

      // Re-subscribe to any active jobs
      if (_subscribedJobs.isNotEmpty) {
        subscribeToJobs(_subscribedJobs.toList());
      }
    } catch (e) {
      _scheduleReconnect();
    }
  }

  void _onData(dynamic data) {
    try {
      final msg = jsonDecode(data as String) as Map<String, dynamic>;
      onMessage?.call(msg);
    } catch (_) {} // Malformed WS frame, ignore
  }

  void _onError(Object error) {
    _cleanup();
    onDisconnected?.call();
    _scheduleReconnect();
  }

  void _onDone() {
    _cleanup();
    onDisconnected?.call();
    _scheduleReconnect();
  }

  void _scheduleReconnect() {
    if (_settings == null) return;
    _reconnectTimer?.cancel();
    final delay = (_reconnectAttempts < 5)
        ? (1 << _reconnectAttempts)
        : _maxReconnectDelay;
    _reconnectAttempts++;
    _reconnectTimer = Timer(Duration(seconds: delay), _doConnect);
  }

  void subscribeToJobs(List<String> jobIds) {
    _subscribedJobs.addAll(jobIds);
    _send({'type': 'subscribe', 'job_ids': jobIds});
  }

  void unsubscribeFromJobs(List<String> jobIds) {
    _subscribedJobs.removeAll(jobIds);
    _send({'type': 'unsubscribe', 'job_ids': jobIds});
  }

  void _send(Map<String, dynamic> message) {
    if (_channel == null) return;
    try {
      _channel!.sink.add(jsonEncode(message));
    } catch (e) {
      debugPrint('[WS] Send failed: $e');
    }
  }

  void _cleanup() {
    _subscription?.cancel();
    _subscription = null;
    try {
      _channel?.sink.close();
    } catch (_) {} // Expected: socket may already be closed
    _channel = null;
  }

  void disconnect() {
    _settings = null;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _subscribedJobs.clear();
    _cleanup();
  }

  void dispose() {
    disconnect();
  }
}
