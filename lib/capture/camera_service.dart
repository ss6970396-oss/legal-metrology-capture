import 'dart:ui' show Offset;

import 'package:camera/camera.dart';

/// What the camera was actually configured to do when a frame was taken.
///
/// Recorded per capture rather than assumed, because several of these settings
/// are best-effort: `setFocusMode`, `setZoomLevel` and friends throw on
/// hardware that does not support them, and a capture taken with autofocus
/// unavailable is a materially different piece of evidence from one taken with
/// autofocus locked on. Stating which settings took effect is more useful than
/// claiming all of them did.
class CameraCapabilities {
  const CameraCapabilities({
    required this.lensDirection,
    required this.resolutionPreset,
    required this.autofocusApplied,
    required this.exposureAutoApplied,
    required this.flashDisabled,
    required this.zoomReset,
    required this.appliedZoom,
    required this.previewWidth,
    required this.previewHeight,
    this.notes = const <String>[],
  });

  final String lensDirection;
  final String resolutionPreset;
  final bool autofocusApplied;
  final bool exposureAutoApplied;
  final bool flashDisabled;
  final bool zoomReset;
  final double appliedZoom;
  final double previewWidth;
  final double previewHeight;

  /// Anything that did not apply cleanly, in plain words.
  final List<String> notes;

  Map<String, String> toMetadata() => <String, String>{
        'lensDirection': lensDirection,
        'resolutionPreset': resolutionPreset,
        'autofocus': autofocusApplied ? 'auto' : 'unavailable',
        'exposure': exposureAutoApplied ? 'auto' : 'unavailable',
        'flash': flashDisabled ? 'off' : 'unknown',
        'digitalZoom': appliedZoom.toStringAsFixed(2),
        'zoomReset': zoomReset ? 'yes' : 'no',
        'previewSize':
            '${previewWidth.toStringAsFixed(0)}x${previewHeight.toStringAsFixed(0)}',
        // package:camera exposes no JPEG compression setting. The plugin
        // hands back whatever the platform encoder produces at the chosen
        // resolution preset, which on both Android and iOS is the high
        // quality path. Stated here rather than silently implied, because
        // "max JPEG quality" is a requirement someone will want to verify.
        'jpegQuality': 'platform-default (not configurable via package:camera)',
        if (notes.isNotEmpty) 'notes': notes.join('; '),
      };
}

/// Owns the [CameraController] and puts it into the configuration inspection
/// evidence requires: rear lens, maximum resolution, autofocus on, no digital
/// zoom, flash off.
///
/// Flash defaults off deliberately. An on-camera flash fired at a laminated or
/// shrink-wrapped package produces exactly the specular hotspot the glare
/// check is looking for, so defaulting it on would manufacture the failure.
/// The torch remains available as an explicit inspector choice for genuinely
/// dark premises.
class CameraService {
  CameraController? _controller;
  CameraCapabilities? _capabilities;
  String? _lastError;
  bool _torchOn = false;

  CameraController? get controller => _controller;
  CameraCapabilities? get capabilities => _capabilities;
  String? get lastError => _lastError;
  bool get torchOn => _torchOn;

  bool get isReady => _controller?.value.isInitialized ?? false;

  /// Brings the camera up. Never throws: a device without a usable camera has
  /// to fail visibly in the UI, not crash the inspection.
  Future<bool> initialize() async {
    await dispose();
    _lastError = null;

    try {
      final cameras = await availableCameras();
      if (cameras.isEmpty) {
        _lastError = 'No camera was found on this device.';
        return false;
      }

      final rear = cameras.firstWhere(
        (c) => c.lensDirection == CameraLensDirection.back,
        orElse: () => cameras.first,
      );

      final controller = CameraController(
        rear,
        // Maximum the hardware offers. Legal metrology declarations are set in
        // very small type; resolution is the one thing that cannot be
        // recovered after the fact.
        ResolutionPreset.max,
        enableAudio: false,
        imageFormatGroup: ImageFormatGroup.jpeg,
      );

      await controller.initialize();
      _controller = controller;

      final notes = <String>[];
      if (rear.lensDirection != CameraLensDirection.back) {
        notes.add(
          'No rear camera available; fell back to '
          '${rear.lensDirection.name} lens',
        );
      }

      final autofocus = await _tryApply(
        () => controller.setFocusMode(FocusMode.auto),
        notes,
        'autofocus could not be enabled',
      );
      final exposure = await _tryApply(
        () => controller.setExposureMode(ExposureMode.auto),
        notes,
        'auto exposure could not be enabled',
      );
      final flash = await _tryApply(
        () => controller.setFlashMode(FlashMode.off),
        notes,
        'flash could not be forced off',
      );

      // Digital zoom is interpolation, not detail. Pin to the minimum the
      // device reports, which is the un-zoomed sensor read-out.
      var appliedZoom = 1.0;
      var zoomReset = false;
      try {
        final minimumZoom = await controller.getMinZoomLevel();
        await controller.setZoomLevel(minimumZoom);
        appliedZoom = minimumZoom;
        zoomReset = true;
      } on Object {
        notes.add('zoom level could not be reset');
      }

      final previewSize = controller.value.previewSize;
      _capabilities = CameraCapabilities(
        lensDirection: rear.lensDirection.name,
        resolutionPreset: ResolutionPreset.max.name,
        autofocusApplied: autofocus,
        exposureAutoApplied: exposure,
        flashDisabled: flash,
        zoomReset: zoomReset,
        appliedZoom: appliedZoom,
        previewWidth: previewSize?.width ?? 0,
        previewHeight: previewSize?.height ?? 0,
        notes: notes,
      );
      _torchOn = false;
      return true;
    } on CameraException catch (error) {
      _lastError = error.description ?? error.code;
      return false;
    } on Object catch (error) {
      _lastError = '$error';
      return false;
    }
  }

  Future<bool> _tryApply(
    Future<void> Function() action,
    List<String> notes,
    String failureNote,
  ) async {
    try {
      await action();
      return true;
    } on Object {
      notes.add(failureNote);
      return false;
    }
  }

  /// Focuses on the point the inspector tapped, expressed in 0..1 preview
  /// coordinates.
  Future<void> focusAt(double x, double y) async {
    final controller = _controller;
    if (controller == null || !controller.value.isInitialized) return;
    try {
      await controller.setFocusPoint(Offset(x, y));
      await controller.setExposurePoint(Offset(x, y));
    } on Object {
      // Tap-to-focus is a convenience; hardware without it still captures.
    }
  }

  Future<void> setTorch(bool on) async {
    final controller = _controller;
    if (controller == null || !controller.value.isInitialized) return;
    try {
      await controller.setFlashMode(on ? FlashMode.torch : FlashMode.off);
      _torchOn = on;
    } on Object {
      _torchOn = false;
    }
  }

  /// Takes a frame. The returned file lives in the plugin's temporary
  /// directory; the caller is responsible for moving its bytes into the
  /// evidence tree before anything else happens to it.
  Future<XFile> takePicture() {
    final controller = _controller;
    if (controller == null || !controller.value.isInitialized) {
      throw StateError('Camera is not ready.');
    }
    return controller.takePicture();
  }

  Future<void> dispose() async {
    final controller = _controller;
    _controller = null;
    _capabilities = null;
    if (controller != null) {
      try {
        await controller.dispose();
      } on Object {
        // Nothing useful to do if teardown fails.
      }
    }
  }
}
