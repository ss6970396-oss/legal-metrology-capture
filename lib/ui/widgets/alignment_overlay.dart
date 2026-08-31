import 'package:flutter/material.dart';

import '../../models/surface_step.dart';

/// The alignment guide drawn over the live camera preview.
///
/// It is not decoration. The framing check downstream fails a capture whose
/// silhouette touches a frame edge or fills too little of it, so the guide is
/// drawn at the size that check is happy with — an inspector who fills the
/// guide passes framing. A guide that disagreed with the checker would train
/// people to compose shots that then get rejected.
class AlignmentOverlay extends StatelessWidget {
  const AlignmentOverlay({
    super.key,
    required this.shape,
    required this.label,
    this.guidance,
  });

  final OverlayShape shape;
  final String label;
  final String? guidance;

  @override
  Widget build(BuildContext context) {
    return IgnorePointer(
      child: CustomPaint(
        painter: _AlignmentPainter(shape),
        child: SafeArea(
          child: Padding(
            padding: const EdgeInsets.all(20),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: <Widget>[
                _Caption(title: label, body: guidance),
                const Spacer(),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _Caption extends StatelessWidget {
  const _Caption({required this.title, this.body});

  final String title;
  final String? body;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: 0.62),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          Text(
            title,
            style: const TextStyle(
              color: Colors.white,
              fontSize: 16,
              fontWeight: FontWeight.w600,
            ),
          ),
          if (body != null) ...<Widget>[
            const SizedBox(height: 4),
            Text(
              body!,
              style: TextStyle(
                color: Colors.white.withValues(alpha: 0.85),
                fontSize: 13,
                height: 1.3,
              ),
            ),
          ],
        ],
      ),
    );
  }
}

/// Fraction of the preview each guide shape occupies.
Size guideFractionFor(OverlayShape shape) => switch (shape) {
      OverlayShape.principalPanel => const Size(0.74, 0.80),
      OverlayShape.rearPanel => const Size(0.74, 0.80),
      OverlayShape.narrowSide => const Size(0.36, 0.80),
      OverlayShape.freeform => const Size(0.86, 0.86),
    };

class _AlignmentPainter extends CustomPainter {
  _AlignmentPainter(this.shape);

  final OverlayShape shape;

  @override
  void paint(Canvas canvas, Size size) {
    final fraction = guideFractionFor(shape);
    final guide = Rect.fromCenter(
      center: Offset(size.width / 2, size.height / 2),
      width: size.width * fraction.width,
      height: size.height * fraction.height,
    );
    final rounded = RRect.fromRectAndRadius(guide, const Radius.circular(12));

    // Dim everything outside the guide so the eye goes to the target area.
    final scrim = Path.combine(
      PathOperation.difference,
      Path()..addRect(Offset.zero & size),
      Path()..addRRect(rounded),
    );
    canvas.drawPath(scrim, Paint()..color = Colors.black.withValues(alpha: 0.38));

    canvas.drawRRect(
      rounded,
      Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2
        ..color = Colors.white.withValues(alpha: 0.9),
    );

    // Corner ticks — easier to align a package against than a plain outline.
    final tick = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 4
      ..strokeCap = StrokeCap.round
      ..color = Colors.white;
    const armLength = 26.0;

    void corner(Offset origin, double dx, double dy) {
      canvas.drawLine(origin, origin.translate(armLength * dx, 0), tick);
      canvas.drawLine(origin, origin.translate(0, armLength * dy), tick);
    }

    corner(guide.topLeft, 1, 1);
    corner(guide.topRight, -1, 1);
    corner(guide.bottomLeft, 1, -1);
    corner(guide.bottomRight, -1, -1);
  }

  @override
  bool shouldRepaint(_AlignmentPainter oldDelegate) =>
      oldDelegate.shape != shape;
}
