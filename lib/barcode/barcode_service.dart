import 'package:mobile_scanner/mobile_scanner.dart';

import '../core/ids.dart';
import '../models/barcode_scan.dart';

/// Live barcode reading, and the rules around when to stop asking for it.
///
/// Worth restating at the top of this file, because it is the single easiest
/// thing to get wrong in a compliance app: a barcode identifies the product.
/// It is not evidence about the product's labelling. Everything here produces
/// a [BarcodeScan], which is filed under product identification and is
/// structurally incapable of counting toward surface coverage or toward any
/// determination that a declaration is present.
class BarcodeScanConfig {
  const BarcodeScanConfig._();

  /// The symbologies worth looking for on retail packaging. Restricting the
  /// set makes detection faster and cuts false reads off stray printed
  /// matter in the frame.
  static const List<BarcodeFormat> formats = <BarcodeFormat>[
    BarcodeFormat.ean13,
    BarcodeFormat.upcA,
    BarcodeFormat.qrCode,
    BarcodeFormat.code128,
  ];

  /// Rejected detections — wrong symbology, failed check digit — before the
  /// manual entry route is offered.
  static const int failedAttemptsBeforeManualEntry = 3;

  /// Or simply this long spent scanning without a usable read. A damaged or
  /// obscured barcode produces no detections at all, so an attempt counter
  /// alone would leave the inspector stuck with nothing to press.
  static const Duration scanTimeBeforeManualEntry = Duration(seconds: 15);
}

BarcodeSymbology symbologyFromFormat(BarcodeFormat format) => switch (format) {
      BarcodeFormat.ean13 => BarcodeSymbology.ean13,
      BarcodeFormat.upcA => BarcodeSymbology.upcA,
      BarcodeFormat.qrCode => BarcodeSymbology.qr,
      BarcodeFormat.code128 => BarcodeSymbology.code128,
      _ => BarcodeSymbology.other,
    };

/// Builds a scan record from a live detection.
///
/// A failed check digit does not reject the read. The digits seen are recorded
/// with `checkDigitValid: false` so a reviewer can see both the value and the
/// doubt, which is more useful than discarding it and leaving the product
/// unidentified.
BarcodeScan scanFromDetection({
  required Barcode barcode,
  required String inspectionId,
  required String productSessionId,
  required int failedScanAttempts,
}) {
  final symbology = symbologyFromFormat(barcode.format);
  final value = barcode.rawValue ?? barcode.displayValue ?? '';
  return BarcodeScan(
    scanId: Ids.scan(),
    inspectionId: inspectionId,
    productSessionId: productSessionId,
    rawValue: value,
    symbology: symbology,
    entryMode: BarcodeEntryMode.liveScan,
    scannedAtUtc: DateTime.now().toUtc(),
    checkDigitValid: symbology.hasGtinCheckDigit && isValidGtin(value),
    failedScanAttempts: failedScanAttempts,
  );
}

/// Builds a scan record from a GTIN the inspector keyed in.
BarcodeScan scanFromManualEntry({
  required String gtin,
  required String inspectionId,
  required String productSessionId,
  required int failedScanAttempts,
}) {
  final value = gtin.trim();
  final symbology = switch (value.length) {
    13 => BarcodeSymbology.ean13,
    12 => BarcodeSymbology.upcA,
    _ => BarcodeSymbology.other,
  };
  return BarcodeScan(
    scanId: Ids.scan(),
    inspectionId: inspectionId,
    productSessionId: productSessionId,
    rawValue: value,
    symbology: symbology,
    entryMode: BarcodeEntryMode.manualEntry,
    scannedAtUtc: DateTime.now().toUtc(),
    checkDigitValid: isValidGtin(value),
    failedScanAttempts: failedScanAttempts,
  );
}
