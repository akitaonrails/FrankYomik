import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'app.dart';
import 'services/ipv6_http_overrides.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  HttpOverrides.global = IPv6PreferringHttpOverrides();
  runApp(
    const ProviderScope(
      child: FrankApp(),
    ),
  );
}
