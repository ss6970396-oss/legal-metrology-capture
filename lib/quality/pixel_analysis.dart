import 'dart:math' as math;
import 'dart:typed_data';

import 'package:image/image.dart' as img;

import '../models/quality_report.dart';
import 'thresholds.dart';

/// Pixel-domain quality analysis.
///
/// This whole file is written to be callable from a background isolate: it
/// takes bytes in and returns plain JSON-shaped maps out, touching no plugin,
/// no file handle, and no Flutter binding. Decoding a full-resolution JPEG and
/// running four convolutions over it takes tens to hundreds of milliseconds,
/// and it happens immediately after the shutter, so doing it on the UI isolate
/// would visibly freeze the camera screen at exactly the wrong moment.
///
/// The source image is never modified or re-encoded here. It is decoded into a
/// throwaway buffer, downscaled, and discarded. The JPEG on disk keeps the
/// exact bytes the camera produced, EXIF included.
Map<String, dynamic> analysePixelsSync(Uint8List jpegBytes) {
  final decoded = img.decodeJpg(jpegBytes);
  if (decoded == null) {
    return <String, dynamic>{
      'sourceWidth': 0,
      'sourceHeight': 0,
      'analysedWidth': 0,
      'analysedHeight': 0,
      'checks': <Map<String, dynamic>>[
        QualityCheckResult.unavailable(
          checkId: 'decode',
          label: 'Image decode',
          message: 'The captured file could not be decoded as JPEG, so no '
              'quality checks could run. Retake this surface.',
        ).toJson(),
      ],
    };
  }

  final scaled = _downscale(decoded, QualityThresholds.analysisMaxDimension);
  final luma = _LumaPlane.fromImage(scaled);

  final checks = <QualityCheckResult>[
    _blurCheck(luma),
    _brightnessCheck(luma),
    _glareCheck(luma),
    _croppingCheck(luma),
  ];

  return <String, dynamic>{
    'sourceWidth': decoded.width,
    'sourceHeight': decoded.height,
    'analysedWidth': luma.width,
    'analysedHeight': luma.height,
    'checks': checks.map((c) => c.toJson()).toList(),
  };
}

img.Image _downscale(img.Image source, int maxDimension) {
  final longest = math.max(source.width, source.height);
  if (longest <= maxDimension) return source;
  final scale = maxDimension / longest;
  return img.copyResize(
    source,
    width: math.max(1, (source.width * scale).round()),
    height: math.max(1, (source.height * scale).round()),
    interpolation: img.Interpolation.average,
  );
}

/// Single-channel luminance buffer. Every check below reads from this rather
/// than from the decoded image, so the comparatively expensive per-pixel
/// colour conversion happens exactly once.
class _LumaPlane {
  _LumaPlane(this.data, this.width, this.height);

  final Uint8List data;
  final int width;
  final int height;

  int get length => data.length;

  factory _LumaPlane.fromImage(img.Image source) {
    final width = source.width;
    final height = source.height;
    final data = Uint8List(width * height);
    // JPEG is 8-bit, but guard anyway: a 16-bit buffer would otherwise push
    // every threshold in this file off by a factor of 257.
    final maxChannel = source.maxChannelValue;
    final normalise = maxChannel > 0 ? 255.0 / maxChannel : 1.0;

    var index = 0;
    img.Pixel? cursor;
    for (var y = 0; y < height; y++) {
      for (var x = 0; x < width; x++) {
        cursor = source.getPixel(x, y, cursor);
        // Rec. 601 luma, which matches how the eye weights the channels — and
        // "can a person read this" is the question every check here answers.
        final value =
            (0.299 * cursor.r + 0.587 * cursor.g + 0.114 * cursor.b) *
                normalise;
        data[index++] = value < 0 ? 0 : (value > 255 ? 255 : value.round());
      }
    }
    return _LumaPlane(data, width, height);
  }
}

// ---------------------------------------------------------------------------
// Blur — variance of the Laplacian.
// ---------------------------------------------------------------------------

