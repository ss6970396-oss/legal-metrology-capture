import '../core/json_store.dart';

/// Outcome of a single quality check.
///
/// `unavailable` is distinct from `fail`. A check that could not run (no OCR
/// engine on this platform, an image that would not decode) must never be
/// recorded as a passed check, and must never be silently dropped either —
/// a reviewer needs to see that the check was attempted and why it produced
/// no verdict.
enum QualityVerdict { pass, warn, fail, unavailable }

extension QualityVerdictX on QualityVerdict {
  String get wireName => name;

  /// Ordering used to reduce many check results to one overall verdict.
  int get severity => switch (this) {
        QualityVerdict.pass => 0,
        QualityVerdict.unavailable => 1,
        QualityVerdict.warn => 2,
        QualityVerdict.fail => 3,
      };

  String get label => switch (this) {
        QualityVerdict.pass => 'Pass',
        QualityVerdict.warn => 'Warn',
        QualityVerdict.fail => 'Fail',
        QualityVerdict.unavailable => 'Not assessed',
      };

  static QualityVerdict parse(Object? value) => QualityVerdict.values.firstWhere(
        (v) => v.name == value,
        orElse: () => QualityVerdict.unavailable,
      );
}

/// One check's result. Stored in metadata regardless of outcome — a passing
/// score is as much a part of the evidence record as a failing one, because
/// it documents that the check ran.
class QualityCheckResult {
  const QualityCheckResult({
    required this.checkId,
    required this.label,
    required this.score,
    required this.rawValue,
    required this.unit,
    required this.verdict,
    required this.message,
    this.diagnostics = const <String, double>{},
  });

  /// Stable machine identifier, e.g. `blur`.
  final String checkId;

  /// Inspector-facing name, e.g. `Sharpness`.
  final String label;

  /// Normalised 0..1 where higher is better. `-1` when the check could not run.
  final double score;

  /// The measurement in its native unit, kept unnormalised so thresholds can
  /// be recalibrated later against captures already collected in the field.
  final double rawValue;

  final String unit;
  final QualityVerdict verdict;

  /// Specific, actionable text shown to the inspector on a retake prompt.
  /// Never a generic "image quality problem".
  final String message;

  /// Additional raw measurements retained for threshold recalibration.
  final Map<String, double> diagnostics;

  bool get isBlocking => verdict == QualityVerdict.fail;

  factory QualityCheckResult.unavailable({
    required String checkId,
    required String label,
    required String message,
  }) =>
      QualityCheckResult(
        checkId: checkId,
        label: label,
        score: -1,
        rawValue: -1,
        unit: 'n/a',
        verdict: QualityVerdict.unavailable,
        message: message,
      );

  Map<String, dynamic> toJson() => <String, dynamic>{
        'checkId': checkId,
        'label': label,
        'score': score,
        'rawValue': rawValue,
        'unit': unit,
        'verdict': verdict.wireName,
        'message': message,
        'diagnostics': diagnostics,
      };

  factory QualityCheckResult.fromJson(Map<String, dynamic> json) =>
      QualityCheckResult(
        checkId: json['checkId'] as String? ?? 'unknown',
        label: json['label'] as String? ?? 'Unknown check',
        score: (json['score'] as num?)?.toDouble() ?? -1,
        rawValue: (json['rawValue'] as num?)?.toDouble() ?? -1,
        unit: json['unit'] as String? ?? 'n/a',
        verdict: QualityVerdictX.parse(json['verdict']),
        message: json['message'] as String? ?? '',
        diagnostics: (json['diagnostics'] as Map?)?.map(
              (k, v) => MapEntry('$k', (v as num).toDouble()),
            ) ??
            const <String, double>{},
      );
}

/// The full set of check results for one captured frame.
class QualityReport {
  const QualityReport({
    required this.results,
    required this.analysisDurationMs,
    required this.sourceWidth,
    required this.sourceHeight,
    required this.analysedWidth,
    required this.analysedHeight,
    required this.pipelineVersion,
    required this.analysedAtUtc,
  });

  final List<QualityCheckResult> results;
  final int analysisDurationMs;

  /// Dimensions of the stored JPEG.
  final int sourceWidth;
  final int sourceHeight;

  /// Dimensions of the downscaled buffer the pixel checks actually ran on.
  /// Thresholds are tied to this size, so it has to travel with the scores.
  final int analysedWidth;
  final int analysedHeight;

  final String pipelineVersion;
  final DateTime analysedAtUtc;

  QualityVerdict get overall => results.isEmpty
      ? QualityVerdict.unavailable
      : results
          .map((r) => r.verdict)
          .reduce((a, b) => a.severity >= b.severity ? a : b);

  List<QualityCheckResult> get failures =>
      results.where((r) => r.verdict == QualityVerdict.fail).toList();

  List<QualityCheckResult> get warnings =>
      results.where((r) => r.verdict == QualityVerdict.warn).toList();

  bool get requiresRetakePrompt => failures.isNotEmpty;

  /// The specific reasons shown on a retake prompt, in severity order.
  List<String> get blockingReasons =>
      failures.map((r) => r.message).toList(growable: false);

  QualityCheckResult? byId(String checkId) {
    for (final result in results) {
      if (result.checkId == checkId) return result;
    }
    return null;
  }

  static QualityReport empty() => QualityReport(
        results: const <QualityCheckResult>[],
        analysisDurationMs: 0,
        sourceWidth: 0,
        sourceHeight: 0,
        analysedWidth: 0,
        analysedHeight: 0,
        pipelineVersion: 'none',
        analysedAtUtc: DateTime.now().toUtc(),
      );

  Map<String, dynamic> toJson() => <String, dynamic>{
        'overall': overall.wireName,
        'pipelineVersion': pipelineVersion,
        'analysedAtUtc': encodeTime(analysedAtUtc),
        'analysisDurationMs': analysisDurationMs,
        'sourceWidth': sourceWidth,
        'sourceHeight': sourceHeight,
        'analysedWidth': analysedWidth,
        'analysedHeight': analysedHeight,
        'checks': results.map((r) => r.toJson()).toList(),
      };

  factory QualityReport.fromJson(Map<String, dynamic> json) => QualityReport(
        results: ((json['checks'] as List?) ?? const [])
            .whereType<Map<String, dynamic>>()
            .map(QualityCheckResult.fromJson)
            .toList(),
        analysisDurationMs: (json['analysisDurationMs'] as num?)?.toInt() ?? 0,
        sourceWidth: (json['sourceWidth'] as num?)?.toInt() ?? 0,
        sourceHeight: (json['sourceHeight'] as num?)?.toInt() ?? 0,
        analysedWidth: (json['analysedWidth'] as num?)?.toInt() ?? 0,
        analysedHeight: (json['analysedHeight'] as num?)?.toInt() ?? 0,
        pipelineVersion: json['pipelineVersion'] as String? ?? 'unknown',
        analysedAtUtc: decodeTime(json['analysedAtUtc']),
      );
}
