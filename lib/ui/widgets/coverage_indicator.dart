import 'package:flutter/material.dart';

import '../../models/product_session.dart';

/// "3 of 4 captured", with the bar behind it.
///
/// Counts steps that are *resolved* — captured or explicitly marked not
/// accessible — not steps that have photographs. A surface an inspector could
/// not reach and said so is finished work, and the indicator should not nag
/// about it as though it were outstanding.
class CoverageIndicator extends StatelessWidget {
  const CoverageIndicator({
    super.key,
    required this.product,
    this.showDetail = true,
  });

  final ProductSession product;
  final bool showDetail;

  @override
  Widget build(BuildContext context) {
    final total = product.requiredCount;
    final resolved = product.resolvedRequiredCount;
    final fraction = total == 0 ? 0.0 : resolved / total;
    final theme = Theme.of(context);

    final skipped = product.requiredSteps
        .where((s) => s.skip != null)
        .length;
    final flagged = product.stepsNeedingManualReview.length;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Row(
          children: <Widget>[
            Text(
              product.coverageLabel,
              style: theme.textTheme.titleMedium
                  ?.copyWith(fontWeight: FontWeight.w600),
            ),
            const Spacer(),
            if (product.isComplete)
              const _Pill(
                label: 'All steps resolved',
                colour: Color(0xFF1B7F4B),
              )
            else
              _Pill(
                label: '${total - resolved} remaining',
                colour: theme.colorScheme.primary,
              ),
          ],
        ),
        const SizedBox(height: 8),
        ClipRRect(
          borderRadius: BorderRadius.circular(4),
          child: LinearProgressIndicator(
            value: fraction,
            minHeight: 7,
            backgroundColor: theme.colorScheme.surfaceContainerHighest,
          ),
        ),
        if (showDetail && (skipped > 0 || flagged > 0)) ...<Widget>[
          const SizedBox(height: 8),
          Text(
            <String>[
              if (skipped > 0)
                '$skipped marked not accessible',
              if (flagged > 0)
                '$flagged flagged for manual review',
            ].join(' · '),
            style: theme.textTheme.bodySmall,
          ),
        ],
      ],
    );
  }
}

class _Pill extends StatelessWidget {
  const _Pill({required this.label, required this.colour});

  final String label;
  final Color colour;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(
        color: colour.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Text(
        label,
        style: TextStyle(
          color: colour,
          fontSize: 12,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}