QualityCheckResult _blurCheck(_LumaPlane luma) {
  const label = 'Sharpness';
  const checkId = 'blur';

  if (luma.width < 3 || luma.height < 3) {
    return QualityCheckResult.unavailable(
      checkId: checkId,
      label: label,
      message: 'Image too small to measure sharpness.',
    );
  }

  // 4-neighbour Laplacian. A sharp image has strong second derivatives at
  // edges, so the response spreads out and its variance is high; blur damps
  // exactly those frequencies and collapses the variance toward zero.
  final width = luma.width;
  final data = luma.data;
  var sum = 0.0;
  var sumSquares = 0.0;
  var count = 0;

  for (var y = 1; y < luma.height - 1; y++) {
    var i = y * width + 1;
    for (var x = 1; x < width - 1; x++, i++) {
      final response =
          (data[i - 1] + data[i + 1] + data[i - width] + data[i + width]) -
              4 * data[i];
      final value = response.toDouble();
      sum += value;
      sumSquares += value * value;
      count++;
    }
  }

  final mean = sum / count;
  final variance = math.max(0.0, sumSquares / count - mean * mean);

  final QualityVerdict verdict;
  final String message;
  if (variance < QualityThresholds.blurFailBelow) {
    verdict = QualityVerdict.fail;
    message =
        'Photo is out of focus (sharpness ${variance.toStringAsFixed(0)}, '
        'needs ${QualityThresholds.blurFailBelow.toStringAsFixed(0)} or more). '
        'Hold the phone steady, tap the package to refocus, and shoot again.';
  } else if (variance < QualityThresholds.blurWarnBelow) {
    verdict = QualityVerdict.warn;
    message = 'Slightly soft (sharpness ${variance.toStringAsFixed(0)}). Fine '
        'print may not survive review — a steadier shot would be better.';
  } else {
    verdict = QualityVerdict.pass;
    message = 'Sharp (${variance.toStringAsFixed(0)}).';
  }

  return QualityCheckResult(
    checkId: checkId,
    label: label,
    score: (variance / QualityThresholds.blurScoreCeiling).clamp(0.0, 1.0),
    rawValue: variance,
    unit: 'laplacian-variance',
    verdict: verdict,
    message: message,
    diagnostics: <String, double>{
      'laplacianMean': mean,
      'analysedPixels': count.toDouble(),
    },
  );
}

// ---------------------------------------------------------------------------
// Brightness — mean luminance plus shadow/highlight clipping.
// ---------------------------------------------------------------------------

QualityCheckResult _brightnessCheck(_LumaPlane luma) {
  const label = 'Exposure';
  const checkId = 'brightness';

  var total = 0;
  var shadowClipped = 0;
  var highlightClipped = 0;
  for (var i = 0; i < luma.length; i++) {
    final value = luma.data[i];
    total += value;
    if (value <= QualityThresholds.shadowClipLevel) shadowClipped++;
    if (value >= QualityThresholds.highlightClipLevel) highlightClipped++;
  }

  final meanLuminance = total / luma.length;
  final shadowFraction = shadowClipped / luma.length;
  final highlightFraction = highlightClipped / luma.length;

  final QualityVerdict verdict;
  final String message;
  if (meanLuminance < QualityThresholds.brightnessFailBelow) {
    verdict = QualityVerdict.fail;
    message =
        'Too dark to read (brightness ${meanLuminance.toStringAsFixed(0)} of '
        '255). Move the package into better light or step out of your own '
        'shadow.';
  } else if (meanLuminance > QualityThresholds.brightnessFailAbove) {
    verdict = QualityVerdict.fail;
    message =
        'Overexposed (brightness ${meanLuminance.toStringAsFixed(0)} of 255). '
        'Text is washed out — move away from the direct light source.';
  } else if (meanLuminance < QualityThresholds.brightnessWarnBelow) {
    verdict = QualityVerdict.warn;
    message = 'Dim (brightness ${meanLuminance.toStringAsFixed(0)} of 255). '
        'Readable, but more light would help.';
  } else if (meanLuminance > QualityThresholds.brightnessWarnAbove) {
    verdict = QualityVerdict.warn;
    message =
        'Bright (brightness ${meanLuminance.toStringAsFixed(0)} of 255). Check '
        'the declaration panel is not washed out.';
  } else if (shadowFraction > QualityThresholds.clippedFractionWarn) {
    verdict = QualityVerdict.warn;
    message = '${(shadowFraction * 100).toStringAsFixed(0)}% of the frame is '
        'crushed to black. Detail in the dark areas is gone.';
  } else if (highlightFraction > QualityThresholds.clippedFractionWarn) {
    verdict = QualityVerdict.warn;
    message = '${(highlightFraction * 100).toStringAsFixed(0)}% of the frame is '
        'blown out to white. Detail in the bright areas is gone.';
  } else {
    verdict = QualityVerdict.pass;
    message = 'Well exposed (${meanLuminance.toStringAsFixed(0)} of 255).';
  }

  // Distance from mid-grey, normalised so 128 scores 1.0 and 0 or 255 score 0.
  final score = 1.0 - (meanLuminance - 128.0).abs() / 128.0;

  return QualityCheckResult(
    checkId: checkId,
    label: label,
    score: score.clamp(0.0, 1.0),
    rawValue: meanLuminance,
    unit: 'mean-luminance-0-255',
    verdict: verdict,
    message: message,
    diagnostics: <String, double>{
      'shadowClippedFraction': shadowFraction,
      'highlightClippedFraction': highlightFraction,
    },
  );
}

