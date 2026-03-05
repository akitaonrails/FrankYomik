import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../services/api_service.dart';
import '../services/websocket_service.dart';
import 'settings_provider.dart';

enum ConnectionStatus { disconnected, connecting, connected, error }

final connectionProvider =
    StateNotifierProvider<ConnectionNotifier, ConnectionStatus>((ref) {
  return ConnectionNotifier(ref);
});

final apiServiceProvider = Provider<ApiService>((ref) => ApiService());

final wsServiceProvider = Provider<WebSocketService>((ref) {
  final ws = WebSocketService();
  ref.onDispose(() => ws.dispose());
  return ws;
});

class ConnectionNotifier extends StateNotifier<ConnectionStatus> {
  final Ref _ref;

  ConnectionNotifier(this._ref) : super(ConnectionStatus.disconnected);

  Future<void> connect() async {
    final settings = _ref.read(settingsProvider);
    if (!settings.isConfigured) {
      state = ConnectionStatus.error;
      return;
    }

    state = ConnectionStatus.connecting;

    // Test REST connectivity
    try {
      final api = _ref.read(apiServiceProvider);
      await api.getHealth(settings);
    } catch (e) {
      debugPrint('[Connection] Health check failed: $e');
      state = ConnectionStatus.error;
      return;
    }

    // Connect WebSocket
    final ws = _ref.read(wsServiceProvider);
    ws.onConnected = () {
      if (mounted) state = ConnectionStatus.connected;
    };
    ws.onDisconnected = () {
      if (mounted) state = ConnectionStatus.disconnected;
    };
    ws.connect(settings);

    state = ConnectionStatus.connected;
  }

  void disconnect() {
    final ws = _ref.read(wsServiceProvider);
    ws.disconnect();
    state = ConnectionStatus.disconnected;
  }
}
