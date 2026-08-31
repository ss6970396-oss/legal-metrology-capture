import 'dart:io';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:exif/exif.dart';

import '../core/app_paths.dart';
import '../core/device_identity.dart';
import '../core/ids.dart';
import '../models/capture_record.dart';
import '../models/product_session.dart';
import '../models/surface_step.dart';
import '../quality/quality_pipeline.dart';
import 'camera_service.dart';

/// Turns a shutter press into a durable, fully described piece of evidence.
///
/// Order matters here and is deliberate:
///   1. take the frame,
///   2. write the bytes into the evidence tree,
///   3. hash them,
///   4. read EXIF,
///   5. run quality checks.
///
/// The image is on disk and hashed before any analysis runs. If the app is
/// killed during the quality pass — plausible, since it is the most expensive
/// thing that happens — the photograph still exists and is still attributable.
/// Analysis can be redone from the file; a lost capture cannot be redone at
/// all once the inspector has left the premises.
class CaptureService {
  CaptureService({
    required this.cameraService,
    QualityPipeline? pipeline,
  }) : _pipeline = pipeline ?? QualityPipeline();

  final CameraService cameraService;
  final QualityPipeline _pipeline;

  Future<CaptureRecord> capture({
    required ProductSession product,
    required SurfaceStepState step,
  }) async {
    final shot = await cameraService.takePicture();
    final captureId = Ids.capture();
    final capturedAt = DateTime.now().toUtc();

    await AppPaths.instance.ensureProductDir(
      product.inspectionId,
      product.productSessionId,
    );
    final destination = AppPaths.instance.captureFile(
      product.inspectionId,
      product.productSessionId,
      captureId,
    );

    // Byte-for-byte copy. Nothing in this module ever re-encodes a capture,
    // which is what guarantees the EXIF block — orientation in particular —
    // reaches the reviewer exactly as the camera wrote it.
    final source = File(shot.path);
    final bytes = await source.readAsBytes();
    await destination.writeAsBytes(bytes, flush: true);
    try {
      await source.delete();
    } on Object {
      // The plugin's temp file will be cleaned up by the OS. Failing to
      // remove it is not worth interrupting a capture over.
    }

    final digest = sha256.convert(bytes).toString();
    final exif = await _readExif(bytes);
    final quality = await _pipeline.analyse(destination);
    final device = DeviceIdentity.current;

    return CaptureRecord(
      captureId: captureId,
      inspectionId: product.inspectionId,
      productSessionId: product.productSessionId,
      surfaceId: step.surfaceId,
      surfaceLabel: step.label,
      capturedAtUtc: capturedAt,
      localPath: destination.path,
      fileSizeBytes: bytes.length,
      sha256: digest,
      attemptNumber: step.attempts.length + 1,
      quality: quality,
      disposition: CaptureDisposition.pendingReview,
      deviceId: device.deviceId,
      deviceModel: device.model,
      osVersion: device.osVersion,
      exif: exif,
      cameraSettings:
          cameraService.capabilities?.toMetadata() ?? const <String, String>{},
    );
  }

  /// Reads the EXIF block for the metadata bundle.
  ///
  /// This is a copy for convenience of downstream consumers; the authoritative
  /// EXIF is the one still embedded in the untouched JPEG. Maker notes and
  /// embedded thumbnails are dropped — they are large, vendor-specific, and
  /// carry nothing a reviewer needs.
  Future<Map<String, String>> _readExif(Uint8List bytes) async {
    try {
      final tags = await readExifFromBytes(bytes);
      final result = <String, String>{};
      for (final entry in tags.entries) {
        final key = entry.key;
        if (key.contains('MakerNote') ||
            key.startsWith('Thumbnail') ||
            key.contains('JPEGThumbnail')) {
          continue;
        }
        final printable = entry.value.printable;
        if (printable.isEmpty) continue;
        result[key] =
            printable.length > 256 ? printable.substring(0, 256) : printable;
      }
      return result;
    } on Object {
      // A JPEG with no EXIF, or an EXIF block this parser chokes on, is not a
      // reason to lose the capture. The absence is itself recorded.
      return const <String, String>{};
    }
  }

  Future<void> dispose() => _pipeline.dispose();
}
