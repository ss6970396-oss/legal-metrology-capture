import 'dart:io';

import 'package:flutter/material.dart';

import '../../models/capture_record.dart';
import '../../models/quality_report.dart';
import '../../models/surface_step.dart';
import '../../state/inspection_controller.dart';
import '../widgets/quality_badges.dart';

/// Full-screen review of one captured frame.
///
/// Exactly two actions: Accept or Retake. There is no auto-accept path, no
/// timeout that decides for the inspector, and no way to leave without
/// choosing — backing out is a retake, and it says so.
///
/// When a quality check has failed, Accept stays disabled until the retry
/// allowance is spent, and the specific failure text is shown rather than a
/// generic "quality problem". An inspector who is told "photo is out of focus,
/// tap to refocus" can fix it; one told "quality check failed" can only guess.
class PreviewScreen extends StatefulWidget {
  const PreviewScreen({
    super.key,
    required this.step,
    required this.record,
  });

  final SurfaceStepState step;
  final CaptureRecord record;

  @override
  State<PreviewScreen> createState() => _PreviewScreenState();
}

class _PreviewScreenState extends State<PreviewScreen> {
  bool _showDetail = false;
  bool _busy = false;

  Future<void> _accept() async {
    if (_busy) return;
    setState(() => _busy = true);
    final controller = InspectionScope.read(context);
    final navigator = Navigator.of(context);
    await controller.acceptCapture(widget.step, widget.record);
    if (!mounted) return;
    navigator.pop(true);
  }

  Future<void> _retake() async {
    if (_busy) return;
    setState(() => _busy = true);
    final controller = InspectionScope.read(context);
    final navigator = Navigator.of(context);
    await controller.markRetaken(widget.step, widget.record);
    if (!mounted) return;
    navigator.pop(false);
  }

  @override
  Widget build(BuildContext context) {
    final controller = InspectionScope.of(context);
    final report = widget.record.quality;
    final blockedReason =
        controller.acceptanceBlockedReason(widget.step, widget.record);
    final canAccept = blockedReason == null;
    final failed = report.overall == QualityVerdict.fail;

    return PopScope(
      // Leaving without deciding would strand the attempt in "pending". Treat
      // a back gesture as an explicit retake instead.
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) _retake();
      },
      child: Scaffold(
        backgroundColor: Colors.black,
        appBar: AppBar(
          backgroundColor: Colors.black,
          foregroundColor: Colors.white,
          title: Text('${widget.step.label} — attempt '
              '${widget.record.attemptNumber}'),
        ),
        body: Column(
          children: <Widget>[
            Expanded(
              child: Stack(
                children: <Widget>[
                  Positioned.fill(
                    child: InteractiveViewer(
                      minScale: 1,
                      maxScale: 6,
                      child: Center(
                        child: Image.file(
                          File(widget.record.localPath),
                          fit: BoxFit.contain,
                          errorBuilder: (context, error, stack) => const Center(
                            child: Text(
                              'The captured image could not be displayed.',
                              style: TextStyle(color: Colors.white70),
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                  Positioned(
                    left: 12,
                    right: 12,
                    top: 12,
                    child: QualityBadgeRow(report: report),
                  ),
                  const Positioned(
                    left: 12,
                    right: 12,
                    bottom: 12,
                    child: Text(
                      'Pinch to zoom and check the small print before deciding.',
                      style: TextStyle(color: Colors.white54, fontSize: 12),
                    ),
                  ),
                ],
              ),
            ),
            _buildDecisionPanel(
              report: report,
              failed: failed,
              canAccept: canAccept,
              blockedReason: blockedReason,
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildDecisionPanel({
    required QualityReport report,
    required bool failed,
    required bool canAccept,
    required String? blockedReason,
  }) {
    final theme = Theme.of(context);
    final flagOnAccept = failed && canAccept;

    return Material(
      color: theme.colorScheme.surface,
      child: SafeArea(
        top: false,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 14, 16, 12),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
              if (failed) ...<Widget>[
                for (final reason in report.blockingReasons)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 8),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: <Widget>[
                        Icon(
                          Icons.cancel_outlined,
                          size: 18,
                          color: verdictColour(QualityVerdict.fail),
                        ),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text(
                            reason,
                            style: const TextStyle(fontSize: 13.5, height: 1.35),
                          ),
                        ),
                      ],
                    ),
                  ),
              ] else if (report.warnings.isNotEmpty) ...<Widget>[
                for (final warning in report.warnings)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 8),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: <Widget>[
                        Icon(
                          Icons.error_outline,
                          size: 18,
                          color: verdictColour(QualityVerdict.warn),
                        ),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text(
                            warning.message,
                            style: const TextStyle(fontSize: 13.5, height: 1.35),
                          ),
                        ),
                      ],
                    ),
                  ),
              ] else
                Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: Row(
                    children: <Widget>[
                      Icon(
                        Icons.check_circle_outline,
                        size: 18,
                        color: verdictColour(QualityVerdict.pass),
                      ),
                      const SizedBox(width: 8),
                      const Expanded(
                        child: Text(
                          'All quality checks passed.',
                          style: TextStyle(fontSize: 13.5),
                        ),
                      ),
                    ],
                  ),
                ),

              if (blockedReason != null)
                Container(
                  margin: const EdgeInsets.only(bottom: 10),
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: verdictColour(QualityVerdict.fail)
                        .withValues(alpha: 0.09),
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Text(
                    blockedReason,
                    style: const TextStyle(fontSize: 12.5, height: 1.35),
                  ),
                ),

              if (flagOnAccept)
                Container(
                  margin: const EdgeInsets.only(bottom: 10),
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: verdictColour(QualityVerdict.warn)
                        .withValues(alpha: 0.10),
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Text(
                    'Accepting will store this surface as '
                    '"${SurfaceStepState.manualReviewFlag}". The image and all '
                    'its scores are uploaded either way.',
                    style: const TextStyle(fontSize: 12.5, height: 1.35),
                  ),
                ),

              TextButton(
                onPressed: () => setState(() => _showDetail = !_showDetail),
                child: Text(
                  _showDetail
                      ? 'Hide all check results'
                      : 'Show all check results',
                ),
              ),
              if (_showDetail)
                Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: QualityDetailList(report: report),
                ),

              Row(
                children: <Widget>[
                  Expanded(
                    child: OutlinedButton.icon(
                      onPressed: _busy ? null : _retake,
                      icon: const Icon(Icons.refresh),
                      style: OutlinedButton.styleFrom(
                        padding: const EdgeInsets.symmetric(vertical: 14),
                      ),
                      label: const Text('Retake'),
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: FilledButton.icon(
                      onPressed: (_busy || !canAccept) ? null : _accept,
                      icon: Icon(
                        flagOnAccept ? Icons.flag_outlined : Icons.check,
                      ),
                      style: FilledButton.styleFrom(
                        padding: const EdgeInsets.symmetric(vertical: 14),
                        backgroundColor: flagOnAccept
                            ? verdictColour(QualityVerdict.warn)
                            : null,
                      ),
                      label: Text(
                        flagOnAccept ? 'Accept & flag' : 'Accept',
                      ),
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}
