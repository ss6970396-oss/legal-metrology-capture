import 'dart:io';

import 'package:google_mlkit_text_recognition/google_mlkit_text_recognition.dart';

import '../models/quality_report.dart';
import 'thresholds.dart';

/// Fast on-device readability pass.
///
/// This is deliberately NOT text extraction. The question it answers is "would
/// a person reviewing this photograph be able to read the declaration", not
/// "what does the declaration say". Reading the declaration is a compliance
/// determination and it happens after upload, by a person, on the full
/// resolution image — not here, on a phone, in a shop aisle.
///
/// That distinction has a concrete consequence in the code: the recognised
/// strings are used to count and measure lines and are then discarded. Nothing
/// derived from the text content is written to the metadata bundle. Storing it
/// would create a second, lower-quality transcript of the package sitting
/// beside the real evidence, and there is no good outcome from that appearing
/// in a case file.
class TextReadabilityCheck {
  TextReadabilityCheck();

  static const String checkId = 'text_readability';
  static const String label = 'Text legibility';

  TextRecognizer? _recognizer;

  /// ML Kit ships native binaries for Android and iOS only. On any other
  /// platform the check reports `unavailable` rather than passing by default.
  bool get isSupported => Platform.isAndroid || Platform.isIOS;

  Future<QualityCheckResult> run({
    required String imagePath,
    required int imageHeight,
  }) async {
    if (!isSupported) {
      return QualityCheckResult.unavailable(
        checkId: checkId,
        label: label,
        message: 'On-device text recognition is not available on '
            '${Platform.operatingSystem}; legibility was not assessed. The '
            'other quality checks still ran.',
      );
    }

    try {
      final recognizer =
          _recognizer ??= TextRecognizer(script: TextRecognitionScript.latin);
      final recognised =
          await recognizer.processImage(InputImage.fromFilePath(imagePath));
      return _score(recognised, imageHeight);
    } on Object catch (error) {
      // A recognition failure is not a quality failure. Recording it as `fail`
      // would push the inspector into a retake loop they cannot win.
      return QualityCheckResult.unavailable(
        checkId: checkId,
        label: label,
        message: 'Text recognition could not run on this image '
            '($error); legibility was not assessed.',
      );
    }
  }

  QualityCheckResult _score(RecognizedText recognised, int imageHeight) {
    final lines = <TextLine>[
      for (final block in recognised.blocks) ...block.lines,
    ];

    if (lines.isEmpty) {
      return QualityCheckResult(
        checkId: checkId,
        label: label,
        score: 0,
        rawValue: 0,
        unit: 'text-lines',
        verdict: QualityVerdict.fail,
        message: 'No readable text was found anywhere on this surface. If the '
            'panel really does carry printed text, move closer, steady the '
            'phone and retake. If this face of the package genuinely has no '
            'text, mark the step not accessible with that reason rather than '
            'accepting a blank capture.',
        diagnostics: const <String, double>{'blockCount': 0, 'lineCount': 0},
      );
    }

    // ML Kit reports per-line confidence on iOS but leaves it null on Android
    // in most builds, so the geometric fallback below is the common path, not
    // the exceptional one.
    final confidences = <double>[
      for (final line in lines)
        if (line.confidence != null) line.confidence!,
    ];

    final minimumHeight = imageHeight > 0
        ? imageHeight * QualityThresholds.ocrLegibleLineHeightFraction
        : 12.0;
    final legibleLines = lines
        .where((line) => line.boundingBox.height >= minimumHeight)
        .length;
    final legibleFraction = legibleLines / lines.length;

    final hasConfidence = confidences.isNotEmpty;
    final meanConfidence = hasConfidence
        ? confidences.reduce((a, b) => a + b) / confidences.length
        : -1.0;

    final diagnostics = <String, double>{
      'blockCount': recognised.blocks.length.toDouble(),
      'lineCount': lines.length.toDouble(),
      'legibleLineCount': legibleLines.toDouble(),
      'legibleLineFraction': legibleFraction,
      'minimumLineHeightPx': minimumHeight,
      'meanConfidence': meanConfidence,
      'confidenceReported': hasConfidence ? 1.0 : 0.0,
    };

    if (lines.length < QualityThresholds.ocrMinimumLines) {
      return QualityCheckResult(
        checkId: checkId,
        label: label,
        score: legibleFraction * 0.5,
        rawValue: lines.length.toDouble(),
        unit: 'text-lines',
        verdict: QualityVerdict.fail,
        message: 'Only ${lines.length} line'
            '${lines.length == 1 ? '' : 's'} of text could be made out, which '
            'is too little to review a declaration panel. Move closer and fill '
            'the frame with the printed area, then retake.',
        diagnostics: diagnostics,
      );
    }

    if (hasConfidence) {
      final QualityVerdict verdict;
      final String message;
      if (meanConfidence < QualityThresholds.ocrConfidenceFailBelow) {
        verdict = QualityVerdict.fail;
        message = 'Text is present but hard to make out (recognition '
            'confidence ${(meanConfidence * 100).toStringAsFixed(0)}%). The '
            'small print will not be reviewable — steady the phone, get closer '
            'and retake.';
      } else if (meanConfidence < QualityThresholds.ocrConfidenceWarnBelow) {
        verdict = QualityVerdict.warn;
        message = 'Text is readable but marginal (recognition confidence '
            '${(meanConfidence * 100).toStringAsFixed(0)}%). Check the '
            'declaration panel specifically before accepting.';
      } else {
        verdict = QualityVerdict.pass;
        message = 'Text is legible (recognition confidence '
            '${(meanConfidence * 100).toStringAsFixed(0)}%, ${lines.length} '
            'lines).';
      }
      return QualityCheckResult(
        checkId: checkId,
        label: label,
        score: meanConfidence.clamp(0.0, 1.0),
        rawValue: meanConfidence,
        unit: 'mean-line-confidence',
        verdict: verdict,
        message: message,
        diagnostics: diagnostics,
      );
    }

    // Geometric fallback: how much of the detected text is rendered large
    // enough to survive review. This is a weaker signal than a real confidence
    // score and the unit recorded in metadata says so.
    final QualityVerdict verdict;
    final String message;
    if (legibleFraction < QualityThresholds.ocrLegibleFractionFailBelow) {
      verdict = QualityVerdict.fail;
      message = 'Most of the text on this surface is too small to be read back '
          '(only ${(legibleFraction * 100).toStringAsFixed(0)}% of the '
          '${lines.length} detected lines are large enough). Move closer so '
          'the printed panel fills more of the frame, then retake.';
    } else if (legibleFraction < QualityThresholds.ocrLegibleFractionWarnBelow) {
      verdict = QualityVerdict.warn;
      message = 'Some text is small — '
          '${(legibleFraction * 100).toStringAsFixed(0)}% of ${lines.length} '
          'detected lines are comfortably readable. Check the declaration '
          'panel is among the legible part.';
    } else {
      verdict = QualityVerdict.pass;
      message = 'Text is legible '
          '(${(legibleFraction * 100).toStringAsFixed(0)}% of ${lines.length} '
          'detected lines are large enough to read).';
    }

    return QualityCheckResult(
      checkId: checkId,
      label: label,
      score: legibleFraction.clamp(0.0, 1.0),
      rawValue: legibleFraction,
      unit: 'legible-line-fraction',
      verdict: verdict,
      message: message,
      diagnostics: diagnostics,
    );
  }

  Future<void> dispose() async {
    await _recognizer?.close();
    _recognizer = null;
  }
}
