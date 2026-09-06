import '../models/barcode_scan.dart';
import '../models/capture_record.dart';
import '../models/inspection_session.dart';
import '../models/product_session.dart';
import '../models/surface_step.dart';
import 'extraction_contract.dart';

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

  static const String schemaVersion = 'lm-capture-metadata/1.1';

  static Map<String, dynamic> build({
    required InspectionSession inspection,
    required ProductSession product,
    required SurfaceStepState step,
    required CaptureRecord capture,
  }) {
    return <String, dynamic>{
      'schemaVersion': schemaVersion,

      // Nested session identity, stamped on every image.
      //
      // Both vocabularies appear here, deliberately. The bare UUIDs are what
      // the device wrote into its own evidence tree and what a reviewer will
      // find on disk; the prefixed forms are what the extraction contract and
      // the fact provenance refer to. Carrying both means neither side has to
      // reconstruct the other's identifier from a naming convention.
      'ids': <String, dynamic>{
        'inspectionId': capture.inspectionId,
        'productSessionId': capture.productSessionId,
        'captureId': capture.captureId,
        'captureSessionId':
            ExtractionContract.captureSessionId(capture.productSessionId),
        'packageId': ExtractionContract.packageId(capture.productSessionId),
        'artifactId': ExtractionContract.artifactId(capture.captureId),
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
          'complete': product.isCoverageComplete,
        },
      },

      // The declared commercial context, repeated on every artifact.
      //
      // It is already on the capture session, so this is redundant on the
      // wire — and that redundancy is the point. An artifact and its bundle
      // have to be sufficient on their own, and an image whose applicability
      // context can only be recovered by joining against another record is
      // not sufficient on its own. Stored in the local shape rather than the
      // contract shape because a bundle is written the moment a capture is
      // accepted, which can precede the inspector answering the last context
      // question — and a half-answered context must serialise as half
      // answered rather than refusing to serialise at all.
      'context': product.context.toJson(),

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
