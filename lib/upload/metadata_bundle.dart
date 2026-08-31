import '../models/barcode_scan.dart';
import '../models/capture_record.dart';
import '../models/inspection_session.dart';
import '../models/product_session.dart';
import '../models/surface_step.dart';

/// Builds the metadata that travels with every uploaded image.
///
/// One rule shapes this whole file: the bundle has to be sufficient on its
/// own. Someone holding one image and its bundle, with no access to the app or
/// any other file, must be able to say which visit and which package it
/// belongs to, which face of the package it shows, when it was taken, on what
/// device, and what the quality checks concluded. Nothing here is a pointer
/// into state that lives somewhere else.
class MetadataBundle {
  const MetadataBundle._();

  static const String schemaVersion = 'lm-capture-metadata/1.0';

  static Map<String, dynamic> build({
    required InspectionSession inspection,
    required ProductSession product,
    required SurfaceStepState step,
    required CaptureRecord capture,
  }) {
    return <String, dynamic>{
      'schemaVersion': schemaVersion,

      // Nested session identity, stamped on every image.
      'ids': <String, dynamic>{
        'inspectionId': capture.inspectionId,
        'productSessionId': capture.productSessionId,
        'captureId': capture.captureId,
        'scheme': 'uuid-v4-client-generated',
        // The device is the origin of these identifiers. Evidence already
        // written to local storage references them, so a server-side reissue
        // would orphan it.
        'serverMustPreserveClientIds': true,
      },

      'inspection': <String, dynamic>{
        'inspectionId': inspection.inspectionId,
        'premisesLabel': inspection.premisesLabel,
        'inspectorReference': inspection.inspectorReference,
        'startedAtUtc': inspection.startedAtUtc.toIso8601String(),
      },

      'productSession': <String, dynamic>{
        'productSessionId': product.productSessionId,
        'productLabel': product.productLabel,
        'startedAtUtc': product.startedAtUtc.toIso8601String(),
        'coverage': <String, dynamic>{
          'requiredSteps': product.requiredCount,
          'resolvedSteps': product.resolvedRequiredCount,
          'label': product.coverageLabel,
          'complete': product.isComplete,
        },
      },

      'surface': <String, dynamic>{
        'surfaceId': capture.surfaceId,
        'surfaceLabel': capture.surfaceLabel,
        'attemptNumber': capture.attemptNumber,
        'totalAttempts': step.attempts.length,
        'disposition': capture.disposition.name,
        'manualReviewRequired': capture.needsManualReview,
        'manualReviewReason': step.manualReviewReason,
      },

      'capturedAtUtc': capture.capturedAtUtc.toIso8601String(),

      'device': <String, dynamic>{
        'deviceId': capture.deviceId,
        'model': capture.deviceModel,
        'osVersion': capture.osVersion,
      },

      'file': <String, dynamic>{
        'fileName': '${capture.captureId}.jpg',
        'mimeType': 'image/jpeg',
        'sizeBytes': capture.fileSizeBytes,
        'sha256': capture.sha256,
        're_encoded': false,
        'exifPreserved': true,
      },

      'cameraSettings': capture.cameraSettings,
      'exif': capture.exif,

      // Every check that ran, whatever it concluded — passes included.
      'quality': capture.quality.toJson(),

      // Product identity, kept in its own section and explicitly labelled so
      // it can never be mistaken for a compliance finding. A scanned barcode
      // says which product this is; it says nothing about whether the package
      // carries a lawful declaration.
      'identification': product.identification == null
          ? null
          : <String, dynamic>{
              ...product.identification!.toJson(),
              'note': BarcodeScan.disclaimer,
            },
    };
  }
}
