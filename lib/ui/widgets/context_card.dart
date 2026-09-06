import 'package:flutter/material.dart';

import '../../models/capture_context.dart';
import '../../state/inspection_controller.dart';

/// The commercial-context declaration for one package.
///
/// Four questions, none of them answered in advance. That is the whole design
/// of this widget and it is worth being explicit about why, because a form
/// with sensible defaults would be quicker to fill in and would be wrong.
///
/// These answers are applicability switches in the compliance engine. Whether
/// a country-of-origin declaration is required at all, whether the retail
/// packaging rules bite, whether the e-commerce provisions apply — each turns
/// on one of these flags. A default of "no" on `isImported` does not read
/// downstream as "nobody said"; it reads as an inspector asserting the package
/// is domestic, and it can suppress a requirement that should have been
/// evaluated. So there is no default, no pre-selection, and no way to finish a
/// package while a question is unanswered.
///
/// The questions ask what the inspector can see. None of them asks for a legal
/// conclusion, and the hints under each option describe the situation rather
/// than its consequence — an inspector deciding "is this for retail sale"
/// should be thinking about the shelf in front of them, not about which rule
/// they are about to trigger.
class ContextCard extends StatelessWidget {
  const ContextCard({super.key});

  @override
  Widget build(BuildContext context) {
    final controller = InspectionScope.of(context);
    final product = controller.product;
    if (product == null) return const SizedBox.shrink();

    final declaration = product.context;
    final theme = Theme.of(context);
    final complete = declaration.isComplete;

    return Card(
      margin: EdgeInsets.zero,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(
          color: complete
              ? theme.colorScheme.outlineVariant
              : theme.colorScheme.primary,
          width: complete ? 1 : 1.6,
        ),
      ),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                Icon(
                  complete ? Icons.fact_check_outlined : Icons.help_outline,
                  size: 20,
                  color: complete ? const Color(0xFF1B7F4B) : null,
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    'How this package was offered',
                    style: theme.textTheme.titleSmall
                        ?.copyWith(fontWeight: FontWeight.w700),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 6),
            Text(
              complete
                  ? 'Recorded. These answers travel with the photographs and '
                      'decide which rules are assessed.'
                  : 'Answer all four before finishing this package. Nothing is '
                      'filled in for you — a guessed answer here changes which '
                      'rules get assessed.',
              style: theme.textTheme.bodySmall,
            ),
            const SizedBox(height: 14),

            _ChannelQuestion(selected: declaration.saleChannel),

            const Divider(height: 26),

            _YesNoQuestion(
              question: 'Is the package imported?',
              hint: 'Look for an importer name, a country of origin, or '
                  'customs marking. If you cannot tell from the package, say '
                  'so on the record rather than guessing here.',
              value: declaration.isImported,
              onChanged: (v) => controller.updateContext(isImported: v),
            ),

            const SizedBox(height: 18),

            _YesNoQuestion(
              question: 'Is it for retail sale?',
              hint: 'A package on a shelf or counter for a shopper to buy. '
                  'Bulk stock behind the counter and "not for retail sale" '
                  'marked packages are not.',
              value: declaration.isForRetail,
              onChanged: (v) => controller.updateContext(isForRetail: v),
            ),

            const SizedBox(height: 18),

            _YesNoQuestion(
              question: 'Does this back an e-commerce listing?',
              hint: 'Yes only when you are inspecting this package against an '
                  'online listing of it.',
              value: declaration.isEcommerceListing,
              onChanged: (v) => controller.updateContext(isEcommerceListing: v),
            ),

            const Divider(height: 26),

            _StateField(jurisdiction: declaration.jurisdiction),
          ],
        ),
      ),
    );
  }
}

/// One sale-channel option.
///
/// Hand-rolled rather than a [RadioListTile] so that "nothing is selected yet"
/// is a state the widget renders honestly — an unselected radio group reads as
/// a question awaiting an answer, which is exactly what it is.
class _ChannelOption extends StatelessWidget {
  const _ChannelOption({
    required this.channel,
    required this.selected,
    required this.onTap,
  });

