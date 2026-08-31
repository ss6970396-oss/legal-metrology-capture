import 'package:flutter_test/flutter_test.dart';
import 'package:legal_metrology_capture/models/barcode_scan.dart';
import 'package:legal_metrology_capture/models/capture_record.dart';
import 'package:legal_metrology_capture/models/product_session.dart';
import 'package:legal_metrology_capture/models/quality_report.dart';
import 'package:legal_metrology_capture/models/surface_step.dart';
import 'package:legal_metrology_capture/quality/thresholds.dart';

/// Tests for the logic that decides what counts as finished evidence.
///
/// These are the rules where a bug would be invisible in use and damaging
/// later: a package that reports itself complete when a face was never
/// photographed, or a barcode that quietly becomes a compliance result. They
/// are all pure Dart, so they run without a device.
void main() {
  group('GTIN check digit', () {
    test('accepts known-good retail codes', () {
      expect(isValidGtin('4006381333931'), isTrue); // EAN-13
      expect(isValidGtin('036000291452'), isTrue); // UPC-A
      expect(isValidGtin('73513537'), isTrue); // EAN-8
    });

    test('rejects a transposed digit', () {
      expect(isValidGtin('4006381333913'), isFalse);
    });

    test('rejects wrong lengths and non-digits', () {
      expect(isValidGtin('40063813339'), isFalse);
      expect(isValidGtin('40063813339XY'), isFalse);
      expect(isValidGtin(''), isFalse);
    });
  });

  group('barcode is identification, never compliance evidence', () {
    test('serialises with an explicit non-evidence marker', () {
      final scan = BarcodeScan(
        scanId: 's1',
        inspectionId: 'i1',
        productSessionId: 'p1',
        rawValue: '4006381333931',
        symbology: BarcodeSymbology.ean13,
        entryMode: BarcodeEntryMode.liveScan,
        scannedAtUtc: DateTime.utc(2026, 1, 1),
        checkDigitValid: true,
      );
      final json = scan.toJson();
      expect(json['isComplianceEvidence'], isFalse);
      expect(json['evidenceClass'], 'product_identification');
    });

    test('does not contribute to surface coverage', () {
      final product = _product();
      product.identification = BarcodeScan(
        scanId: 's1',
        inspectionId: product.inspectionId,
        productSessionId: product.productSessionId,
        rawValue: '4006381333931',
        symbology: BarcodeSymbology.ean13,
        entryMode: BarcodeEntryMode.liveScan,
        scannedAtUtc: DateTime.utc(2026, 1, 1),
        checkDigitValid: true,
      );
      expect(product.resolvedRequiredCount, 0);
      expect(product.isComplete, isFalse);
    });
  });

  group('product completion', () {
    test('is false while any required surface is pending', () {
      final product = _product();
      _accept(product.steps[0]);
      _accept(product.steps[1]);
      expect(product.coverageLabel, '2 of 4 captured');
      expect(product.isComplete, isFalse);
    });

    test('an explicitly skipped step resolves the step', () {
      final product = _product();
      _accept(product.steps[0]);
      _accept(product.steps[1]);
      _accept(product.steps[2]);
      product.steps[3].skip = StepSkip(
        reason: SkipReason.surfaceNotPresent,
        recordedAtUtc: DateTime.utc(2026, 1, 1),
      );
      expect(product.isComplete, isTrue);
      expect(product.coverageLabel, '4 of 4 captured');
    });

    test('an optional extra angle never blocks completion', () {
      final product = _product();
      for (final step in product.steps) {
        _accept(step);
      }
      product.steps.add(
        SurfaceStepState(definition: SurfaceStepDefinition.extraAngle(1)),
      );
      expect(product.isComplete, isTrue);
    });
  });

  group('retake allowance', () {
    test('counts only attempts that failed their checks', () {
      final step = _step();
      step.attempts.add(_capture(step, 1, QualityVerdict.fail));
      step.attempts.add(_capture(step, 2, QualityVerdict.warn));
      expect(step.failedAttemptCount, 1);
      expect(step.retryLimitReached, isFalse);
    });

    test('opens the manual-review route once the allowance is spent', () {
      final step = _step();
      for (var i = 1; i <= QualityThresholds.maxFailedAttempts; i++) {
        step.attempts.add(_capture(step, i, QualityVerdict.fail));
      }
      expect(step.retryLimitReached, isTrue);
      expect(step.attemptsRemaining, 0);
    });

    test('a capture accepted under review carries the flag', () {
      final step = _step();
      final record = _capture(step, 1, QualityVerdict.fail)
          .copyWith(disposition: CaptureDisposition.acceptedUnderManualReview);
      step.attempts.add(record);
      step.acceptedCaptureId = record.captureId;

      expect(step.status, StepStatus.capturedNeedsReview);
      expect(
        step.manualReviewReason,
        contains('Manual Review Required — capture quality unresolved'),
      );
      // The specific failure reason travels with the flag, not a generic one.
      expect(step.manualReviewReason, contains('out of focus'));
    });
  });

  group('quality report', () {
    test('overall verdict is the worst of the checks', () {
      expect(
        _report(QualityVerdict.fail).overall,
        QualityVerdict.fail,
      );
      expect(
        _report(QualityVerdict.warn).overall,
        QualityVerdict.warn,
      );
    });

    test('keeps passing and unavailable results, not just failures', () {
      final report = _report(QualityVerdict.fail);
      expect(report.results.length, 3);
      expect(
        report.results.map((r) => r.verdict),
        containsAll(<QualityVerdict>[
          QualityVerdict.pass,
          QualityVerdict.unavailable,
        ]),
      );
    });

    test('survives a serialise/deserialise round trip', () {
      final original = _report(QualityVerdict.fail);
      final restored = QualityReport.fromJson(original.toJson());
      expect(restored.overall, original.overall);
      expect(restored.results.length, original.results.length);
      expect(restored.blockingReasons, original.blockingReasons);
    });
  });
}

