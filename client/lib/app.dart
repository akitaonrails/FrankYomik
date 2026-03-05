import 'package:flutter/material.dart';
import 'screens/home_screen.dart';

class FrankApp extends StatelessWidget {
  const FrankApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Frank Yomik',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(
          seedColor: Colors.indigo,
          brightness: Brightness.dark,
        ),
        useMaterial3: true,
      ),
      home: const HomeScreen(),
    );
  }
}