  final SaleChannel channel;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(8),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 7, horizontal: 4),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Icon(
              selected
                  ? Icons.radio_button_checked
                  : Icons.radio_button_unchecked,
              size: 20,
              color: selected
                  ? theme.colorScheme.primary
                  : theme.colorScheme.outline,
            ),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  Text(
                    channel.label,
                    style: TextStyle(
                      fontSize: 14,
                      fontWeight: selected ? FontWeight.w600 : FontWeight.w400,
                    ),
                  ),
                  const SizedBox(height: 1),
                  Text(
                    channel.hint,
                    style: const TextStyle(fontSize: 11.5, height: 1.25),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ChannelQuestion extends StatelessWidget {
  const _ChannelQuestion({required this.selected});

  final SaleChannel? selected;

  @override
  Widget build(BuildContext context) {
    final controller = InspectionScope.read(context);
    final theme = Theme.of(context);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Text(
          'Where was it offered for sale?',
          style: theme.textTheme.bodyMedium
              ?.copyWith(fontWeight: FontWeight.w600),
        ),
        const SizedBox(height: 6),
        for (final channel in SaleChannel.values)
          _ChannelOption(
            channel: channel,
            selected: selected == channel,
            onTap: () => controller.updateContext(saleChannel: channel),
          ),
      ],
    );
  }
}

/// A yes/no question with no default.
///
/// Rendered as two buttons rather than a switch or a checkbox on purpose: a
/// switch has an off position that looks like an answer, and an inspector who
/// scrolls past it has silently said "no". Two buttons, neither selected, look
/// like what they are — a question nobody has answered yet.
class _YesNoQuestion extends StatelessWidget {
  const _YesNoQuestion({
    required this.question,
    required this.hint,
    required this.value,
    required this.onChanged,
  });

  final String question;
  final String hint;
  final bool? value;
  final ValueChanged<bool> onChanged;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Text(
          question,
          style: theme.textTheme.bodyMedium
              ?.copyWith(fontWeight: FontWeight.w600),
        ),
        const SizedBox(height: 3),
        Text(hint, style: const TextStyle(fontSize: 11.5, height: 1.25)),
        const SizedBox(height: 8),
        Row(
          children: <Widget>[
            _Choice(
              label: 'Yes',
              selected: value == true,
              onPressed: () => onChanged(true),
            ),
            const SizedBox(width: 8),
            _Choice(
              label: 'No',
              selected: value == false,
              onPressed: () => onChanged(false),
            ),
            if (value == null) ...<Widget>[
              const SizedBox(width: 10),
              Text(
                'Not answered',
                style: TextStyle(
                  fontSize: 11.5,
                  color: theme.colorScheme.primary,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ],
        ),
      ],
    );
  }
}

class _Choice extends StatelessWidget {
  const _Choice({
    required this.label,
    required this.selected,
    required this.onPressed,
  });

  final String label;
  final bool selected;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    if (selected) {
      return FilledButton(
        onPressed: onPressed,
        style: FilledButton.styleFrom(
          visualDensity: VisualDensity.compact,
          padding: const EdgeInsets.symmetric(horizontal: 22),
        ),
        child: Text(label),
      );
    }
    return OutlinedButton(
      onPressed: onPressed,
      style: OutlinedButton.styleFrom(
        visualDensity: VisualDensity.compact,
        padding: const EdgeInsets.symmetric(horizontal: 22),
        foregroundColor: theme.colorScheme.onSurfaceVariant,
      ),
      child: Text(label),
    );
  }
}

/// Optional state within the jurisdiction.
///
/// Optional because the contract types it as nullable and because an inspector
/// must never be blocked from capturing evidence by a form field. An empty box
/// serialises as `null`, which is the contract's own representation of "not
/// specified" — distinct from a wrong state name.
class _StateField extends StatefulWidget {
  const _StateField({required this.jurisdiction});

  final Jurisdiction jurisdiction;

  @override
  State<_StateField> createState() => _StateFieldState();
}

class _StateFieldState extends State<_StateField> {
  late final TextEditingController _text =
      TextEditingController(text: widget.jurisdiction.state ?? '');

  @override
  void dispose() {
    _text.dispose();
    super.dispose();
  }

  void _commit(String raw) {
    final trimmed = raw.trim();
    InspectionScope.read(context).updateContext(
      jurisdiction: Jurisdiction(
        country: widget.jurisdiction.country,
        state: trimmed.isEmpty ? null : trimmed,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.center,
      children: <Widget>[
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
          decoration: BoxDecoration(
            color: Theme.of(context).colorScheme.surfaceContainerHighest,
            borderRadius: BorderRadius.circular(7),
          ),
          child: Text(
            widget.jurisdiction.country,
            style: const TextStyle(fontWeight: FontWeight.w700),
          ),
        ),
        const SizedBox(width: 10),
        Expanded(
          child: TextField(
            controller: _text,
            textCapitalization: TextCapitalization.words,
            decoration: const InputDecoration(
              labelText: 'State (optional)',
              isDense: true,
              border: OutlineInputBorder(),
            ),
            onSubmitted: _commit,
            onTapOutside: (_) {
              FocusScope.of(context).unfocus();
              _commit(_text.text);
            },
          ),
        ),
      ],
    );
  }
}
