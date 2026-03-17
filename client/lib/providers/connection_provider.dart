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
    debugPrint('[Connection] connect: serverUrl=${settings.serverUrl} isLoaded=${settings.isLoaded} isConfigured=${settings.isConfigured}');
    if (!settings.isConfigured) {
      state = ConnectionStatus.error;
      return;
    }

    state = ConnectionStatus.connecting;

    // Test REST connectivity with retry for transient failures
    final api = _ref.read(apiServiceProvider);
    var connected = false;
    for (var attempt = 1; attempt <= 3; attempt++) {
      try {
        await api.getHealth(settings);
        connected = true;
        break;
      } catch (e) {
        debugPrint('[Connection] Health check attempt $attempt failed: $e');
        if (attempt < 3) {
          await Future.delayed(Duration(seconds: attempt));
        }
      }
    }
    if (!connected) {
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