// ---------------------------------------------------------------------------
// Glare — near-saturated specular highlights.
// ---------------------------------------------------------------------------

/// KNOWN WEAK POINT — see [QualityThresholds.glareLuminanceLevel].
///
/// This measures how much of the frame sits at or near sensor saturation, and
/// how much of that falls in one contiguous blob. That catches a genuine
/// specular reflection off a laminated or shrink-wrapped package.
///
/// It also catches a plain white label, which is the problem. At the pixel
/// level a well-lit white background and a blown-out reflection are the same
/// thing — both are large contiguous regions pinned near 255 — and no choice
/// of threshold on these two numbers separates them.
///
/// The distinguishing signal is most likely the boundary: a specular hotspot
/// falls off over a few pixels with a steep gradient, whereas a printed white
/// panel is bounded by the package edge or by print, giving a different
/// gradient profile. That boundary gradient is measured here and stored as a
/// diagnostic, but it is deliberately NOT used to decide the verdict, because
/// the cutoff separating the two cases cannot be chosen honestly without real
/// photographs of both. When calibration images exist, the fix belongs here.
QualityCheckResult _glareCheck(_LumaPlane luma) {
  const label = 'Glare';
  const checkId = 'glare';

  final width = luma.width;
  final height = luma.height;
  final data = luma.data;
  final total = luma.length;

  final bright = Uint8List(total);
  var brightCount = 0;
  for (var i = 0; i < total; i++) {
    if (data[i] >= QualityThresholds.glareLuminanceLevel) {
      bright[i] = 1;
      brightCount++;
    }
  }

  final brightFraction = brightCount / total;

  // Largest 4-connected component of the bright mask, found with an explicit
  // stack. Recursion would overflow the isolate stack on a large highlight.
  var largestBlob = 0;
  if (brightCount > 0) {
    final visited = Uint8List(total);
    final stack = Int32List(total);
    for (var seed = 0; seed < total; seed++) {
      if (bright[seed] == 0 || visited[seed] == 1) continue;
      var stackSize = 0;
      stack[stackSize++] = seed;
      visited[seed] = 1;
      var blobSize = 0;
      while (stackSize > 0) {
        final index = stack[--stackSize];
        blobSize++;
        stackSize = _pushBrightNeighbours(
          index: index,
          width: width,
          height: height,
          bright: bright,
          visited: visited,
          stack: stack,
          stackSize: stackSize,
        );
      }
      if (blobSize > largestBlob) largestBlob = blobSize;
    }
  }

  final blobFraction = largestBlob / total;
  final boundaryGradient = _meanBoundaryGradient(luma, bright);

  final QualityVerdict verdict;
  final String message;
  if (blobFraction >= QualityThresholds.glareBlobFailFraction) {
    verdict = QualityVerdict.fail;
    message =
        'A bright hotspot covers ${(blobFraction * 100).toStringAsFixed(1)}% of '
        'the frame in a single patch, which usually means a reflection sitting '
        'over the label. Tilt the package or move the light, then retake. '
        'If this surface is a plain white panel with no reflection, this check '
        'is known to over-report — retake once more, or proceed and the '
        'surface will be flagged for manual review.';
  } else if (brightFraction >= QualityThresholds.glareTotalWarnFraction) {
    verdict = QualityVerdict.warn;
    message = '${(brightFraction * 100).toStringAsFixed(1)}% of the frame is '
        'near-white. Check no reflection is sitting over the declaration.';
  } else {
    verdict = QualityVerdict.pass;
    message = 'No significant glare '
        '(${(brightFraction * 100).toStringAsFixed(1)}% near-white).';
  }

  return QualityCheckResult(
    checkId: checkId,
    label: label,
    score: (1.0 - blobFraction / QualityThresholds.glareBlobFailFraction)
        .clamp(0.0, 1.0),
    rawValue: blobFraction,
    unit: 'largest-specular-blob-fraction',
    verdict: verdict,
    message: message,
    diagnostics: <String, double>{
      'brightFraction': brightFraction,
      'largestBlobFraction': blobFraction,
      'largestBlobPixels': largestBlob.toDouble(),
      // Retained for recalibration; see the doc comment above.
      'boundaryGradientMean': boundaryGradient,
      'boundaryGradientAboveFloor': boundaryGradient >=
              QualityThresholds.glareEdgeGradientDiagnosticFloor
          ? 1.0
          : 0.0,
    },
  );
}

