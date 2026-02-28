import 'dart:async';
import 'dart:io';
import 'dart:typed_data';
import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

/// Local cache for translated page images using SQLite metadata + filesystem storage.
class CacheService {
  Database? _db;
  String? _cacheDir;
  final Completer<void> _initCompleter = Completer<void>();

  /// Max age for cached entries (30 days).
  static const maxAgeDays = 30;

  /// Max total cache size in bytes (500 MB).
  static const maxCacheBytes = 500 * 1024 * 1024;

  /// In-memory hash → translated image cache for instant re-visit lookups.
  /// Avoids re-reading from SQLite + disk for pages seen this session.
  final Map<String, Uint8List> _memoryCache = {};

  /// Wait for init() to complete before accessing the database.
  Future<void> get ready => _initCompleter.future;

  Future<void> init() async {
    sqfliteFfiInit();
    databaseFactory = databaseFactoryFfi;

    // On Linux, getApplicationSupportDirectory() can point to a read-only
    // path. Use getApplicationDocumentsDirectory() as a reliable fallback.
    Directory appDir;
    if (Platform.isLinux) {
      appDir = await getApplicationDocumentsDirectory();
      appDir = Directory(p.join(appDir.path, '.frank_client'));
    } else {
      appDir = await getApplicationSupportDirectory();
    }
    _cacheDir = p.join(appDir.path, 'cache');
    await Directory(_cacheDir!).create(recursive: true);

    final dbPath = p.join(appDir.path, 'frank_cache.db');
    _db = await openDatabase(
      dbPath,
      version: 2,
      onCreate: (db, version) async {
        await db.execute('''
          CREATE TABLE pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_hash TEXT NOT NULL,
            pipeline TEXT NOT NULL,
            title TEXT,
            chapter TEXT,
            page_number TEXT,
            file_path TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            metadata_json TEXT,
            UNIQUE(image_hash, pipeline)
          )
        ''');
        await db.execute(
            'CREATE INDEX idx_pages_meta ON pages(pipeline, title, chapter, page_number)');
      },
      onUpgrade: (db, oldVersion, newVersion) async {
        if (oldVersion < 2) {
          await db.execute('ALTER TABLE pages ADD COLUMN metadata_json TEXT');
        }
      },
    );

    _initCompleter.complete();

    // Run eviction in background on startup
    evict();
  }

  /// Compute SHA256 hash of image bytes on a background isolate.
  Future<String> hashImage(Uint8List bytes) {
    return compute(_sha256Worker, bytes);
  }

  static String _sha256Worker(Uint8List bytes) {
    return sha256.convert(bytes).toString();
  }

  /// Look up a cached translation by image hash.
  /// Checks in-memory cache first, then SQLite + disk.
  Future<Uint8List?> lookupByHash(String hash, String pipeline) async {
    // Fast path: in-memory cache (no I/O)
    final memKey = '$hash:$pipeline';
    final memHit = _memoryCache[memKey];
    if (memHit != null) {
      debugPrint('[Cache] Memory hit for hash=${hash.substring(0, 12)}');
      return memHit;
    }

    await ready;
    final rows = await _db?.query(
      'pages',
      columns: ['file_path'],
      where: 'image_hash = ? AND pipeline = ?',
      whereArgs: [hash, pipeline],
      limit: 1,
    );
    if (rows == null || rows.isEmpty) {
      debugPrint('[Cache] Miss for hash=${hash.substring(0, 12)} pipeline=$pipeline');
      return null;
    }

    final filePath = rows.first['file_path'] as String;
    final file = File(filePath);
    if (!await file.exists()) {
      debugPrint('[Cache] DB hit but file missing: $filePath');
      return null;
    }
    final bytes = await file.readAsBytes();
    debugPrint('[Cache] Disk hit for hash=${hash.substring(0, 12)} (${bytes.length} bytes)');

    // Populate memory cache for future lookups
    _memoryCache[memKey] = bytes;

    return bytes;
  }

