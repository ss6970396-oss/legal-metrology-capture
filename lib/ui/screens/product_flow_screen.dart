import 'dart:io';

import 'package:flutter/material.dart';

import '../../models/barcode_scan.dart';
import '../../models/surface_step.dart';
import '../../state/inspection_controller.dart';
import '../widgets/coverage_indicator.dart';
import '../widgets/quality_badges.dart';
import '../widgets/skip_step_sheet.dart';
import 'barcode_screen.dart';
import 'camera_screen.dart';

/// The guided capture sequence for one package.
///
/// Steps run in order — principal display panel, back, sides, then any extra
/// angles — and each one can end in exactly two ways: a capture the inspector
/// accepted, or a recorded reason there is none. "Finish this package" stays
/// disabled until every required step has reached one of those two states.
/// There is deliberately no override on that button: an incomplete evidence
/// set that reports itself complete is worse than one that admits it is not.
class ProductFlowScreen extends StatelessWidget {
  const ProductFlowScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final controller = InspectionScope.of(context);
    final product = controller.product;

    if (product == null) {
      return const Scaffold(
        body: Center(child: Text('No package session is open.')),
      );
    }

    return Scaffold(
      appBar: AppBar(
        title: Text(product.productLabel ?? 'Package'),
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(58),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
            child: CoverageIndicator(product: product),
          ),
        ),
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 120),
        children: <Widget>[
          _IdentificationCard(),
          const SizedBox(height: 18),
          Text(
            'Surfaces',
            style: Theme.of(context)
                .textTheme
                .titleSmall
                ?.copyWith(fontWeight: FontWeight.w700),
          ),
          const SizedBox(height: 8),
          for (final step in product.steps)
            _StepTile(
              step: step,
              isNext: identical(step, product.nextStep),
            ),
          const SizedBox(height: 12),
          OutlinedButton.icon(
            onPressed: () async {
              final step = await controller.addExtraAngle();
              if (!context.mounted) return;
              await Navigator.of(context).push(
                MaterialPageRoute<bool>(
                  builder: (_) => CameraScreen(step: step),
                ),
              );
            },
            icon: const Icon(Icons.add_a_photo_outlined),
            label: const Text('Add an extra angle'),
          ),
        ],
      ),
      bottomNavigationBar: _FinishBar(),
    );
  }
}

