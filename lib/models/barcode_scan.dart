import '../core/json_store.dart';

enum BarcodeSymbology { ean13, upcA, qr, code128, other }

extension BarcodeSymbologyX on BarcodeSymbology {
  String get label => switch (this) {
        BarcodeSymbology.ean13 => 'EAN-13',
        BarcodeSymbology.upcA => 'UPC-A',
        BarcodeSymbology.qr => 'QR',
        BarcodeSymbology.code128 => 'Code 128',
        BarcodeSymbology.other => 'Other symbology',
      };

  /// Only the retail GTIN symbologies carry a modulo-10 check digit that this
  /// module knows how to verify.
  bool get hasGtinCheckDigit =>
      this == BarcodeSymbology.ean13 || this == BarcodeSymbology.upcA;

  static BarcodeSymbology parse(Object? value) =>
      BarcodeSymbology.values.firstWhere(
        (v) => v.name == value,
        orElse: () => BarcodeSymbology.other,
      );
}

enum BarcodeEntryMode { liveScan, manualEntry }

extension BarcodeEntryModeX on BarcodeEntryMode {
  String get label => switch (this) {
        BarcodeEntryMode.liveScan => 'Scanned on device',
        BarcodeEntryMode.manualEntry => 'Keyed in by inspector',
      };

  static BarcodeEntryMode parse(Object? value) =>
      BarcodeEntryMode.values.firstWhere(
        (v) => v.name == value,
        orElse: () => BarcodeEntryMode.liveScan,
      );
}

/// A barcode read from a package.
///
/// IMPORTANT, and the reason this class carries so much commentary: a barcode
/// identifies a product. It is not evidence of compliance with anything.
///
/// A successful scan means "this package bears a machine-readable code that
/// resolves to a GTIN". It does not mean a declaration is present, legible,
/// accurate, or lawful. Those are determined from the photographs of the
/// package faces, by a reviewer. Nothing in this module may record a scan as a
/// compliance result, contribute a scan to surface coverage, or let a scan
/// stand in for a capture — and [evidenceClass] is written into every
/// serialised scan so the same distinction survives on the server.
class BarcodeScan {
  const BarcodeScan({
    required this.scanId,
    required this.inspectionId,
    required this.productSessionId,
    required this.rawValue,
    required this.symbology,
    required this.entryMode,
    required this.scannedAtUtc,
    required this.checkDigitValid,
    this.failedScanAttempts = 0,
  });

  /// The classification that travels with the record. Product identity —
  /// never `compliance_evidence`.
  static const String evidenceClass = 'product_identification';

  /// Shown wherever a scan result is displayed, so the distinction is visible
  /// to the inspector too, not only in the data.
  static const String disclaimer =
      'Identifies the product only. This is not evidence that any declaration '
      'is present or compliant.';

  final String scanId;
  final String inspectionId;
  final String productSessionId;

  final String rawValue;
  final BarcodeSymbology symbology;
  final BarcodeEntryMode entryMode;
  final DateTime scannedAtUtc;

  /// Result of the modulo-10 check on GTIN symbologies. `false` on a keyed
  /// entry means the inspector likely mistyped a digit; it does not make the
  /// package non-compliant.
  final bool checkDigitValid;

  /// How many live scan attempts failed before this record was produced.
  /// Drives the offer of manual entry.
  final int failedScanAttempts;

  bool get isGtin => symbology.hasGtinCheckDigit;

  Map<String, dynamic> toJson() => <String, dynamic>{
        'scanId': scanId,
        'inspectionId': inspectionId,
        'productSessionId': productSessionId,
        'rawValue': rawValue,
        'symbology': symbology.name,
        'entryMode': entryMode.name,
        'scannedAtUtc': encodeTime(scannedAtUtc),
        'checkDigitValid': checkDigitValid,
        'failedScanAttempts': failedScanAttempts,
        // Consumed by the server to keep identification out of the compliance
        // determination. See the class doc.
        'evidenceClass': evidenceClass,
        'isComplianceEvidence': false,
      };

  factory BarcodeScan.fromJson(Map<String, dynamic> json) => BarcodeScan(
        scanId: json['scanId'] as String? ?? '',
        inspectionId: json['inspectionId'] as String? ?? '',
        productSessionId: json['productSessionId'] as String? ?? '',
        rawValue: json['rawValue'] as String? ?? '',
        symbology: BarcodeSymbologyX.parse(json['symbology']),
        entryMode: BarcodeEntryModeX.parse(json['entryMode']),
        scannedAtUtc: decodeTime(json['scannedAtUtc']),
        checkDigitValid: json['checkDigitValid'] as bool? ?? false,
        failedScanAttempts: (json['failedScanAttempts'] as num?)?.toInt() ?? 0,
      );
}

/// GTIN modulo-10 check digit validation, used for both scanned and keyed
/// values. Accepts the 8, 12, 13 and 14 digit forms.
bool isValidGtin(String value) {
  final digits = value.trim();
  if (!RegExp(r'^\d+$').hasMatch(digits)) return false;
  if (![8, 12, 13, 14].contains(digits.length)) return false;

  var sum = 0;
  // Weights alternate 3 and 1 running right to left from the digit before the
  // check digit.
  for (var i = digits.length - 2; i >= 0; i--) {
    final digit = digits.codeUnitAt(i) - 0x30;
    final positionFromRight = digits.length - 2 - i;
    sum += digit * (positionFromRight.isEven ? 3 : 1);
  }
  final expected = (10 - (sum % 10)) % 10;
  return expected == digits.codeUnitAt(digits.length - 1) - 0x30;
}