ProductSession _product() => ProductSession.start(
      inspectionId: 'inspection-1',
      productSessionId: 'product-1',
    );

SurfaceStepState _step() => SurfaceStepState(
      definition: SurfaceStepDefinition.standardSequence().first,
    );

void _accept(SurfaceStepState step) {
  final record = _capture(step, 1, QualityVerdict.pass)
      .copyWith(disposition: CaptureDisposition.accepted);
  step.attempts.add(record);
  step.acceptedCaptureId = record.captureId;
}

QualityReport _report(QualityVerdict worst) => QualityReport(
      results: <QualityCheckResult>[
        const QualityCheckResult(
          checkId: 'brightness',
          label: 'Exposure',
          score: 0.9,
          rawValue: 130,
          unit: 'mean-luminance-0-255',
          verdict: QualityVerdict.pass,
          message: 'Well exposed (130 of 255).',
        ),
        QualityCheckResult(
          checkId: 'blur',
          label: 'Sharpness',
          score: 0.1,
          rawValue: 20,
          unit: 'laplacian-variance',
          verdict: worst,
          message: 'Photo is out of focus (sharpness 20, needs 45 or more).',
        ),
        QualityCheckResult.unavailable(
          checkId: 'text_readability',
          label: 'Text legibility',
          message: 'On-device text recognition is not available on this '
              'platform; legibility was not assessed.',
        ),
      ],
      analysisDurationMs: 120,
      sourceWidth: 4000,
      sourceHeight: 3000,
      analysedWidth: 800,
      analysedHeight: 600,
      pipelineVersion: QualityThresholds.pipelineVersion,
      analysedAtUtc: DateTime.utc(2026, 1, 1),
    );

CaptureRecord _capture(
  SurfaceStepState step,
  int attempt,
  QualityVerdict verdict,
) =>
    CaptureRecord(
      captureId: 'capture-${step.surfaceId}-$attempt',
      inspectionId: 'inspection-1',
      productSessionId: 'product-1',
      surfaceId: step.surfaceId,
      surfaceLabel: step.label,
      capturedAtUtc: DateTime.utc(2026, 1, 1),
      localPath: '/tmp/capture-$attempt.jpg',
      fileSizeBytes: 1024,
      sha256: 'deadbeef',
      attemptNumber: attempt,
      quality: _report(verdict),
      disposition: CaptureDisposition.pendingReview,
      deviceId: 'device-1',
      deviceModel: 'test',
      osVersion: 'test',
    );