class _IdentificationCard extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    final controller = InspectionScope.of(context);
    final scan = controller.product?.identification;
    final theme = Theme.of(context);

    return Card(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                const Icon(Icons.qr_code_2, size: 20),
                const SizedBox(width: 8),
                Text(
                  'Product identification',
                  style: theme.textTheme.titleSmall
                      ?.copyWith(fontWeight: FontWeight.w700),
                ),
              ],
            ),
            const SizedBox(height: 8),
            if (scan == null)
              Text(
                'No barcode recorded yet. Optional — a package with no '
                'readable code is still inspected from its photographs.',
                style: theme.textTheme.bodySmall,
              )
            else ...<Widget>[
              Text(
                scan.rawValue,
                style: const TextStyle(
                  fontSize: 18,
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(height: 2),
              Text(
                '${scan.symbology.label} · ${scan.entryMode.label}'
                '${scan.isGtin ? (scan.checkDigitValid ? ' · check digit valid' : ' · check digit MISMATCH') : ''}',
                style: theme.textTheme.bodySmall,
              ),
            ],
            const SizedBox(height: 10),
            Container(
              padding: const EdgeInsets.all(9),
              decoration: BoxDecoration(
                color: theme.colorScheme.errorContainer.withValues(alpha: 0.35),
                borderRadius: BorderRadius.circular(7),
              ),
              child: Text(
                BarcodeScan.disclaimer,
                style: const TextStyle(fontSize: 11.5, height: 1.3),
              ),
            ),
            const SizedBox(height: 8),
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton.icon(
                onPressed: () => Navigator.of(context).push(
                  MaterialPageRoute<bool>(
                    builder: (_) => const BarcodeScreen(),
                  ),
                ),
                icon: const Icon(Icons.qr_code_scanner, size: 18),
                label: Text(scan == null ? 'Scan barcode' : 'Rescan'),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _StepTile extends StatelessWidget {
  const _StepTile({required this.step, required this.isNext});

  final SurfaceStepState step;
  final bool isNext;

  Color _statusColour(BuildContext context) => switch (step.status) {
        StepStatus.captured => const Color(0xFF1B7F4B),
        StepStatus.capturedNeedsReview => const Color(0xFF9A6400),
        StepStatus.skipped => const Color(0xFF5A5F66),
        StepStatus.pending => Theme.of(context).colorScheme.outline,
      };

  IconData get _statusIcon => switch (step.status) {
        StepStatus.captured => Icons.check_circle,
        StepStatus.capturedNeedsReview => Icons.flag,
        StepStatus.skipped => Icons.block,
        StepStatus.pending => Icons.radio_button_unchecked,
      };

  @override
  Widget build(BuildContext context) {
    final controller = InspectionScope.of(context);
    final theme = Theme.of(context);
    final accepted = step.acceptedCapture;
    final colour = _statusColour(context);

    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: isNext
            ? BorderSide(color: theme.colorScheme.primary, width: 1.6)
            : BorderSide(color: theme.colorScheme.outlineVariant),
      ),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Icon(_statusIcon, color: colour, size: 20),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: <Widget>[
                      Text(
                        step.label,
                        style: const TextStyle(fontWeight: FontWeight.w600),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        step.status.label,
                        style: theme.textTheme.bodySmall?.copyWith(color: colour),
                      ),
                    ],
                  ),
                ),
                if (isNext)
                  Container(
                    padding: const EdgeInsets.symmetric(
                        horizontal: 8, vertical: 3),
                    decoration: BoxDecoration(
                      color: theme.colorScheme.primary.withValues(alpha: 0.12),
                      borderRadius: BorderRadius.circular(20),
                    ),
                    child: Text(
                      'Next',
                      style: TextStyle(
                        fontSize: 11,
                        fontWeight: FontWeight.w700,
                        color: theme.colorScheme.primary,
                      ),
                    ),
                  ),
              ],
            ),

            if (step.skip != null) ...<Widget>[
              const SizedBox(height: 8),
              Text(
                step.skip!.description,
                style: theme.textTheme.bodySmall,
              ),
            ],

            if (accepted != null) ...<Widget>[
              const SizedBox(height: 12),
              Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  ClipRRect(
                    borderRadius: BorderRadius.circular(8),
                    child: Image.file(
                      File(accepted.localPath),
                      width: 74,
                      height: 74,
                      fit: BoxFit.cover,
                      errorBuilder: (_, _, _) => Container(
                        width: 74,
                        height: 74,
                        color: theme.colorScheme.surfaceContainerHighest,
                        child: const Icon(Icons.broken_image_outlined),
                      ),
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(child: QualityBadgeRow(report: accepted.quality)),
                ],
              ),
              if (step.manualReviewReason != null) ...<Widget>[
                const SizedBox(height: 10),
                Container(
                  padding: const EdgeInsets.all(9),
                  decoration: BoxDecoration(
                    color: const Color(0xFF9A6400).withValues(alpha: 0.10),
                    borderRadius: BorderRadius.circular(7),
                  ),
                  child: Text(
                    step.manualReviewReason!,
                    style: const TextStyle(fontSize: 11.5, height: 1.3),
                  ),
                ),
              ],
            ],

            const SizedBox(height: 6),
            Row(
              children: <Widget>[
                TextButton.icon(
                  onPressed: () => Navigator.of(context).push(
                    MaterialPageRoute<bool>(
                      builder: (_) => CameraScreen(step: step),
                    ),
                  ),
                  icon: Icon(
                    accepted == null ? Icons.camera_alt_outlined : Icons.refresh,
                    size: 18,
                  ),
                  // Manual retake is always available, whatever the checks
                  // concluded. An inspector who thinks a passing photo is
                  // wrong is right more often than the checker is.
                  label: Text(accepted == null ? 'Capture' : 'Retake'),
                ),
                const Spacer(),
                if (step.skip == null)
                  TextButton(
                    onPressed: () async {
                      final skip = await showSkipStepSheet(
                        context,
                        surfaceLabel: step.label,
                      );
                      if (skip == null) return;
                      await controller.skipStep(
                        step,
                        reason: skip.reason,
                        note: skip.note,
                      );
                    },
                    child: const Text('Not accessible'),
                  )
                else
                  TextButton(
                    onPressed: () => controller.clearSkip(step),
                    child: const Text('Undo'),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _FinishBar extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    final controller = InspectionScope.of(context);
    final product = controller.product;
    if (product == null) return const SizedBox.shrink();

    final theme = Theme.of(context);
    final pending = product.pendingSteps;

    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: <Widget>[
            if (pending.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Text(
                  'Still to resolve: '
                  '${pending.map((s) => s.label).join(', ')}',
                  style: theme.textTheme.bodySmall,
                ),
              ),
            FilledButton(
              onPressed: product.isComplete
                  ? () {
                      controller.closeProductSession();
                      Navigator.of(context).pop();
                    }
                  : null,
              style: FilledButton.styleFrom(
                padding: const EdgeInsets.symmetric(vertical: 15),
              ),
              child: Text(
                product.isComplete
                    ? 'Finish this package'
                    : '${pending.length} step'
                        '${pending.length == 1 ? '' : 's'} left',
              ),
            ),
          ],
        ),
      ),
    );
  }
}
