import 'dart:async';

import 'package:flutter/widgets.dart';

import '../capture/camera_service.dart';
import '../capture/capture_service.dart';
import '../core/app_paths.dart';
import '../core/device_identity.dart';
import '../core/ids.dart';
import '../core/json_store.dart';
import '../models/barcode_scan.dart';
import '../models/capture_record.dart';
import '../models/inspection_session.dart';
import '../models/product_session.dart';
import '../models/quality_report.dart';
import '../models/surface_step.dart';
import '../quality/thresholds.dart';
import '../upload/metadata_bundle.dart';
import '../upload/upload_queue.dart';
import '../upload/upload_transport.dart';

/// Owns the inspection in progress and every service it needs.
///
/// State lives here rather than in the widgets because a capture flow is a
/// sequence an inspector walks away from and comes back to — a phone call
/// mid-visit, the screen locking in a cold aisle — and the flow's position has
/// to outlive any individual screen.
class InspectionController extends ChangeNotifier {
  InspectionController({
    required this.uploadQueue,
    CameraService? cameraService,
    CaptureService? captureService,
    this.store = const JsonStore(),
  }) : cameraService = cameraService ?? CameraService() {
    _captureService = captureService ??
        CaptureService(cameraService: this.cameraService);
  }

  final UploadQueue uploadQueue;
  final CameraService cameraService;
  final JsonStore store;
  late final CaptureService _captureService;

  InspectionSession? _inspection;
  ProductSession? _product;

  InspectionSession? get inspection => _inspection;
  ProductSession? get product => _product;

  bool get hasActiveInspection => _inspection != null;
  bool get hasActiveProduct => _product != null;

  /// Boots storage, identity and the upload queue. Everything here works
  /// offline; nothing blocks on a network call.
  static Future<InspectionController> bootstrap({
    UploadTransport? transport,
  }) async {
    await AppPaths.ensureInitialized();
    await DeviceIdentity.load();

    final queue = UploadQueue(
      transport: transport ?? FakeUploadTransport(),
    );
    await queue.load();
    queue.start();

    return InspectionController(uploadQueue: queue);
  }

  // --- Session lifecycle -------------------------------------------------

  /// Starts a visit. The identifier is minted here, on the device, before
  /// anything else happens — there is no point in the flow where the app waits
  /// on a server to tell it what this inspection is called.
  InspectionSession startInspection({String? premisesLabel}) {
    final session = InspectionSession.start(
      inspectionId: Ids.inspection(),
      deviceId: DeviceIdentity.current.deviceId,
    )..premisesLabel = premisesLabel;
    _inspection = session;
    _product = null;
    notifyListeners();
    unawaited(_persist());
    return session;
  }

  /// Starts work on one package within the visit.
  ProductSession startProductSession() {
    final inspection = _requireInspection();
    final session = ProductSession.start(
      inspectionId: inspection.inspectionId,
      productSessionId: Ids.productSession(),
    );
    inspection.products.add(session);
    _product = session;
    notifyListeners();
    unawaited(_persist());
    return session;
  }

  void openProductSession(String productSessionId) {
    final found = _inspection?.productById(productSessionId);
    if (found == null) return;
    _product = found;
    notifyListeners();
  }

  void closeProductSession() {
    final product = _product;
    if (product != null && product.isComplete) {
      product.closedAtUtc = DateTime.now().toUtc();
    }
    _product = null;
    notifyListeners();
    unawaited(_persist());
  }

  void closeInspection() {
    _inspection?.closedAtUtc = DateTime.now().toUtc();
    _product = null;
    notifyListeners();
    unawaited(_persist());
  }

  void setPremisesLabel(String value) {
    _inspection?.premisesLabel = value.trim().isEmpty ? null : value.trim();
    notifyListeners();
    unawaited(_persist());
  }

  void setProductLabel(String value) {
    _product?.productLabel = value.trim().isEmpty ? null : value.trim();
    notifyListeners();
    unawaited(_persist());
  }

  // --- Capture -----------------------------------------------------------

  /// Takes a frame for [step], analyses it, and records it as a pending
  /// attempt. The image is on disk before this returns, whatever the inspector
  /// decides next.
  Future<CaptureRecord> captureSurface(SurfaceStepState step) async {
    final product = _requireProduct();
    final record = await _captureService.capture(product: product, step: step);
    step.attempts.add(record);
    notifyListeners();
    await _persist();
    return record;
  }

  /// Whether the inspector is allowed to accept this frame.
  ///
  /// A frame that failed a quality check cannot be accepted while retakes
  /// remain. Once the allowance is spent the inspector may proceed — with the
  /// surface flagged — because an inspector standing in front of a package
  /// that will not photograph well must not be trapped in a loop they cannot
  /// exit. What they must not be able to do is make the problem invisible.
  bool canAccept(SurfaceStepState step, CaptureRecord record) {
    if (record.quality.overall != QualityVerdict.fail) return true;
    return step.failedAttemptCount >= QualityThresholds.maxFailedAttempts;
  }

  /// Why acceptance is blocked, in words the inspector can act on. Null when
  /// acceptance is available.
  String? acceptanceBlockedReason(
    SurfaceStepState step,
    CaptureRecord record,
  ) {
    if (canAccept(step, record)) return null;
    final used = step.failedAttemptCount;
    final total = QualityThresholds.maxFailedAttempts;
    return 'Attempt $used of $total failed its quality checks. Retake this '
        'surface. After $total failed attempts you can proceed with the '
        'surface flagged for manual review.';
  }

