import 'dart:io';
import 'dart:typed_data';
import 'package:crypto/crypto.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

/// Local cache for translated page images using SQLite metadata + filesystem storage.
class CacheService {
  Database? _db;
  String? _cacheDir;

  Future<void> init() async {
    sqfliteFfiInit();
    databaseFactory = databaseFactoryFfi;

    final appDir = await getApplicationSupportDirectory();
    _cacheDir = p.join(appDir.path, 'cache');
    await Directory(_cacheDir!).create(recursive: true);

    final dbPath = p.join(appDir.path, 'frank_cache.db');
    _db = await openDatabase(
      dbPath,
      version: 1,
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
            UNIQUE(image_hash, pipeline)
          )
        ''');
        await db.execute(
            'CREATE INDEX idx_pages_meta ON pages(pipeline, title, chapter, page_number)');
      },
    );
  }

  /// Compute SHA256 hash of image bytes.
  String hashImage(Uint8List bytes) => sha256.convert(bytes).toString();

  /// Look up a cached translation by image hash.
  Future<Uint8List?> lookupByHash(String hash, String pipeline) async {
    final rows = await _db?.query(
      'pages',
      columns: ['file_path'],
      where: 'image_hash = ? AND pipeline = ?',
      whereArgs: [hash, pipeline],
      limit: 1,
    );
    if (rows == null || rows.isEmpty) return null;

    final filePath = rows.first['file_path'] as String;
    final file = File(filePath);
    if (!await file.exists()) return null;
    return file.readAsBytes();
  }

  /// Look up by metadata (title/chapter/page).
  Future<Uint8List?> lookupByMetadata(
      String pipeline, String title, String chapter, String pageNumber) async {
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

  /// Store a translated image in the local cache.
  Future<void> store({
    required String hash,
    required String pipeline,
    required Uint8List imageBytes,
    String? title,
    String? chapter,
    String? pageNumber,
  }) async {
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
      },
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
  }

  Future<void> dispose() async {
    await _db?.close();
  }
}
