/// Every tunable constant used by the quality pipeline, in one place.
///
/// These are starting values, not calibrated ones. They were chosen to be
/// plausible for handheld shots of retail packaging and they will need to be
/// re-fitted against a real corpus of field captures before anyone treats a
/// `fail` here as authoritative. Each capture stores its raw measurements
/// alongside its verdict precisely so that re-fitting is possible after the
/// fact, without re-shooting anything.
///
/// A threshold change is a change to what counts as acceptable evidence, so
/// bump [QualityThresholds.pipelineVersion] whenever one moves. The version
/// travels with every stored report; without it, scores gathered under
/// different rules would be silently incomparable.
class QualityThresholds {
  const QualityThresholds._();

  /// Identifies the rule set that produced a given set of scores.
  static const String pipelineVersion = 'qc-1.0.0';

  /// Longest edge of the buffer the pixel checks run on. Every pixel-domain
  /// threshold below is expressed in terms of this size: Laplacian variance
  /// in particular scales with resolution, so changing this value invalidates
  /// the blur thresholds.
  static const int analysisMaxDimension = 800;

  // --- Blur: variance of the Laplacian over the luminance plane. ----------
  // Higher variance means more high-frequency detail, i.e. sharper.
  static const double blurFailBelow = 45.0;
  static const double blurWarnBelow = 110.0;

  /// Variance treated as "definitively sharp" when normalising to 0..1.
  static const double blurScoreCeiling = 300.0;

  // --- Brightness: mean luminance, 0..255. -------------------------------
  static const double brightnessFailBelow = 45.0;
  static const double brightnessWarnBelow = 78.0;
  static const double brightnessWarnAbove = 188.0;
  static const double brightnessFailAbove = 212.0;

  /// Luminance at or below this counts as crushed shadow.
  static const int shadowClipLevel = 16;

  /// Luminance at or above this counts as blown highlight.
  static const int highlightClipLevel = 239;

  /// Fraction of the frame that may be clipped before it is called out.
  static const double clippedFractionWarn = 0.08;

  // --- Glare: near-saturated specular highlights. ------------------------
  // KNOWN WEAK POINT. See GlareCheck in pixel_analysis.dart. A plain white
  // label is, at the pixel level, indistinguishable from a blown-out
  // reflection by these measures alone, so white packaging produces false
  // positives at these thresholds. Do not tune these by guesswork; they need
  // real package photographs, including known-good white-label shots.
  static const int glareLuminanceLevel = 250;
  static const double glareBlobFailFraction = 0.020;
  static const double glareTotalWarnFraction = 0.015;

  /// A specular hotspot has a hard edge; a white label does not. This is the
  /// mean luminance gradient measured on the boundary of the bright region,
  /// retained as a diagnostic only. It is NOT currently used to decide the
  /// verdict, because the cutoff that would separate the two cases is exactly
  /// what the field photographs are needed to establish.
  static const double glareEdgeGradientDiagnosticFloor = 12.0;

  // --- Cropping: package silhouette position and size in frame. ----------
  /// Distance from the frame border, as a fraction of the frame dimension,
  /// within which the silhouette counts as touching the edge.
  static const double cropEdgeMarginFraction = 0.012;

  /// Below this fill fraction the package is too small to read.
  static const double cropFillFailBelow = 0.20;
  static const double cropFillWarnBelow = 0.36;

  /// Fraction of total edge energy trimmed from each side when locating the
  /// silhouette bounding box. Makes the box robust to sensor noise and to a
  /// busy background.
  static const double cropEnergyTrimFraction = 0.02;

  // --- Text readability: fast OCR confidence pass. -----------------------
  /// Mean line confidence, when the platform reports it.
  static const double ocrConfidenceFailBelow = 0.50;
  static const double ocrConfidenceWarnBelow = 0.70;

  /// Fallback geometric signal, used when confidence is not reported (the
  /// common case on Android). A line is "legible" when its bounding box is at
  /// least this tall relative to the full image height.
  static const double ocrLegibleLineHeightFraction = 0.008;

  static const double ocrLegibleFractionFailBelow = 0.30;
  static const double ocrLegibleFractionWarnBelow = 0.60;

  /// Below this many detected lines the surface is treated as having no
  /// readable declaration text at all.
  static const int ocrMinimumLines = 3;

  // --- Retake policy. ----------------------------------------------------
  /// Failed attempts allowed before the inspector may proceed with the
  /// surface flagged for manual review.
  static const int maxFailedAttempts = 3;
}