  /// Accepts a frame as the evidence for its surface and queues it for upload.
  Future<void> acceptCapture(
    SurfaceStepState step,
    CaptureRecord record,
  ) async {
    final inspection = _requireInspection();
    final product = _requireProduct();

    final failed = record.quality.overall == QualityVerdict.fail;
    final accepted = record.copyWith(
      disposition: failed
          ? CaptureDisposition.acceptedUnderManualReview
          : CaptureDisposition.accepted,
    );

    final index =
        step.attempts.indexWhere((a) => a.captureId == record.captureId);
    if (index >= 0) {
      step.attempts[index] = accepted;
    } else {
      step.attempts.add(accepted);
    }
    step.acceptedCaptureId = accepted.captureId;
    // Accepting a capture clears any earlier "not accessible" mark: the
    // surface demonstrably was accessible.
    step.skip = null;

    notifyListeners();
    await _persist();

    await uploadQueue.enqueue(
      capture: accepted,
      metadata: MetadataBundle.build(
        inspection: inspection,
        product: product,
        step: step,
        capture: accepted,
      ),
    );
  }

  /// Marks a frame as superseded. The file and its scores stay on disk; the
  /// attempt history is part of the record.
  Future<void> markRetaken(SurfaceStepState step, CaptureRecord record) async {
    final index =
        step.attempts.indexWhere((a) => a.captureId == record.captureId);
    if (index >= 0) {
      step.attempts[index] =
          record.copyWith(disposition: CaptureDisposition.retaken);
    }
    if (step.acceptedCaptureId == record.captureId) {
      step.acceptedCaptureId = null;
    }
    notifyListeners();
    await _persist();
  }

  // --- Skipping and extra angles ----------------------------------------

  /// Records that a surface could not be photographed, and why.
  Future<void> skipStep(
    SurfaceStepState step, {
    required SkipReason reason,
    String? note,
  }) async {
    step.skip = StepSkip(
      reason: reason,
      note: note,
      recordedAtUtc: DateTime.now().toUtc(),
    );
    step.acceptedCaptureId = null;
    notifyListeners();
    await _persist();
  }

  Future<void> clearSkip(SurfaceStepState step) async {
    step.skip = null;
    notifyListeners();
    await _persist();
  }

  /// Adds an optional extra angle to the end of the sequence.
  Future<SurfaceStepState> addExtraAngle() async {
    final product = _requireProduct();
    final existing =
        product.steps.where((s) => s.definition.userAdded).length;
    final step = SurfaceStepState(
      definition: SurfaceStepDefinition.extraAngle(existing + 1),
    );
    product.steps.add(step);
    notifyListeners();
    await _persist();
    return step;
  }

  // --- Identification ----------------------------------------------------

  /// Files a barcode read against the current package.
  ///
  /// This records what the product is. It contributes nothing to surface
  /// coverage and nothing to any compliance determination — see [BarcodeScan].
  Future<void> setIdentification(BarcodeScan scan) async {
    _requireProduct().identification = scan;
    notifyListeners();
    await _persist();
  }

  Future<void> clearIdentification() async {
    _requireProduct().identification = null;
    notifyListeners();
    await _persist();
  }

  // --- Persistence -------------------------------------------------------

  /// Serialises manifest writes so they cannot interleave.
  Future<void> _writeChain = Future<void>.value();

  /// The last storage failure, if any. Surfaced rather than swallowed: the
  /// images are the durable evidence and they are already on disk, but a
  /// manifest that stopped updating is something someone needs to know about.
  String? _lastStorageError;
  String? get lastStorageError => _lastStorageError;

  Future<void> _persist() {
    final inspection = _inspection;
    if (inspection == null) return Future<void>.value();

    // Snapshot now, write later. The chain may not run for a few event-loop
    // turns, by which time the session could have moved on — serialising the
    // *current* state here is what keeps the file consistent with the moment
    // the change happened, and keeps a slow earlier write from overwriting a
    // newer one with stale JSON.
    final snapshot = inspection.toJson();
    final target = AppPaths.instance.inspectionManifest(inspection.inspectionId);

    _writeChain = _writeChain.then((_) async {
      try {
        await store.write(target, snapshot);
        _lastStorageError = null;
      } on Object catch (error) {
        // Recorded, not rethrown: a failed index write must not take down an
        // inspection in progress, and the chain has to survive for the next
        // change to have a chance of landing.
        _lastStorageError = '$error';
      }
    });
    return _writeChain;
  }

  InspectionSession _requireInspection() {
    final value = _inspection;
    if (value == null) {
      throw StateError('No inspection has been started.');
    }
    return value;
  }

  ProductSession _requireProduct() {
    final value = _product;
    if (value == null) {
      throw StateError('No product session is open.');
    }
    return value;
  }

  @override
  void dispose() {
    unawaited(cameraService.dispose());
    unawaited(_captureService.dispose());
    super.dispose();
  }
}

/// Makes the controller available to the widget tree and rebuilds dependents
/// when it changes. An [InheritedNotifier] is all this app needs; a full DI
/// container would be more machinery than one capture flow justifies.
class InspectionScope extends InheritedNotifier<InspectionController> {
  const InspectionScope({
    super.key,
    required InspectionController controller,
    required super.child,
  }) : super(notifier: controller);

  static InspectionController of(BuildContext context) {
    final scope =
        context.dependOnInheritedWidgetOfExactType<InspectionScope>();
    assert(scope != null, 'No InspectionScope found in the widget tree.');
    return scope!.notifier!;
  }

  /// Reads the controller without subscribing to rebuilds — for event
  /// handlers, where a dependency would be pointless churn.
  static InspectionController read(BuildContext context) {
    final scope =
        context.getInheritedWidgetOfExactType<InspectionScope>();
    assert(scope != null, 'No InspectionScope found in the widget tree.');
    return scope!.notifier!;
  }
}
