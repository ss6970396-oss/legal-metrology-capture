import 'dart:io';

import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';

/// On-device storage layout for inspection evidence.
///
/// Everything is written under a single root inside the application documents
/// directory, so an evidence set can be located, backed up, or handed to a
/// reviewer as one directory tree:
///
/// ```
/// legal_metrology/
///   device.json                          installation identity
///   upload_queue/queue.json              durable upload queue
///   inspections/
///     <inspectionId>/
///       inspection.json                  visit manifest (all product sessions)
///       <productSessionId>/
///         <captureId>.jpg                original camera bytes, never rewritten
/// ```
///
/// Image files are written once and never modified. Quality analysis decodes
/// a copy in memory; it never writes back, which is what keeps the original
/// EXIF block (orientation especially) byte-identical to what the camera
/// produced.
class AppPaths {
  AppPaths._(this.root);

  final Directory root;

  static AppPaths? _instance;

  /// The initialised instance. Throws if [ensureInitialized] has not run.
  static AppPaths get instance {
    final value = _instance;
    if (value == null) {
      throw StateError(
        'AppPaths.ensureInitialized() must be awaited before use.',
      );
    }
    return value;
  }

  static bool get isInitialized => _instance != null;

  static Future<AppPaths> ensureInitialized() async {
    final existing = _instance;
    if (existing != null) return existing;

    final documents = await getApplicationDocumentsDirectory();
    final root = Directory(p.join(documents.path, 'legal_metrology'));
    final paths = AppPaths._(root);
    await paths.inspectionsRoot.create(recursive: true);
    await paths.queueRoot.create(recursive: true);
    _instance = paths;
    return paths;
  }

  Directory get inspectionsRoot =>
      Directory(p.join(root.path, 'inspections'));

  Directory get queueRoot => Directory(p.join(root.path, 'upload_queue'));

  File get deviceFile => File(p.join(root.path, 'device.json'));

  File get queueFile => File(p.join(queueRoot.path, 'queue.json'));

  Directory inspectionDir(String inspectionId) =>
      Directory(p.join(inspectionsRoot.path, inspectionId));

  File inspectionManifest(String inspectionId) =>
      File(p.join(inspectionDir(inspectionId).path, 'inspection.json'));

  Directory productDir(String inspectionId, String productSessionId) =>
      Directory(p.join(inspectionDir(inspectionId).path, productSessionId));

  File captureFile(
    String inspectionId,
    String productSessionId,
    String captureId,
  ) =>
      File(
        p.join(
          productDir(inspectionId, productSessionId).path,
          '$captureId.jpg',
        ),
      );

  Future<Directory> ensureProductDir(
    String inspectionId,
    String productSessionId,
  ) =>
      productDir(inspectionId, productSessionId).create(recursive: true);
}