/// Pushes every unvisited 4-neighbour of [index] that is part of the bright
/// mask, returning the new stack size.
int _pushBrightNeighbours({
  required int index,
  required int width,
  required int height,
  required Uint8List bright,
  required Uint8List visited,
  required Int32List stack,
  required int stackSize,
}) {
  final x = index % width;
  final y = index ~/ width;
  var size = stackSize;

  if (x > 0) {
    final n = index - 1;
    if (bright[n] == 1 && visited[n] == 0) {
      visited[n] = 1;
      stack[size++] = n;
    }
  }
  if (x < width - 1) {
    final n = index + 1;
    if (bright[n] == 1 && visited[n] == 0) {
      visited[n] = 1;
      stack[size++] = n;
    }
  }
  if (y > 0) {
    final n = index - width;
    if (bright[n] == 1 && visited[n] == 0) {
      visited[n] = 1;
      stack[size++] = n;
    }
  }
  if (y < height - 1) {
    final n = index + width;
    if (bright[n] == 1 && visited[n] == 0) {
      visited[n] = 1;
      stack[size++] = n;
    }
  }
  return size;
}

/// Mean luminance gradient along the boundary of the near-white region.
/// Diagnostic only — recorded so the glare rule can be re-fitted against real
/// package photographs later.
double _meanBoundaryGradient(_LumaPlane luma, Uint8List bright) {
  final width = luma.width;
  final height = luma.height;
  final data = luma.data;
  var sum = 0.0;
  var count = 0;

  for (var y = 1; y < height - 1; y++) {
    var i = y * width + 1;
    for (var x = 1; x < width - 1; x++, i++) {
      if (bright[i] == 0) continue;
      final isBoundary = bright[i - 1] == 0 ||
          bright[i + 1] == 0 ||
          bright[i - width] == 0 ||
          bright[i + width] == 0;
      if (!isBoundary) continue;
      final dx = (data[i + 1] - data[i - 1]).abs();
      final dy = (data[i + width] - data[i - width]).abs();
      sum += math.sqrt((dx * dx + dy * dy).toDouble());
      count++;
    }
  }
  return count == 0 ? 0.0 : sum / count;
}

// ---------------------------------------------------------------------------
// Cropping — where the package silhouette sits in the frame.
// ---------------------------------------------------------------------------

