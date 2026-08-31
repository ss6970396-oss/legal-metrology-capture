import 'dart:io';

import 'package:flutter/foundation.dart';

import '../models/quality_report.dart';
import 'pixel_analysis.dart';
import 'text_readability_check.dart';
import 'thresholds.dart';

/// Runs every quality check against a freshly captured frame.
///
/// Two stages, for a reason. The pixel checks are pure computation over a byte
/// buffer, so they go to a background isolate via [compute] and keep the
/// camera screen responsive. The OCR pass talks to ML Kit over a platform
/// channel, which only works from the root isolate, so it runs here.
///
/// Every check's result is returned regardless of outcome — passes, warnings,
/// failures, and checks that could not run at all. The caller stores all of
/// them. A quality check that ran and passed is part of the evidence record:
/// it documents that the frame was examined, which is a different claim from
/// "nobody looked".
class QualityPipeline {
  QualityPipeline({TextReadabilityCheck? textReadabilityCheck})
      : _textCheck = textReadabilityCheck ?? TextReadabilityCheck();

  final TextReadabilityCheck _textCheck;

  Future<QualityReport> analyse(File jpeg) async {
    final stopwatch = Stopwatch()..start();

    final bytes = await jpeg.readAsBytes();
    final pixelOutcome = await compute(analysePixelsSync, bytes);

    final results = <QualityCheckResult>[
      ...((pixelOutcome['checks'] as List?) ?? const [])
          .whereType<Map<String, dynamic>>()
          .map(QualityCheckResult.fromJson),
    ];

    final sourceWidth = (pixelOutcome['sourceWidth'] as num?)?.toInt() ?? 0;
    final sourceHeight = (pixelOutcome['sourceHeight'] as num?)?.toInt() ?? 0;

    results.add(
      await _textCheck.run(imagePath: jpeg.path, imageHeight: sourceHeight),
    );

    stopwatch.stop();

    return QualityReport(
      results: results,
      analysisDurationMs: stopwatch.elapsedMilliseconds,
      sourceWidth: sourceWidth,
      sourceHeight: sourceHeight,
      analysedWidth: (pixelOutcome['analysedWidth'] as num?)?.toInt() ?? 0,
      analysedHeight: (pixelOutcome['analysedHeight'] as num?)?.toInt() ?? 0,
      pipelineVersion: QualityThresholds.pipelineVersion,
      analysedAtUtc: DateTime.now().toUtc(),
    );
  }

  Future<void> dispose() => _textCheck.dispose();
}