  /// Look up by metadata (title/chapter/page).
  Future<Uint8List?> lookupByMetadata(
      String pipeline, String title, String chapter, String pageNumber) async {
    await ready;
    final rows = await _db?.query(
      'pages',
      columns: ['file_path'],
      where:
          'pipeline = ? AND title = ? AND chapter = ? AND page_number = ?',
      whereArgs: [pipeline, title, chapter, pageNumber],
      limit: 1,
    );
    if (rows == null || rows.isEmpty) return null;

    final filePath = rows.first['file_path'] as String;
    final file = File(filePath);
    if (!await file.exists()) return null;
    return file.readAsBytes();
  }

  /// Look up cached metadata JSON by image hash and pipeline.
  Future<String?> lookupMetadataByHash(String hash, String pipeline) async {
    await ready;
    final rows = await _db?.query(
      'pages',
      columns: ['metadata_json'],
      where: 'image_hash = ? AND pipeline = ?',
      whereArgs: [hash, pipeline],
      limit: 1,
    );
    if (rows == null || rows.isEmpty) return null;
    return rows.first['metadata_json'] as String?;
  }

  /// Update metadata JSON for an existing cache entry.
  Future<void> updateMetadata(String hash, String pipeline, String metadataJson) async {
    await ready;
    await _db?.update(
      'pages',
      {'metadata_json': metadataJson},
      where: 'image_hash = ? AND pipeline = ?',
      whereArgs: [hash, pipeline],
    );
    debugPrint('[Cache] Updated metadata for ${hash.substring(0, 12)}');
  }

  /// Store a translated image in the local cache.
  Future<void> store({
    required String hash,
    required String pipeline,
    required Uint8List imageBytes,
    String? title,
    String? chapter,
    String? pageNumber,
    String? metadataJson,
  }) async {
    // Always populate memory cache immediately
    _memoryCache['$hash:$pipeline'] = imageBytes;

    await ready;
    final fileName = '$hash.png';
    final filePath = p.join(_cacheDir!, pipeline, fileName);
    await Directory(p.dirname(filePath)).create(recursive: true);
    await File(filePath).writeAsBytes(imageBytes);

    await _db?.insert(
      'pages',
      {
        'image_hash': hash,
        'pipeline': pipeline,
        'title': title,
        'chapter': chapter,
        'page_number': pageNumber,
        'file_path': filePath,
        'created_at': DateTime.now().millisecondsSinceEpoch,
        'metadata_json': metadataJson,
      },
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
    debugPrint('[Cache] Stored ${hash.substring(0, 12)}');
  }

  /// Evict expired entries (older than maxAgeDays) and enforce size limit.
  Future<void> evict() async {
    if (_db == null) return;

    // 1. Delete entries older than maxAgeDays
    final cutoff = DateTime.now()
        .subtract(const Duration(days: maxAgeDays))
        .millisecondsSinceEpoch;
    final expired = await _db!.query(
      'pages',
      columns: ['id', 'file_path'],
      where: 'created_at < ?',
      whereArgs: [cutoff],
    );
    for (final row in expired) {
      final filePath = row['file_path'] as String;
      try {
        await File(filePath).delete();
      } catch (_) {}
    }
    if (expired.isNotEmpty) {
      await _db!.delete('pages', where: 'created_at < ?', whereArgs: [cutoff]);
      debugPrint('[Cache] Evicted ${expired.length} expired entries');
    }

    // 2. Enforce size limit — delete oldest entries until under maxCacheBytes
    final allRows = await _db!.query(
      'pages',
      columns: ['id', 'file_path', 'created_at'],
      orderBy: 'created_at DESC',
    );
    var totalSize = 0;
    final toDelete = <int>[];
    final filesToDelete = <String>[];
    for (final row in allRows) {
      final filePath = row['file_path'] as String;
      final file = File(filePath);
      if (await file.exists()) {
        totalSize += await file.length();
      }
      if (totalSize > maxCacheBytes) {
        toDelete.add(row['id'] as int);
        filesToDelete.add(filePath);
      }
    }
    for (final path in filesToDelete) {
      try {
        await File(path).delete();
      } catch (_) {}
    }
    if (toDelete.isNotEmpty) {
      final ids = toDelete.join(',');
      await _db!.delete('pages', where: 'id IN ($ids)');
      debugPrint('[Cache] Evicted ${toDelete.length} entries over size limit');
    }
  }

  Future<void> dispose() async {
    await _db?.close();
  }
}