/// Locates the package by edge energy rather than by colour, because packaging
/// has no predictable colour but always has print and a boundary against its
/// background. Sobel magnitude is thresholded, then the bounding box is
/// trimmed inward from each side until a small fraction of the total edge
/// energy has been discarded, which stops sensor noise or a stray background
/// object from stretching the box to the full frame.
///
/// This is a heuristic with a predictable failure mode: a busy background — a
/// shelf of other packages — contributes edge energy of its own and inflates
/// the box toward the frame borders, reading as "cut off". The alignment
/// overlay in the capture UI exists partly to push inspectors toward a clean
/// background so this stays reliable.
QualityCheckResult _croppingCheck(_LumaPlane luma) {
  const label = 'Framing';
  const checkId = 'cropping';

  final width = luma.width;
  final height = luma.height;
  if (width < 8 || height < 8) {
    return QualityCheckResult.unavailable(
      checkId: checkId,
      label: label,
      message: 'Image too small to assess framing.',
    );
  }

  final data = luma.data;
  final magnitude = Uint16List(width * height);
  var sum = 0.0;
  var sumSquares = 0.0;
  var count = 0;

  for (var y = 1; y < height - 1; y++) {
    var i = y * width + 1;
    for (var x = 1; x < width - 1; x++, i++) {
      final tl = data[i - width - 1];
      final t = data[i - width];
      final tr = data[i - width + 1];
      final l = data[i - 1];
      final r = data[i + 1];
      final bl = data[i + width - 1];
      final b = data[i + width];
      final br = data[i + width + 1];

      final gx = (tr + 2 * r + br) - (tl + 2 * l + bl);
      final gy = (bl + 2 * b + br) - (tl + 2 * t + tr);
      final mag = math.sqrt((gx * gx + gy * gy).toDouble());
      magnitude[i] = mag > 65535 ? 65535 : mag.round();
      sum += mag;
      sumSquares += mag * mag;
      count++;
    }
  }

  final mean = sum / count;
  final variance = math.max(0.0, sumSquares / count - mean * mean);
  final threshold = mean + math.sqrt(variance);

  final columnEnergy = Int32List(width);
  final rowEnergy = Int32List(height);
  var totalEnergy = 0;
  for (var y = 1; y < height - 1; y++) {
    var i = y * width + 1;
    for (var x = 1; x < width - 1; x++, i++) {
      if (magnitude[i] < threshold) continue;
      columnEnergy[x]++;
      rowEnergy[y]++;
      totalEnergy++;
    }
  }

  if (totalEnergy == 0) {
    return QualityCheckResult.unavailable(
      checkId: checkId,
      label: label,
      message: 'No package outline could be found — the frame has almost no '
          'detail. Check the lens is not covered, then retake.',
    );
  }

  final trim = (totalEnergy * QualityThresholds.cropEnergyTrimFraction).round();
  final left = _trimFromStart(columnEnergy, trim);
  final right = _trimFromEnd(columnEnergy, trim);
  final top = _trimFromStart(rowEnergy, trim);
  final bottom = _trimFromEnd(rowEnergy, trim);

  final boxWidth = math.max(1, right - left + 1);
  final boxHeight = math.max(1, bottom - top + 1);
  final fillFraction = (boxWidth * boxHeight) / (width * height);

  final horizontalMargin =
      math.max(1, (width * QualityThresholds.cropEdgeMarginFraction).round());
  final verticalMargin =
      math.max(1, (height * QualityThresholds.cropEdgeMarginFraction).round());

  final touching = <String>[
    if (left <= horizontalMargin) 'left',
    if (right >= width - 1 - horizontalMargin) 'right',
    if (top <= verticalMargin) 'top',
    if (bottom >= height - 1 - verticalMargin) 'bottom',
  ];

  final QualityVerdict verdict;
  final String message;
  if (touching.isNotEmpty) {
    verdict = QualityVerdict.fail;
    message = 'The package runs off the ${_joinSides(touching)} of the frame, '
        'so part of the surface is missing. Step back and fit the whole panel '
        'inside the guide before shooting.';
  } else if (fillFraction < QualityThresholds.cropFillFailBelow) {
    verdict = QualityVerdict.fail;
    message =
        'The package fills only ${(fillFraction * 100).toStringAsFixed(0)}% of '
        'the frame, too small for the declaration to be legible. Move closer '
        'until it fills the guide.';
  } else if (fillFraction < QualityThresholds.cropFillWarnBelow) {
    verdict = QualityVerdict.warn;
    message =
        'The package fills ${(fillFraction * 100).toStringAsFixed(0)}% of the '
        'frame. Moving closer would make the small print easier to read.';
  } else {
    verdict = QualityVerdict.pass;
    message = 'Well framed, filling '
        '${(fillFraction * 100).toStringAsFixed(0)}% of the frame.';
  }

  return QualityCheckResult(
    checkId: checkId,
    label: label,
    score: touching.isNotEmpty
        ? 0.0
        : (fillFraction / QualityThresholds.cropFillWarnBelow).clamp(0.0, 1.0),
    rawValue: fillFraction,
    unit: 'silhouette-frame-fill-fraction',
    verdict: verdict,
    message: message,
    diagnostics: <String, double>{
      'boxLeft': left.toDouble(),
      'boxTop': top.toDouble(),
      'boxRight': right.toDouble(),
      'boxBottom': bottom.toDouble(),
      'edgeThreshold': threshold,
      'edgePixelCount': totalEnergy.toDouble(),
      'touchingSideCount': touching.length.toDouble(),
    },
  );
}

int _trimFromStart(Int32List energy, int budget) {
  var accumulated = 0;
  for (var i = 0; i < energy.length; i++) {
    accumulated += energy[i];
    if (accumulated > budget) return i;
  }
  return 0;
}

int _trimFromEnd(Int32List energy, int budget) {
  var accumulated = 0;
  for (var i = energy.length - 1; i >= 0; i--) {
    accumulated += energy[i];
    if (accumulated > budget) return i;
  }
  return energy.length - 1;
}

String _joinSides(List<String> sides) {
  if (sides.length == 1) return '${sides.single} edge';
  if (sides.length == 2) return '${sides[0]} and ${sides[1]} edges';
  return '${sides.sublist(0, sides.length - 1).join(', ')} and '
      '${sides.last} edges';
}
