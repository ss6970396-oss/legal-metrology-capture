import '../core/json_store.dart';
import 'barcode_scan.dart';
import 'capture_context.dart';
import 'surface_step.dart';

/// One package examined within a visit.
///
/// The [productSessionId] is minted client-side when the inspector starts on a
/// package and is stamped on every image, scan and check result that follows.
class ProductSession {
  ProductSession({
    required this.productSessionId,
    required this.inspectionId,
    required this.startedAtUtc,
    required this.steps,
    this.context = const CaptureContext(),
    this.identification,
    this.productLabel,
    this.closedAtUtc,
  });

  final String productSessionId;
  final String inspectionId;
  final DateTime startedAtUtc;

  /// The guided sequence for this package, in order.
  final List<SurfaceStepState> steps;

  /// What the inspector declares about how this package was offered for sale.
  ///
  /// Declared, not observed. The compliance engine uses it to decide which
  /// rules apply, so it must be answered by a person rather than defaulted —
  /// see [CaptureContext].
  CaptureContext context;

  /// Product identity from a barcode. Never compliance evidence — see
  /// [BarcodeScan].
  BarcodeScan? identification;

  /// Optional inspector-supplied name, useful when no barcode is present.
  String? productLabel;

  DateTime? closedAtUtc;

  List<SurfaceStepState> get requiredSteps =>
      steps.where((s) => s.definition.isRequired).toList();

  /// Steps that have been captured or explicitly skipped. Used by the coverage
  /// indicator. A skipped step counts as resolved: the inspector recorded why
  /// there is no photograph, which is a real answer.
  int get resolvedRequiredCount =>
      requiredSteps.where((s) => s.status.isResolved).length;

  int get requiredCount => requiredSteps.length;

  int get capturedCount =>
      steps.where((s) => s.acceptedCapture != null).length;

  /// The coverage string shown throughout the flow.
  String get coverageLabel =>
      '$resolvedRequiredCount of $requiredCount captured';

  /// A product session is only complete when every required step has either a
  /// capture the inspector accepted or a recorded reason it has none, and the
  /// commercial context has been declared. There is no path that marks it
  /// complete with a step still pending or an applicability flag unanswered.
  bool get isComplete =>
      context.isComplete && requiredSteps.every((s) => s.status.isResolved);

  /// Coverage alone, ignoring the context declaration. The capture flow uses
  /// this to decide when to stop asking for photographs; [isComplete] is what
  /// gates submission.
  bool get isCoverageComplete =>
      requiredSteps.every((s) => s.status.isResolved);

  /// What still stands between this package and a submittable session, in the
  /// order the flow should ask for it.
  List<String> get outstandingWork => <String>[
        for (final step in pendingSteps) 'Capture ${step.label}',
        ...context.missingFields,
      ];

  List<SurfaceStepState> get pendingSteps =>
      requiredSteps.where((s) => !s.status.isResolved).toList();

  List<SurfaceStepState> get stepsNeedingManualReview => steps
      .where((s) => s.status == StepStatus.capturedNeedsReview)
      .toList();

  bool get hasManualReviewFlags => stepsNeedingManualReview.isNotEmpty;

  SurfaceStepState? stepById(String surfaceId) {
    for (final step in steps) {
      if (step.surfaceId == surfaceId) return step;
    }
    return null;
  }

  /// The next step the guided flow should present: the first unresolved
  /// required step, in sequence order.
  SurfaceStepState? get nextStep {
    for (final step in requiredSteps) {
      if (!step.status.isResolved) return step;
    }
    return null;
  }

  factory ProductSession.start({
    required String inspectionId,
    required String productSessionId,
  }) =>
      ProductSession(
        productSessionId: productSessionId,
        inspectionId: inspectionId,
        startedAtUtc: DateTime.now().toUtc(),
        steps: SurfaceStepDefinition.standardSequence()
            .map((d) => SurfaceStepState(definition: d))
            .toList(),
      );

  Map<String, dynamic> toJson() => <String, dynamic>{
        'productSessionId': productSessionId,
        'inspectionId': inspectionId,
        'productLabel': productLabel,
        'startedAtUtc': encodeTime(startedAtUtc),
        'closedAtUtc': closedAtUtc == null ? null : encodeTime(closedAtUtc!),
        'isComplete': isComplete,
        'coverage': <String, dynamic>{
          'requiredSteps': requiredCount,
          'resolvedSteps': resolvedRequiredCount,
          'capturedSteps': capturedCount,
          'label': coverageLabel,
        },
        'manualReviewRequired': hasManualReviewFlags,
        'context': context.toJson(),
        'identification': identification?.toJson(),
        'steps': steps.map((s) => s.toJson()).toList(),
      };

  factory ProductSession.fromJson(Map<String, dynamic> json) {
    final identificationJson =
        (json['identification'] as Map?)?.cast<String, dynamic>();
    return ProductSession(
      productSessionId: json['productSessionId'] as String? ?? '',
      inspectionId: json['inspectionId'] as String? ?? '',
      productLabel: json['productLabel'] as String?,
      context: CaptureContext.fromJson(
        (json['context'] as Map?)?.cast<String, dynamic>() ??
            const <String, dynamic>{},
      ),
      startedAtUtc: decodeTime(json['startedAtUtc']),
      closedAtUtc: decodeTimeOrNull(json['closedAtUtc']),
      identification: identificationJson == null
          ? null
          : BarcodeScan.fromJson(identificationJson),
      steps: ((json['steps'] as List?) ?? const [])
          .whereType<Map<String, dynamic>>()
          .map(SurfaceStepState.fromJson)
          .toList(),
    );
  }
}
