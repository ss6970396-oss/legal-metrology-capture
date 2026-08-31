import 'dart:convert';
import 'dart:io';

/// Atomic JSON persistence.
///
/// Manifests are written to a sibling `.tmp` file and then renamed over the
/// target. A rename within a directory is atomic on both Android and iOS, so
/// a process death mid-write leaves the previous manifest intact rather than
/// a truncated one. Evidence records are not worth losing to a half-written
/// file.
class JsonStore {
  const JsonStore();

  static const JsonEncoder _encoder = JsonEncoder.withIndent('  ');

  /// Distinguishes the scratch files of overlapping writes.
  ///
  /// A single shared `.tmp` name is not safe here: two writes to the same
  /// manifest that overlap would both write the one scratch file, and the
  /// first rename would pull it out from under the second, which then fails
  /// with "cannot find the file". Session state is persisted from several
  /// places — starting a package, accepting a capture, recording a skip — so
  /// overlapping writes are ordinary, not exotic.
  static int _scratchCounter = 0;

  Future<void> write(File file, Map<String, dynamic> data) async {
    await file.parent.create(recursive: true);
    final temp = File('${file.path}.${_scratchCounter++}.tmp');
    try {
      await temp.writeAsString(_encoder.convert(data), flush: true);
      // Rename within a directory is atomic on Android and iOS, so a crash
      // mid-write leaves the previous manifest intact rather than a truncated
      // one.
      await temp.rename(file.path);
    } on Object {
      if (await temp.exists()) {
        try {
          await temp.delete();
        } on Object {
          // Best effort; a stray scratch file is harmless.
        }
      }
      rethrow;
    }
  }

  Future<Map<String, dynamic>?> read(File file) async {
    if (!await file.exists()) return null;
    try {
      final raw = await file.readAsString();
      if (raw.trim().isEmpty) return null;
      final decoded = jsonDecode(raw);
      return decoded is Map<String, dynamic> ? decoded : null;
    } on FormatException {
      // A corrupt manifest must not take the app down mid-inspection. The
      // caller falls back to an empty state; the image files on disk are the
      // durable record and can be re-indexed.
      return null;
    }
  }
}

/// ISO-8601 UTC helpers. Every timestamp in this module is stored in UTC so
/// that evidence captured across a device timezone change still sorts
/// correctly.
String encodeTime(DateTime value) => value.toUtc().toIso8601String();

DateTime decodeTime(Object? value) {
  if (value is String) {
    final parsed = DateTime.tryParse(value);
    if (parsed != null) return parsed.toUtc();
  }
  return DateTime.now().toUtc();
}

DateTime? decodeTimeOrNull(Object? value) {
  if (value is String) return DateTime.tryParse(value)?.toUtc();
  return null;
}
