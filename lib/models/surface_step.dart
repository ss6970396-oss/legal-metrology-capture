import '../core/json_store.dart';
import '../quality/thresholds.dart';
import 'capture_record.dart';
import 'quality_report.dart';

/// Shape of the on-screen alignment guide for a step.
enum OverlayShape { principalPanel, rearPanel, narrowSide, freeform }

/// One face of the package the inspector is asked to photograph.
class SurfaceStepDefinition {
  const SurfaceStepDefinition({
    required this.surfaceId,
    required this.label,
    required this.guidance,
    required this.overlay,
    this.isRequired = true,
    this.userAdded = false,
  });

  /// Stable machine identifier, e.g. `front`. Travels with every image.
  final String surfaceId;

  /// Inspector-facing name, e.g. `Front — Principal Display Panel`.
  final String label;

  /// What specifically to get in frame on this face.
  final String guidance;

  final OverlayShape overlay;

  /// Required steps must be captured or explicitly skipped before the product
  /// session can be completed. Extra angles are not required.
  final bool isRequired;

  /// True for extra angles the inspector added during the visit.
  final bool userAdded;

  Map<String, dynamic> toJson() => <String, dynamic>{
        'surfaceId': surfaceId,
        'label': label,
        'guidance': guidance,
        'overlay': overlay.name,
        'isRequired': isRequired,
        'userAdded': userAdded,
      };

  factory SurfaceStepDefinition.fromJson(Map<String, dynamic> json) =>
      SurfaceStepDefinition(
        surfaceId: json['surfaceId'] as String? ?? '',
        label: json['label'] as String? ?? '',
        guidance: json['guidance'] as String? ?? '',
        overlay: OverlayShape.values.firstWhere(
          (v) => v.name == json['overlay'],
          orElse: () => OverlayShape.freeform,
        ),
        isRequired: json['isRequired'] as bool? ?? true,
        userAdded: json['userAdded'] as bool? ?? false,
      );

  /// The default guided sequence: principal display panel, then back, then the
  /// two sides. Extra angles are appended by the inspector as needed.
  static List<SurfaceStepDefinition> standardSequence() =>
      const <SurfaceStepDefinition>[
        SurfaceStepDefinition(
          surfaceId: 'front',
          label: 'Front — Principal Display Panel',
          guidance: 'The face a shopper sees first. Get the product name, the '
              'net quantity declaration and the brand fully inside the guide.',
          overlay: OverlayShape.principalPanel,
        ),
        SurfaceStepDefinition(
          surfaceId: 'back',
          label: 'Back',
          guidance: 'Usually carries the manufacturer or packer address, the '
              'date of manufacture and the MRP. Keep the whole panel in frame.',
          overlay: OverlayShape.rearPanel,
        ),
        SurfaceStepDefinition(
          surfaceId: 'side_1',
          label: 'Side 1',
          guidance: 'The narrow face to the left of the front panel. Batch '
              'and lot markings often sit here.',
          overlay: OverlayShape.narrowSide,
        ),
        SurfaceStepDefinition(
          surfaceId: 'side_2',
          label: 'Side 2',
          guidance: 'The opposite narrow face. Capture it even if it looks '
              'blank — a blank side is itself a finding.',
          overlay: OverlayShape.narrowSide,
        ),
      ];

  static SurfaceStepDefinition extraAngle(int index) => SurfaceStepDefinition(
        surfaceId: 'extra_$index',
        label: 'Extra angle $index',
        guidance: 'An additional view — a close-up of small print, a seal, or '
            'a face not covered by the standard sequence.',
        overlay: OverlayShape.freeform,
        isRequired: false,
        userAdded: true,
      );
}

/// Why a surface was not photographed.
///
/// A skip is a recorded finding, not an absence of work. It exists so an
/// inspector who cannot reach a surface has somewhere honest to put that,
/// rather than photographing the same face twice to clear the coverage
/// indicator.
enum SkipReason {
  surfaceNotPresent,
  packageNotAccessible,
  obstructedPackaging,
  accessRefused,
  other,
}

extension SkipReasonX on SkipReason {
  String get label => switch (this) {
        SkipReason.surfaceNotPresent =>
          'This face does not exist on this package',
        SkipReason.packageNotAccessible =>
          'Package could not be handled or turned',
        SkipReason.obstructedPackaging =>
          'Face obscured by outer wrapping or a fixture',
        SkipReason.accessRefused => 'Access refused on site',
        SkipReason.other => 'Other reason',
      };

  /// A free-text note is mandatory for [SkipReason.other]; an unexplained
  /// "other" is not a reason.
  bool get requiresNote => this == SkipReason.other;

