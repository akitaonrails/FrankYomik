import 'dart:io';
import 'package:flutter/foundation.dart';

/// Global HttpOverrides that prefer IPv6 addresses when connecting.
///
/// Works around Cloudflare tunnel issues where IPv4 edge nodes
/// intermittently return 502 but IPv6 works reliably.
///
/// Call `HttpOverrides.global = IPv6PreferringHttpOverrides()` in main().
class IPv6PreferringHttpOverrides extends HttpOverrides {
  @override
  HttpClient createHttpClient(SecurityContext? context) {
    final client = super.createHttpClient(context);
    client.connectionFactory =
        (Uri uri, String? proxyHost, int? proxyPort) async {
      final host = proxyHost ?? uri.host;
      final scheme = uri.scheme;
      final isSecure = scheme == 'https' || scheme == 'wss';
      final defaultPort = isSecure ? 443 : 80;
      final port = (uri.hasPort && uri.port != 0) ? uri.port : defaultPort;

      // Skip for local addresses
      if (host == 'localhost' ||
          host == '127.0.0.1' ||
          host == '::1' ||
          host.startsWith('192.168.') ||
          host.startsWith('10.')) {
        if (isSecure) {
          final sock = await Socket.connect(host, port);
          final secure = await SecureSocket.secure(sock, host: host);
          return ConnectionTask.fromSocket(
              Future.value(secure), () => secure.destroy());
        }
        return Socket.startConnect(host, port);
      }

      // Resolve DNS, preferring IPv6
      final addresses = await InternetAddress.lookup(host);
      addresses.sort((a, b) {
        final aV6 = a.type == InternetAddressType.IPv6 ? 0 : 1;
        final bV6 = b.type == InternetAddressType.IPv6 ? 0 : 1;
        return aV6.compareTo(bV6);
      });

      // Try each address in order
      for (var i = 0; i < addresses.length; i++) {
        final addr = addresses[i];
        try {
          if (isSecure) {
            final sock = await Socket.connect(addr, port,
                timeout: const Duration(seconds: 5));
            final secure =
                await SecureSocket.secure(sock, host: uri.host);
            debugPrint(
                '[HTTP] Connected to ${addr.address} (${addr.type})');
            return ConnectionTask.fromSocket(
                Future.value(secure), () => secure.destroy());
          } else {
            final sock = await Socket.connect(addr, port,
                timeout: const Duration(seconds: 5));
            debugPrint(
                '[HTTP] Connected to ${addr.address} (${addr.type})');
            return ConnectionTask.fromSocket(
                Future.value(sock), () => sock.destroy());
          }
        } catch (e) {
          if (i == addresses.length - 1) rethrow;
          debugPrint(
              '[HTTP] ${addr.address} failed, trying next: $e');
        }
      }

      // Unreachable, but satisfy the compiler
      return Socket.startConnect(host, port);
    };
    return client;
  }
}
