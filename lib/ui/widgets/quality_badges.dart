import 'package:flutter/material.dart';

import '../../models/quality_report.dart';

Color verdictColour(QualityVerdict verdict) => switch (verdict) {
      QualityVerdict.pass => const Color(0xFF1B7F4B),
      QualityVerdict.warn => const Color(0xFF9A6400),
      QualityVerdict.fail => const Color(0xFFB3261E),
      QualityVerdict.unavailable => const Color(0xFF5A5F66),
    };

IconData verdictIcon(QualityVerdict verdict) => switch (verdict) {
      QualityVerdict.pass => Icons.check_circle_outline,
      QualityVerdict.warn => Icons.error_outline,
      QualityVerdict.fail => Icons.cancel_outlined,
      QualityVerdict.unavailable => Icons.help_outline,
    };

/// Compact badges overlaid on the preview image.
///
/// Every check gets a badge, including the ones that passed and the ones that
/// could not run. Showing only problems would leave the inspector unable to
/// tell "this was checked and it is fine" from "this was never checked".
class QualityBadgeRow extends StatelessWidget {
  const QualityBadgeRow({super.key, required this.report});

  final QualityReport report;

  @override
  Widget build(BuildContext context) {
    return Wrap(
      spacing: 8,
      runSpacing: 8,
      children: <Widget>[
        for (final result in report.results)
          _Badge(
            label: result.label,
            verdict: result.verdict,
            score: result.score,
          ),
      ],
    );
  }
}

class _Badge extends StatelessWidget {
  const _Badge({
    required this.label,
    required this.verdict,
    required this.score,
  });

  final String label;
  final QualityVerdict verdict;
  final double score;

  @override
  Widget build(BuildContext context) {
    final colour = verdictColour(verdict);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: colour.withValues(alpha: 0.92),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          Icon(verdictIcon(verdict), size: 15, color: Colors.white),
          const SizedBox(width: 6),
          Text(
            score >= 0
                ? '$label ${(score * 100).round()}%'
                : '$label —',
            style: const TextStyle(
              color: Colors.white,
              fontSize: 12.5,
              fontWeight: FontWeight.w600,
            ),
          ),
        ],
      ),
    );
  }
}

/// The expanded list, with each check's specific message.
class QualityDetailList extends StatelessWidget {
  const QualityDetailList({super.key, required this.report});

  final QualityReport report;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        for (final result in report.results)
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Padding(
                  padding: const EdgeInsets.only(top: 2),
                  child: Icon(
                    verdictIcon(result.verdict),
                    size: 18,
                    color: verdictColour(result.verdict),
                  ),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: <Widget>[
                      Text(
                        '${result.label} — ${result.verdict.label}',
                        style: TextStyle(
                          fontWeight: FontWeight.w600,
                          color: verdictColour(result.verdict),
                        ),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        result.message,
                        style: const TextStyle(fontSize: 13, height: 1.35),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        Padding(
          padding: const EdgeInsets.only(top: 4),
          child: Text(
            'Analysed in ${report.analysisDurationMs} ms · '
            '${report.sourceWidth}×${report.sourceHeight} · '
            'rules ${report.pipelineVersion}. All scores are stored with the '
            'image whatever the outcome.',
            style: TextStyle(
              fontSize: 11.5,
              color: Theme.of(context).textTheme.bodySmall?.color,
            ),
          ),
        ),
      ],
    );
  }
}
