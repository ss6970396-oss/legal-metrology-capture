import 'package:camera/camera.dart';
import 'package:flutter/material.dart';

import '../../models/surface_step.dart';
import '../../state/inspection_controller.dart';
import '../widgets/alignment_overlay.dart';
import 'preview_screen.dart';

/// Live capture for one surface.
///
/// Pops `true` once a frame has been accepted for this step, so the flow
/// screen can move on. Anything else — a retake, a back press — leaves the
/// step exactly as it was.
class CameraScreen extends StatefulWidget {
  const CameraScreen({super.key, required this.step});

  final SurfaceStepState step;

  @override
  State<CameraScreen> createState() => _CameraScreenState();
}

class _CameraScreenState extends State<CameraScreen>
    with WidgetsBindingObserver {
  bool _initialising = true;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    WidgetsBinding.instance.addPostFrameCallback((_) => _openCamera());
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    // Release the camera as we leave; holding it open across the app would
    // block the barcode scanner, which runs its own camera session.
    InspectionScope.read(context).cameraService.dispose();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    final service = InspectionScope.read(context).cameraService;
    if (state == AppLifecycleState.inactive ||
        state == AppLifecycleState.paused) {
      service.dispose();
    } else if (state == AppLifecycleState.resumed && !service.isReady) {
      _openCamera();
    }
  }

  Future<void> _openCamera() async {
    if (!mounted) return;
    setState(() {
      _initialising = true;
      _error = null;
    });
    final service = InspectionScope.read(context).cameraService;
    final ready = await service.initialize();
    if (!mounted) return;
    setState(() {
      _initialising = false;
      _error = ready ? null : (service.lastError ?? 'Camera unavailable.');
    });
  }

  Future<void> _shoot() async {
    if (_busy) return;
    setState(() => _busy = true);
    final controller = InspectionScope.read(context);
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);

    try {
      final record = await controller.captureSurface(widget.step);
      if (!mounted) return;
      setState(() => _busy = false);

      // Nothing is decided here. The frame is on disk and analysed; the
      // inspector decides on the preview screen, and only there.
      final accepted = await navigator.push<bool>(
        MaterialPageRoute<bool>(
          builder: (_) => PreviewScreen(step: widget.step, record: record),
        ),
      );
      if (!mounted) return;
      if (accepted == true) {
        navigator.pop(true);
      }
    } on Object catch (error) {
      if (!mounted) return;
      setState(() => _busy = false);
      messenger.showSnackBar(
        SnackBar(content: Text('Capture failed: $error')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final controller = InspectionScope.of(context);
    final service = controller.cameraService;
    final attemptsUsed = widget.step.failedAttemptCount;

    return Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(
        backgroundColor: Colors.black,
        foregroundColor: Colors.white,
        title: Text(widget.step.label),
        actions: <Widget>[
          if (service.isReady)
            IconButton(
              tooltip: service.torchOn ? 'Torch off' : 'Torch on',
              icon: Icon(
                service.torchOn ? Icons.flashlight_on : Icons.flashlight_off,
              ),
              onPressed: () async {
                await service.setTorch(!service.torchOn);
                if (mounted) setState(() {});
              },
            ),
        ],
      ),
      body: Column(
        children: <Widget>[
          Expanded(child: _buildPreview(service)),
          if (attemptsUsed > 0)
            Container(
              width: double.infinity,
              color: const Color(0xFF3A2A00),
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
              child: Text(
                widget.step.retryLimitReached
                    ? 'Retry allowance used. You can accept the next capture '
                        'with this surface flagged for manual review.'
                    : '$attemptsUsed failed attempt'
                        '${attemptsUsed == 1 ? '' : 's'} so far · '
                        '${widget.step.attemptsRemaining} retake'
                        '${widget.step.attemptsRemaining == 1 ? '' : 's'} '
                        'before this surface can be flagged.',
                style: const TextStyle(color: Color(0xFFFFD79A), fontSize: 12.5),
              ),
            ),
          _buildShutterBar(service),
        ],
      ),
    );
  }

  Widget _buildPreview(dynamic service) {
    if (_initialising) {
      return const Center(child: CircularProgressIndicator());
    }

    final error = _error;
    if (error != null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(28),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: <Widget>[
              const Icon(Icons.no_photography_outlined,
                  size: 44, color: Colors.white54),
              const SizedBox(height: 14),
              Text(
                error,
                textAlign: TextAlign.center,
                style: const TextStyle(color: Colors.white70, height: 1.4),
              ),
              const SizedBox(height: 8),
              const Text(
                'A real camera is needed to capture this surface. You can '
                'still mark it not accessible from the previous screen.',
                textAlign: TextAlign.center,
                style: TextStyle(color: Colors.white38, fontSize: 12.5),
              ),
              const SizedBox(height: 18),
              FilledButton.tonal(
                onPressed: _openCamera,
                child: const Text('Try again'),
              ),
            ],
          ),
        ),
      );
    }

    final CameraController? cameraController = service.controller;
    if (cameraController == null || !cameraController.value.isInitialized) {
      return const Center(child: CircularProgressIndicator());
    }

    return LayoutBuilder(
      builder: (context, constraints) {
        return GestureDetector(
          onTapDown: (details) {
            final x = (details.localPosition.dx / constraints.maxWidth)
                .clamp(0.0, 1.0);
            final y = (details.localPosition.dy / constraints.maxHeight)
                .clamp(0.0, 1.0);
            service.focusAt(x, y);
          },
          child: Stack(
            fit: StackFit.expand,
            children: <Widget>[
              FittedBox(
                fit: BoxFit.cover,
                child: SizedBox(
                  width: cameraController.value.previewSize?.height ??
                      constraints.maxWidth,
                  height: cameraController.value.previewSize?.width ??
                      constraints.maxHeight,
                  child: CameraPreview(cameraController),
                ),
              ),
              AlignmentOverlay(
                shape: widget.step.definition.overlay,
                label: widget.step.label,
                guidance: widget.step.definition.guidance,
              ),
            ],
          ),
        );
      },
    );
  }

  Widget _buildShutterBar(dynamic service) {
    final enabled = service.isReady && !_busy;
    return Container(
      color: Colors.black,
      padding: const EdgeInsets.symmetric(vertical: 20),
      child: Center(
        child: GestureDetector(
          onTap: enabled ? _shoot : null,
          child: AnimatedContainer(
            duration: const Duration(milliseconds: 120),
            width: 74,
            height: 74,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: enabled ? Colors.white : Colors.white24,
              border: Border.all(color: Colors.white38, width: 4),
            ),
            child: _busy
                ? const Padding(
                    padding: EdgeInsets.all(20),
                    child: CircularProgressIndicator(strokeWidth: 3),
                  )
                : null,
          ),
        ),
      ),
    );
  }
}
