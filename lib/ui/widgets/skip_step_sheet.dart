import 'package:flutter/material.dart';

import '../../models/surface_step.dart';

/// Records why a surface has no photograph.
///
/// This exists so that "I could not get at this face" has an honest place to
/// go. Without it the pressure is to photograph the front panel twice to clear
/// the coverage indicator, which produces an evidence set that looks complete
/// and is not — far worse than a documented gap.
///
/// Returns null if the inspector backs out.
Future<StepSkip?> showSkipStepSheet(
  BuildContext context, {
  required String surfaceLabel,
}) {
  return showModalBottomSheet<StepSkip>(
    context: context,
    isScrollControlled: true,
    builder: (context) => _SkipStepSheet(surfaceLabel: surfaceLabel),
  );
}

class _SkipStepSheet extends StatefulWidget {
  const _SkipStepSheet({required this.surfaceLabel});

  final String surfaceLabel;

  @override
  State<_SkipStepSheet> createState() => _SkipStepSheetState();
}

class _SkipStepSheetState extends State<_SkipStepSheet> {
  SkipReason? _reason;
  final TextEditingController _note = TextEditingController();

  @override
  void dispose() {
    _note.dispose();
    super.dispose();
  }

  bool get _canSave {
    final reason = _reason;
    if (reason == null) return false;
    if (reason.requiresNote) return _note.text.trim().isNotEmpty;
    return true;
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: EdgeInsets.only(
        left: 20,
        right: 20,
        top: 20,
        bottom: MediaQuery.of(context).viewInsets.bottom + 20,
      ),
      child: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: <Widget>[
            Text(
              'Mark "${widget.surfaceLabel}" not accessible',
              style: theme.textTheme.titleMedium
                  ?.copyWith(fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 6),
            Text(
              'This records a reason instead of a photograph. It counts as a '
              'resolved step, and the reason travels with the inspection.',
              style: theme.textTheme.bodySmall,
            ),
            const SizedBox(height: 14),
            RadioGroup<SkipReason>(
              groupValue: _reason,
              onChanged: (value) => setState(() => _reason = value),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: <Widget>[
                  for (final reason in SkipReason.values)
                    RadioListTile<SkipReason>(
                      value: reason,
                      title: Text(
                        reason.label,
                        style: const TextStyle(fontSize: 14),
                      ),
                      contentPadding: EdgeInsets.zero,
                      dense: true,
                    ),
                ],
              ),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _note,
              onChanged: (_) => setState(() {}),
              maxLines: 3,
              minLines: 2,
              textCapitalization: TextCapitalization.sentences,
              decoration: InputDecoration(
                labelText: _reason?.requiresNote == true
                    ? 'Reason (required)'
                    : 'Note (optional)',
                border: const OutlineInputBorder(),
                hintText: 'What stopped you photographing this face?',
              ),
            ),
            const SizedBox(height: 16),
            Row(
              children: <Widget>[
                Expanded(
                  child: OutlinedButton(
                    onPressed: () => Navigator.of(context).pop(),
                    child: const Text('Cancel'),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: FilledButton(
                    onPressed: _canSave
                        ? () => Navigator.of(context).pop(
                              StepSkip(
                                reason: _reason!,
                                note: _note.text.trim().isEmpty
                                    ? null
                                    : _note.text.trim(),
                                recordedAtUtc: DateTime.now().toUtc(),
                              ),
                            )
                        : null,
                    child: const Text('Record reason'),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
