import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

import '../../barcode/barcode_service.dart';
import '../../models/barcode_scan.dart';
import '../../state/inspection_controller.dart';

/// Live barcode scan for the current package, with a manual GTIN route once
/// scanning has clearly failed.
///
/// The banner at the top is not boilerplate. An inspector who has just watched
/// a green tick appear will reasonably read it as "this package checks out",
/// and that is precisely the wrong conclusion — the scan says which product
/// this is and nothing more. The screen says so before, during and after the
/// scan.
class BarcodeScreen extends StatefulWidget {
  const BarcodeScreen({super.key});

  @override
  State<BarcodeScreen> createState() => _BarcodeScreenState();
}

class _BarcodeScreenState extends State<BarcodeScreen> {
  final MobileScannerController _scanner = MobileScannerController(
    formats: BarcodeScanConfig.formats,
    detectionSpeed: DetectionSpeed.normal,
    facing: CameraFacing.back,
  );

  Timer? _fallbackTimer;
  int _failedAttempts = 0;
  bool _manualAvailable = false;
  bool _handling = false;

  @override
  void initState() {
    super.initState();
    _fallbackTimer = Timer(
      BarcodeScanConfig.scanTimeBeforeManualEntry,
      () {
        if (mounted) setState(() => _manualAvailable = true);
      },
    );
  }

  @override
  void dispose() {
    _fallbackTimer?.cancel();
    unawaited(_scanner.dispose());
    super.dispose();
  }

  void _noteFailedAttempt() {
    _failedAttempts += 1;
    if (_failedAttempts >= BarcodeScanConfig.failedAttemptsBeforeManualEntry) {
      _manualAvailable = true;
    }
    setState(() {});
  }

  Future<void> _onDetect(BarcodeCapture capture) async {
    if (_handling || capture.barcodes.isEmpty) return;

    final barcode = capture.barcodes.first;
    final value = barcode.rawValue ?? barcode.displayValue;
    if (value == null || value.trim().isEmpty) {
      _noteFailedAttempt();
      return;
    }

    _handling = true;
    await _scanner.stop();
    if (!mounted) return;

    final controller = InspectionScope.read(context);
    final product = controller.product;
    if (product == null) {
      _handling = false;
      return;
    }

    final scan = scanFromDetection(
      barcode: barcode,
      inspectionId: product.inspectionId,
      productSessionId: product.productSessionId,
      failedScanAttempts: _failedAttempts,
    );

    if (!mounted) return;
    final confirmed = await _confirm(scan);
    if (!mounted) return;

    if (confirmed == true) {
      await controller.setIdentification(scan);
      if (!mounted) return;
      Navigator.of(context).pop(true);
      return;
    }

    // The inspector rejected the read — wrong package, stray code in frame.
    _handling = false;
    _noteFailedAttempt();
    await _scanner.start();
  }

