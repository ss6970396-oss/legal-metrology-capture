import '../core/json_store.dart';
import 'quality_report.dart';

/// What happened to a captured frame after the inspector looked at it.
///
/// Rejected attempts are kept, not deleted. If a surface took four tries, the
/// record shows four tries; that history is part of understanding how the
/// evidence was produced, and quietly dropping the failures would make a
/// difficult capture look like an easy one.
enum CaptureDisposition {
  /// Analysed, shown to the inspector, awaiting their decision.
  pendingReview,

  /// Inspector accepted it as the evidence for this surface.
  accepted,

  /// Accepted after the retry limit was reached with quality still failing.
  /// The surface carries a manual-review flag.
  acceptedUnderManualReview,

  /// Inspector chose to retake. Superseded by a later attempt.
  retaken,
}

extension CaptureDispositionX on CaptureDisposition {
  bool get isAccepted =>
      this == CaptureDisposition.accepted ||
      this == CaptureDisposition.acceptedUnderManualReview;

  String get label => switch (this) {
        CaptureDisposition.pendingReview => 'Awaiting review',
        CaptureDisposition.accepted => 'Accepted',
        CaptureDisposition.acceptedUnderManualReview =>
          'Accepted — manual review required',
        CaptureDisposition.retaken => 'Retaken',
      };

  static CaptureDisposition parse(Object? value) =>
      CaptureDisposition.values.firstWhere(
        (v) => v.name == value,
        orElse: () => CaptureDisposition.pendingReview,
      );
}

/// One photograph, with everything needed to establish what it is and where it
/// came from.
///
/// The image file itself is written once and never rewritten, so the EXIF
/// block — orientation above all, since a sideways declaration panel is a
/// review problem — stays exactly as the camera produced it. [exif] here is a
/// readable copy of that block for the metadata bundle, not a replacement for
/// it.
class CaptureRecord {
  const CaptureRecord({
    required this.captureId,
    required this.inspectionId,
    required this.productSessionId,
    required this.surfaceId,
    required this.surfaceLabel,
    required this.capturedAtUtc,
    required this.localPath,
    required this.fileSizeBytes,
    required this.sha256,
    required this.attemptNumber,
    required this.quality,
    required this.disposition,
    required this.deviceId,
    required this.deviceModel,
    required this.osVersion,
    this.exif = const <String, String>{},
    this.cameraSettings = const <String, String>{},
  });

  final String captureId;

  /// Parent visit. Attached to every image so an evidence file can be traced
  /// back without consulting an index.
  final String inspectionId;

  /// Parent package within the visit.
  final String productSessionId;

  final String surfaceId;
  final String surfaceLabel;
  final DateTime capturedAtUtc;

  final String localPath;
  final int fileSizeBytes;

  /// SHA-256 of the stored bytes. Lets the server confirm that what it
  /// received is what the phone wrote, and lets anyone later confirm the file
  /// has not been altered since.
  final String sha256;

  /// 1-based attempt counter within this surface.
  final int attemptNumber;

  final QualityReport quality;
  final CaptureDisposition disposition;

  final String deviceId;
  final String deviceModel;
  final String osVersion;

  final Map<String, String> exif;
  final Map<String, String> cameraSettings;

  bool get needsManualReview =>
      disposition == CaptureDisposition.acceptedUnderManualReview;

  CaptureRecord copyWith({CaptureDisposition? disposition}) => CaptureRecord(
        captureId: captureId,
        inspectionId: inspectionId,
        productSessionId: productSessionId,
        surfaceId: surfaceId,
        surfaceLabel: surfaceLabel,
        capturedAtUtc: capturedAtUtc,
        localPath: localPath,
        fileSizeBytes: fileSizeBytes,
        sha256: sha256,
        attemptNumber: attemptNumber,
        quality: quality,
        disposition: disposition ?? this.disposition,
        deviceId: deviceId,
        deviceModel: deviceModel,
        osVersion: osVersion,
        exif: exif,
        cameraSettings: cameraSettings,
      );

  Map<String, dynamic> toJson() => <String, dynamic>{
        'captureId': captureId,
        'inspectionId': inspectionId,
        'productSessionId': productSessionId,
        'surfaceId': surfaceId,
        'surfaceLabel': surfaceLabel,
        'capturedAtUtc': encodeTime(capturedAtUtc),
        'localPath': localPath,
        'fileSizeBytes': fileSizeBytes,
        'sha256': sha256,
        'attemptNumber': attemptNumber,
        'disposition': disposition.name,
        'device': <String, dynamic>{
          'deviceId': deviceId,
          'model': deviceModel,
          'osVersion': osVersion,
        },
        'cameraSettings': cameraSettings,
        'exif': exif,
        'quality': quality.toJson(),
      };

  factory CaptureRecord.fromJson(Map<String, dynamic> json) {
    final device = (json['device'] as Map?)?.cast<String, dynamic>() ??
        const <String, dynamic>{};
    return CaptureRecord(
      captureId: json['captureId'] as String? ?? '',
      inspectionId: json['inspectionId'] as String? ?? '',
      productSessionId: json['productSessionId'] as String? ?? '',
      surfaceId: json['surfaceId'] as String? ?? '',
      surfaceLabel: json['surfaceLabel'] as String? ?? '',
      capturedAtUtc: decodeTime(json['capturedAtUtc']),
      localPath: json['localPath'] as String? ?? '',
      fileSizeBytes: (json['fileSizeBytes'] as num?)?.toInt() ?? 0,
      sha256: json['sha256'] as String? ?? '',
      attemptNumber: (json['attemptNumber'] as num?)?.toInt() ?? 1,
      quality: QualityReport.fromJson(
        (json['quality'] as Map?)?.cast<String, dynamic>() ??
            const <String, dynamic>{},
      ),
      disposition: CaptureDispositionX.parse(json['disposition']),
      deviceId: device['deviceId'] as String? ?? 'unknown',
      deviceModel: device['model'] as String? ?? 'unknown',
      osVersion: device['osVersion'] as String? ?? 'unknown',
      exif: (json['exif'] as Map?)?.map((k, v) => MapEntry('$k', '$v')) ??
          const <String, String>{},
      cameraSettings:
          (json['cameraSettings'] as Map?)?.map((k, v) => MapEntry('$k', '$v')) ??
              const <String, String>{},
    );
  }
}
