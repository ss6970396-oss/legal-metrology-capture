import 'dart:io';
import 'dart:ui' show Rect;

import 'package:google_mlkit_text_recognition/google_mlkit_text_recognition.dart';

import '../models/quality_report.dart';
import 'thresholds.dart';

/// Fast on-device readability pass.
///
/// This is deliberately NOT text extraction. The question it answers is "would
/// a person reviewing this photograph be able to read the declaration", not
/// "what does the declaration say". Reading the declaration is a compliance
/// determination and it happens after upload, by the backend extraction
/// pipeline and then a person — not here, on a phone, in a shop aisle.
///
/// That distinction has a concrete consequence in the code: the recognised
/// strings are used to count and measure lines and are then discarded. Nothing
/// derived from the text content is written to the metadata bundle. Storing it
/// would create a second, lower-quality transcript of the package sitting
/// beside the real evidence, and there is no good outcome from that appearing
/// in a case file. The authoritative transcript comes from the server-side
/// OCR stack, against the full-resolution original.
///
/// ## Scripts
///
/// Indian retail packaging routinely carries its declarations in Devanagari,
/// in Latin, or in both at once — a Hindi net-quantity line directly above its
/// English equivalent is the ordinary case, not an exotic one. ML Kit will
/// only find what the recognizer it was handed is built for, so a Latin-only
/// pass over a Hindi panel returns nothing and the check concludes there is no
/// readable text on the surface. That verdict is a `fail`, and a `fail` sends
/// the inspector back to retake a photograph that was fine to begin with.
///
/// So both recognizers run over every frame and their lines are pooled. The
/// cost is a second inference per capture; the alternative is an unwinnable
/// retake loop in front of any package that declares in Hindi.
class TextReadabilityCheck {
  TextReadabilityCheck();

  static const String checkId = 'text_readability';
  static const String label = 'Text legibility';

  /// The scripts worth attempting on Indian retail packaging.
  ///
  /// Devanagari covers Hindi and Marathi. The other ML Kit script packs
  /// (Chinese, Japanese, Korean) are not shipped in this app's Android
  /// dependencies and would throw if requested — see the `dependencies` block
  /// in `android/app/build.gradle.kts`, which has to declare each script pack
  /// explicitly because the Flutter plugin lists them all as `compileOnly`.
  ///
  /// Tamil and Telugu appear on packaging in the south and ML Kit has no model
  /// for either. That is a known coverage gap in the on-device assist, not in
  /// the authoritative path: the server stack reads both. It means this check
  /// under-reports legibility on a Tamil-only panel, which is why a `fail`
  /// here is a prompt to the inspector and never a finding about the package.
  static const List<TextRecognitionScript> scripts = <TextRecognitionScript>[
    TextRecognitionScript.latin,
    // Spelled `devanagiri` by the plugin. That is the package's own
    // misspelling of Devanagari, not a typo here.
    TextRecognitionScript.devanagiri,
  ];

  final Map<TextRecognitionScript, TextRecognizer> _recognizers =
      <TextRecognitionScript, TextRecognizer>{};

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

    final input = InputImage.fromFilePath(imagePath);
    final lines = <TextLine>[];
    final blockCounts = <TextRecognitionScript, int>{};
    final failures = <String>[];

    for (final script in scripts) {
      try {
        final recognizer =
            _recognizers[script] ??= TextRecognizer(script: script);
        final recognised = await recognizer.processImage(input);
        blockCounts[script] = recognised.blocks.length;
        for (final block in recognised.blocks) {
          lines.addAll(block.lines);
        }
      } on Object catch (error) {
        // One script pack missing or misbehaving must not sink the whole
        // check. A Latin result on its own is still worth having; it is only
        // when every script fails that legibility is genuinely unassessed.
        blockCounts[script] = 0;
        failures.add('${script.name}: $error');
      }
    }

    if (failures.length == scripts.length) {
      // A recognition failure is not a quality failure. Recording it as `fail`
      // would push the inspector into a retake loop they cannot win.
      return QualityCheckResult.unavailable(
        checkId: checkId,
        label: label,
        message: 'Text recognition could not run on this image '
            '(${failures.join('; ')}); legibility was not assessed.',
      );
    }

    return _score(
      lines: _deduplicate(lines),
      imageHeight: imageHeight,
      blockCounts: blockCounts,
      failures: failures,
    );
  }

  /// Drops lines the two recognizers both found.
  ///
  /// Latin and Devanagari models overlap on digits and on Latin-alphabet
  /// brand text, so a bilingual panel yields the same physical line twice. Left
  /// in, those duplicates inflate the line count and skew the legible
  /// fraction toward whichever script happened to read the large print.
  ///
  /// Two detections are treated as the same line when their bounding boxes
  /// substantially overlap. Exact box equality would not do: the two models
  /// return boxes a pixel or two apart on identical text.
  static List<TextLine> _deduplicate(List<TextLine> lines) {
    // Largest first, so the survivor of a pair is the more complete detection
    // rather than whichever was recognised first.
    final sorted = lines.toList()
      ..sort((a, b) => _area(b.boundingBox).compareTo(_area(a.boundingBox)));

    final kept = <TextLine>[];
    for (final line in sorted) {
      final isDuplicate = kept.any(
        (other) => _overlapFraction(line.boundingBox, other.boundingBox) >
            QualityThresholds.ocrDuplicateLineOverlap,
      );
      if (!isDuplicate) kept.add(line);
    }
    return kept;
  }

  static double _area(Rect r) => r.width * r.height;

  /// Intersection over the smaller of the two boxes.
  ///
  /// Deliberately not IoU. A short line detected inside a longer one — the
  /// common shape of this overlap, where one model merges a wrapped line its
  /// counterpart split — scores low on IoU while still plainly being the same
  /// printed text.
  static double _overlapFraction(Rect a, Rect b) {
    final left = a.left > b.left ? a.left : b.left;
    final top = a.top > b.top ? a.top : b.top;
    final right = a.right < b.right ? a.right : b.right;
    final bottom = a.bottom < b.bottom ? a.bottom : b.bottom;
    if (right <= left || bottom <= top) return 0;

    final intersection = (right - left) * (bottom - top);
    final smaller = _area(a) < _area(b) ? _area(a) : _area(b);
    return smaller <= 0 ? 0 : intersection / smaller;
  }

  QualityCheckResult _score({
    required List<TextLine> lines,
    required int imageHeight,
    required Map<TextRecognitionScript, int> blockCounts,
    required List<String> failures,
  }) {
    final scriptDiagnostics = <String, double>{
      for (final entry in blockCounts.entries)
        'blockCount_${entry.key.name}': entry.value.toDouble(),
    };

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
        diagnostics: <String, double>{
          'blockCount': 0,
          'lineCount': 0,
          ...scriptDiagnostics,
        },
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
      'blockCount':
          blockCounts.values.fold<int>(0, (sum, v) => sum + v).toDouble(),
      'lineCount': lines.length.toDouble(),
      'legibleLineCount': legibleLines.toDouble(),
      'legibleLineFraction': legibleFraction,
      'minimumLineHeightPx': minimumHeight,
      'meanConfidence': meanConfidence,
      'confidenceReported': hasConfidence ? 1.0 : 0.0,
      'scriptPassesFailed': failures.length.toDouble(),
      ...scriptDiagnostics,
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
    for (final recognizer in _recognizers.values) {
      await recognizer.close();
    }
    _recognizers.clear();
  }
}