  Future<bool?> _confirm(BarcodeScan scan) {
    return showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Use this code?'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            SelectableText(
              scan.rawValue,
              style: const TextStyle(
                fontSize: 20,
                fontFeatures: <FontFeature>[FontFeature.tabularFigures()],
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 6),
            Text(scan.symbology.label),
            if (scan.isGtin && !scan.checkDigitValid) ...<Widget>[
              const SizedBox(height: 10),
              const Text(
                'The check digit does not match. The value is still recorded '
                'as read, flagged, so a reviewer can see the doubt.',
                style: TextStyle(fontSize: 12.5, height: 1.35),
              ),
            ],
            const SizedBox(height: 12),
            Text(
              BarcodeScan.disclaimer,
              style: TextStyle(
                fontSize: 12,
                height: 1.35,
                color: Theme.of(context).colorScheme.error,
              ),
            ),
          ],
        ),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('Scan again'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('Use code'),
          ),
        ],
      ),
    );
  }

  Future<void> _enterManually() async {
    await _scanner.stop();
    if (!mounted) return;

    final gtin = await showDialog<String>(
      context: context,
      builder: (context) => const _ManualGtinDialog(),
    );
    if (!mounted) return;

    if (gtin == null) {
      await _scanner.start();
      return;
    }

    final controller = InspectionScope.read(context);
    final product = controller.product;
    if (product == null) return;

    final scan = scanFromManualEntry(
      gtin: gtin,
      inspectionId: product.inspectionId,
      productSessionId: product.productSessionId,
      failedScanAttempts: _failedAttempts,
    );
    await controller.setIdentification(scan);
    if (!mounted) return;
    Navigator.of(context).pop(true);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(
        backgroundColor: Colors.black,
        foregroundColor: Colors.white,
        title: const Text('Identify product'),
      ),
      body: Column(
        children: <Widget>[
          Container(
            width: double.infinity,
            color: const Color(0xFF23303B),
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
            child: const Text(
              'A barcode identifies the product. It is not evidence that any '
              'declaration is present or compliant — that comes from the '
              'package photographs.',
              style: TextStyle(color: Color(0xFFCFE3F2), fontSize: 12.5, height: 1.35),
            ),
          ),
          Expanded(
            child: Stack(
              fit: StackFit.expand,
              children: <Widget>[
                MobileScanner(
                  controller: _scanner,
                  onDetect: _onDetect,
                  errorBuilder: (context, error) => Center(
                    child: Padding(
                      padding: const EdgeInsets.all(28),
                      child: Column(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: <Widget>[
                          const Icon(Icons.qr_code_scanner,
                              size: 44, color: Colors.white54),
                          const SizedBox(height: 14),
                          Text(
                            'Scanner unavailable: ${error.errorCode.name}',
                            textAlign: TextAlign.center,
                            style: const TextStyle(color: Colors.white70),
                          ),
                          const SizedBox(height: 14),
                          FilledButton.tonal(
                            onPressed: _enterManually,
                            child: const Text('Enter GTIN by hand'),
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
                IgnorePointer(
                  child: Center(
                    child: Container(
                      width: 270,
                      height: 170,
                      decoration: BoxDecoration(
                        border: Border.all(color: Colors.white70, width: 2),
                        borderRadius: BorderRadius.circular(12),
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
          Container(
            width: double.infinity,
            color: Colors.black,
            padding: const EdgeInsets.fromLTRB(16, 14, 16, 20),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: <Widget>[
                Text(
                  _failedAttempts == 0
                      ? 'Point the camera at the barcode.'
                      : '$_failedAttempts unusable read'
                          '${_failedAttempts == 1 ? '' : 's'} so far.',
                  style: const TextStyle(color: Colors.white60, fontSize: 12.5),
                ),
                const SizedBox(height: 12),
                SizedBox(
                  width: double.infinity,
                  child: _manualAvailable
                      ? FilledButton.tonalIcon(
                          onPressed: _enterManually,
                          icon: const Icon(Icons.keyboard),
                          label: const Text('Enter GTIN by hand'),
                        )
                      : OutlinedButton(
                          onPressed: () => Navigator.of(context).pop(false),
                          style: OutlinedButton.styleFrom(
                            foregroundColor: Colors.white70,
                          ),
                          child: const Text('Skip identification'),
                        ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _ManualGtinDialog extends StatefulWidget {
  const _ManualGtinDialog();

  @override
  State<_ManualGtinDialog> createState() => _ManualGtinDialogState();
}

class _ManualGtinDialogState extends State<_ManualGtinDialog> {
  final TextEditingController _field = TextEditingController();

  @override
  void dispose() {
    _field.dispose();
    super.dispose();
  }

  String get _value => _field.text.trim();
  bool get _hasValue => _value.isNotEmpty;
  bool get _checkDigitOk => isValidGtin(_value);

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Enter GTIN'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          TextField(
            controller: _field,
            autofocus: true,
            keyboardType: TextInputType.number,
            inputFormatters: <TextInputFormatter>[
              FilteringTextInputFormatter.digitsOnly,
              LengthLimitingTextInputFormatter(14),
            ],
            onChanged: (_) => setState(() {}),
            decoration: const InputDecoration(
              labelText: 'Barcode number',
              hintText: '8 to 14 digits, as printed',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 10),
          if (_hasValue)
            Text(
              _checkDigitOk
                  ? 'Check digit is valid.'
                  : 'Check digit does not match — recheck the digits. You can '
                      'still save it; it will be stored flagged.',
              style: TextStyle(
                fontSize: 12.5,
                height: 1.3,
                color: _checkDigitOk
                    ? const Color(0xFF1B7F4B)
                    : Theme.of(context).colorScheme.error,
              ),
            ),
          const SizedBox(height: 10),
          Text(
            BarcodeScan.disclaimer,
            style: const TextStyle(fontSize: 11.5, height: 1.3),
          ),
        ],
      ),
      actions: <Widget>[
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Cancel'),
        ),
        FilledButton(
          onPressed: _hasValue ? () => Navigator.of(context).pop(_value) : null,
          child: const Text('Save'),
        ),
      ],
    );
  }
}
