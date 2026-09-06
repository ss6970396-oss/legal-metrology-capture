import '../models/capture_record.dart';
import '../models/product_session.dart';

/// Builds the payloads defined by the Team 1 input contract.
///
/// This file is the boundary. Everything above it uses the app's own
/// vocabulary — inspections, product sessions, surface steps, attempts.
/// Everything the contract sees uses the contract's — capture sessions,
/// packages, artifacts, context. The translation happens here and nowhere
/// else, so a change on either side has exactly one place to be reconciled.
///
/// ## What crosses, and what does not
///
/// The contract is deliberately smaller than what the app knows. Per §3 of the
/// architecture: "Mobile may collect additional metadata; only contractually
/// required data crosses into the compliance boundary." So the inspection
/// identifier, the premises label, the inspector reference, the attempt
/// history, the skip reasons and the quality diagnostics do *not* appear in
/// these payloads. They travel in the richer per-artifact bundle
/// ([MetadataBundle]) which the extraction layer keeps and the compliance
/// engine never sees.
///
/// ## Identifier prefixes
///
/// The contract samples show `CS-`, `PKG-` and `IMG-` prefixes. The app mints
/// bare UUIDs. Rather than change identifier generation — those UUIDs are
/// already written into evidence files on disk and reissuing them would orphan
/// that evidence — the prefix is applied here, at the boundary, and the UUID
/// is preserved verbatim after it. A server that strips the prefix gets back
/// exactly the identifier the device recorded.
class ExtractionContract {
  const ExtractionContract._();

  /// Version of the payloads this file emits. Distinct from
  /// `package-facts/1.1`, which is what comes *back* from the extraction
  /// pipeline — this names the input side of the boundary.
  static const String schemaVersion = 'lm-capture-input/1.1';

  static String captureSessionId(String productSessionId) =>
      'CS-$productSessionId';

  static String packageId(String productSessionId) => 'PKG-$productSessionId';

  static String artifactId(String captureId) => 'IMG-$captureId';

  /// The session envelope, sent once per package before any artifact.
  ///
  /// Throws when the commercial context is incomplete. That is the whole point
  /// of creating the session up front: the applicability flags are settled and
  /// on the server before a single image is attributed to them, rather than
  /// being inferred later from whatever happened to be uploaded.
  static Map<String, dynamic> captureSession({
    required ProductSession product,
    required ClientDescriptor client,
  }) =>
      <String, dynamic>{
        'schema_version': schemaVersion,
        'capture_session_id': captureSessionId(product.productSessionId),
        'package_id': packageId(product.productSessionId),
        'client': client.toJson(),
        'context': product.context.toContractJson(),
        'started_at': product.startedAtUtc.toIso8601String(),
      };

  /// One entry of the contract's `images[]` array.
  ///
  /// `uri` is null on the way out and that is correct rather than missing: the
  /// object-store location is assigned by the server when it takes the bytes,
  /// so the device cannot know it. The server fills it in when it registers
  /// the artifact. What the device *can* assert — the identifier, the hash,
  /// the sequence, the capture time and the sensor geometry — is all here, and
  /// the hash is what lets the server prove the bytes it stored are the bytes
  /// the phone wrote.
  static Map<String, dynamic> imageEntry({
    required CaptureRecord capture,
    required int sequenceIndex,
  }) =>
      <String, dynamic>{
        'artifact_id': artifactId(capture.captureId),
        'sha256': capture.sha256,
        'uri': null,
        'sequence_index': sequenceIndex,
        'captured_at': capture.capturedAtUtc.toIso8601String(),
        'capture_metadata': <String, dynamic>{
          // Sensor dimensions as decoded, not as analysed. The quality
          // pipeline works on a downscaled buffer; reporting that size here
          // would understate the evidence by a factor of five and make any
          // downstream pixel reasoning wrong.
          'width_px': capture.quality.sourceWidth,
          'height_px': capture.quality.sourceHeight,
          'focal_length_mm': _focalLengthMm(capture),
          'orientation': _orientation(capture),
        },
      };