  static SkipReason parse(Object? value) => SkipReason.values.firstWhere(
        (v) => v.name == value,
        orElse: () => SkipReason.other,
      );
}

class StepSkip {
  const StepSkip({
    required this.reason,
    required this.recordedAtUtc,
    this.note,
  });

  final SkipReason reason;
  final DateTime recordedAtUtc;
  final String? note;

  String get description =>
      (note == null || note!.trim().isEmpty) ? reason.label : '${reason.label} — ${note!.trim()}';

  Map<String, dynamic> toJson() => <String, dynamic>{
        'reason': reason.name,
        'note': note,
        'recordedAtUtc': encodeTime(recordedAtUtc),
      };

  factory StepSkip.fromJson(Map<String, dynamic> json) => StepSkip(
        reason: SkipReasonX.parse(json['reason']),
        note: json['note'] as String?,
        recordedAtUtc: decodeTime(json['recordedAtUtc']),
      );
}

enum StepStatus { pending, captured, capturedNeedsReview, skipped }

extension StepStatusX on StepStatus {
  bool get isResolved => this != StepStatus.pending;

  String get label => switch (this) {
        StepStatus.pending => 'Not captured',
        StepStatus.captured => 'Captured',
        StepStatus.capturedNeedsReview => 'Manual review required',
        StepStatus.skipped => 'Not accessible',
      };
}

/// Live state of one step in the guided sequence.
class SurfaceStepState {
  SurfaceStepState({
    required this.definition,
    List<CaptureRecord>? attempts,
    this.acceptedCaptureId,
    this.skip,
  }) : attempts = attempts ?? <CaptureRecord>[];

  final SurfaceStepDefinition definition;

  /// Every attempt at this surface in order, including retaken ones.
  final List<CaptureRecord> attempts;

  String? acceptedCaptureId;
  StepSkip? skip;

  String get surfaceId => definition.surfaceId;
  String get label => definition.label;

  CaptureRecord? get acceptedCapture {
    final id = acceptedCaptureId;
    if (id == null) return null;
    for (final attempt in attempts) {
      if (attempt.captureId == id) return attempt;
    }
    return null;
  }

  CaptureRecord? get latestAttempt =>
      attempts.isEmpty ? null : attempts.last;

  /// Attempts whose quality analysis produced at least one blocking failure.
  int get failedAttemptCount => attempts
      .where((a) => a.quality.overall == QualityVerdict.fail)
      .length;

  /// True once the inspector has burned through the retry allowance and may
  /// proceed with the surface flagged.
  bool get retryLimitReached =>
      failedAttemptCount >= QualityThresholds.maxFailedAttempts;

  int get attemptsRemaining =>
      (QualityThresholds.maxFailedAttempts - failedAttemptCount)
          .clamp(0, QualityThresholds.maxFailedAttempts);

  StepStatus get status {
    if (skip != null) return StepStatus.skipped;
    final accepted = acceptedCapture;
    if (accepted == null) return StepStatus.pending;
    return accepted.needsManualReview
        ? StepStatus.capturedNeedsReview
        : StepStatus.captured;
  }

  /// The flag text that follows this surface through to review.
  static const String manualReviewFlag =
      'Manual Review Required — capture quality unresolved';

  String? get manualReviewReason {
    final accepted = acceptedCapture;
    if (accepted == null || !accepted.needsManualReview) return null;
    final reasons = accepted.quality.blockingReasons;
    return reasons.isEmpty
        ? manualReviewFlag
        : '$manualReviewFlag: ${reasons.join(' ')}';
  }

  Map<String, dynamic> toJson() => <String, dynamic>{
        'definition': definition.toJson(),
        'status': status.name,
        'acceptedCaptureId': acceptedCaptureId,
        'manualReviewRequired': status == StepStatus.capturedNeedsReview,
        'manualReviewReason': manualReviewReason,
        'skip': skip?.toJson(),
        'attemptCount': attempts.length,
        'failedAttemptCount': failedAttemptCount,
        'attempts': attempts.map((a) => a.toJson()).toList(),
      };

  factory SurfaceStepState.fromJson(Map<String, dynamic> json) {
    final skipJson = (json['skip'] as Map?)?.cast<String, dynamic>();
    return SurfaceStepState(
      definition: SurfaceStepDefinition.fromJson(
        (json['definition'] as Map?)?.cast<String, dynamic>() ??
            const <String, dynamic>{},
      ),
      attempts: ((json['attempts'] as List?) ?? const [])
          .whereType<Map<String, dynamic>>()
          .map(CaptureRecord.fromJson)
          .toList(),
      acceptedCaptureId: json['acceptedCaptureId'] as String?,
      skip: skipJson == null ? null : StepSkip.fromJson(skipJson),
    );
  }
}