  /// The extraction job, submitted once a package's capture set is settled.
  ///
  /// Only accepted captures are listed. Retaken attempts stay on the device
  /// and in the per-artifact bundles — they are part of understanding how the
  /// evidence was produced — but they are not evidence *of* the package and
  /// feeding them to the resolver would manufacture conflicts between a blurred
  /// attempt and the frame that replaced it.
  static Map<String, dynamic> extractionJob({
    required ProductSession product,
    required ClientDescriptor client,
  }) {
    final accepted = acceptedCaptures(product);
    return <String, dynamic>{
      'schema_version': schemaVersion,
      'capture_session_id': captureSessionId(product.productSessionId),
      'package_id': packageId(product.productSessionId),
      'client': client.toJson(),
      'context': product.context.toContractJson(),
      'images': <Map<String, dynamic>>[
        for (var i = 0; i < accepted.length; i++)
          imageEntry(capture: accepted[i], sequenceIndex: i + 1),
      ],
      // Coverage is reported, not enforced. A package with a surface the
      // inspector could not reach is still worth extracting; the resolver
      // needs to know it is working from a partial set so that a field only
      // ever printed on the missing face resolves to UNKNOWN rather than
      // looking like an absence.
      'coverage': <String, dynamic>{
        'required_surfaces': product.requiredCount,
        'resolved_surfaces': product.resolvedRequiredCount,
        'captured_surfaces': product.capturedCount,
        'complete': product.isCoverageComplete,
        'unresolved_surfaces': <String>[
          for (final step in product.pendingSteps) step.surfaceId,
        ],
        'skipped_surfaces': <Map<String, dynamic>>[
          for (final step in product.steps)
            if (step.skip != null)
              <String, dynamic>{
                'surface_id': step.surfaceId,
                'reason': step.skip!.reason.name,
                'note': step.skip!.note,
              },
        ],
        'surfaces_needing_manual_review': <String>[
          for (final step in product.stepsNeedingManualReview) step.surfaceId,
        ],
      },
    };
  }

  /// The accepted capture for each surface, in the guided sequence's order.
  ///
  /// Sequence order is stable and meaningful — front, back, side, side — which
  /// gives the resolver a usable prior about which surface a declaration is
  /// likely to sit on. Capture time would not: an inspector who retakes the
  /// front panel last would otherwise present it as the final view.
  static List<CaptureRecord> acceptedCaptures(ProductSession product) =>
      <CaptureRecord>[
        for (final step in product.steps)
          if (step.acceptedCapture != null) step.acceptedCapture!,
      ];

  /// EXIF focal length, in millimetres, when the camera reported one.
  ///
  /// Null is the honest and common answer. The tag is a rational like `"4.25"`
  /// or `"17/4"`, many phone cameras omit it, and the contract types the field
  /// as nullable precisely because it cannot be relied on. Note that a focal
  /// length alone is not a scale: without subject distance it does not convert
  /// pixels to millimetres, and nothing downstream may treat its presence as
  /// making physical text measurement possible.
  static double? _focalLengthMm(CaptureRecord capture) {
    final raw = capture.exif['EXIF FocalLength'] ?? capture.exif['FocalLength'];
    if (raw == null) return null;
    final text = raw.trim();

    final slash = text.indexOf('/');
    if (slash > 0) {
      final numerator = double.tryParse(text.substring(0, slash).trim());
      final denominator = double.tryParse(text.substring(slash + 1).trim());
      if (numerator == null || denominator == null || denominator == 0) {
        return null;
      }
      return numerator / denominator;
    }
    return double.tryParse(text);
  }

  /// EXIF orientation as the TIFF integer code 1..8.
  ///
  /// Defaults to 1 (top-left, no rotation) when absent, which matches the
  /// EXIF specification's own default. The `exif` package renders this tag as
  /// prose — "Horizontal (normal)", "Rotated 90 CW" — so the words are mapped
  /// back to the codes the contract asks for.
  static int _orientation(CaptureRecord capture) {
    final raw = capture.exif['Image Orientation'] ??
        capture.exif['EXIF Orientation'] ??
        capture.exif['Orientation'];
    if (raw == null) return 1;

    final direct = int.tryParse(raw.trim());
    if (direct != null && direct >= 1 && direct <= 8) return direct;

    final text = raw.toLowerCase();
    if (text.contains('mirrored')) {
      if (text.contains('90 cw')) return 5;
      if (text.contains('270 cw') || text.contains('90 ccw')) return 7;
      if (text.contains('vertical')) return 4;
      return 2;
    }
    if (text.contains('180')) return 3;
    if (text.contains('90 cw')) return 6;
    if (text.contains('270 cw') || text.contains('90 ccw')) return 8;
    return 1;
  }
}

/// Identifies the app build that produced a capture session.
///
/// Kept as a value object rather than read from a global so tests can state a
/// client without a platform channel, and so the one place that knows the app
/// version is the composition root rather than this file.
class ClientDescriptor {
  const ClientDescriptor({
    required this.platform,
    required this.appVersion,
    required this.deviceModel,
  });

  /// `android` or `ios`, per the contract's enumeration.
  final String platform;
  final String appVersion;
  final String deviceModel;

  Map<String, dynamic> toJson() => <String, dynamic>{
        'platform': platform,
        'app_version': appVersion,
        'device_model': deviceModel,
      };
}
