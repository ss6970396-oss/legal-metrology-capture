import 'dart:async';
import 'dart:io';
import 'dart:math';

/// What happened to one upload attempt.
///
/// The retryable/permanent split is the transport's job, not the queue's. Only
/// the transport knows whether a 409 from this particular server means "you
/// already sent this, stop" or "try again". The queue just honours the answer.
sealed class UploadOutcome {
  const UploadOutcome();
}

class UploadSucceeded extends UploadOutcome {
  const UploadSucceeded({this.serverReference});

  /// Any additional identifier the server assigned. The server is expected to
  /// keep the client's own capture ID as the primary key regardless.
  final String? serverReference;
}

/// A failure worth trying again: no network, a timeout, a 5xx, a 429.
class UploadRetryable extends UploadOutcome {
  const UploadRetryable(this.reason);
  final String reason;
}

/// A failure retrying cannot fix: a malformed request, a rejected credential,
/// a server that refuses this payload outright. The file stays on disk and the
/// task stays visible so a person can deal with it.
class UploadPermanentlyFailed extends UploadOutcome {
  const UploadPermanentlyFailed(this.reason);
  final String reason;
}

/// The seam between this module and the extraction backend.
///
/// Three operations, matching the three steps of the Team 1 input contract:
/// open a capture session for a package, put each accepted image into it, then
/// ask for the set to be extracted. They are separate calls rather than one
/// bulk post because they become due at different times — the session as soon
/// as the inspector has declared the package context, each artifact the moment
/// it is accepted, the job only once the package is finished — and because an
/// inspector working a shop floor may be offline across all three.
///
/// Ordering is the queue's responsibility, not the transport's. Implementations
/// may assume the queue will not call [uploadArtifact] for a session that was
/// never created, nor [submitExtractionJob] before its artifacts landed.
abstract class UploadTransport {
  /// Opens a capture session for one package. Must be idempotent on
  /// `capture_session_id`: the queue retries, and a retry after a response the
  /// device never saw is indistinguishable from a first attempt.
  Future<UploadOutcome> createCaptureSession(Map<String, dynamic> payload);

  /// Sends one image and its metadata bundle. Implementations must not throw;
  /// a failure is an [UploadOutcome], because the queue has to be able to
  /// record and reschedule it. Idempotent on `artifact_id`.
  Future<UploadOutcome> uploadArtifact({
    required File image,
    required Map<String, dynamic> metadata,
  });

  /// Asks the backend to extract facts from the session's artifacts.
  ///
  /// Returns as soon as the job is accepted. The snapshot is not fetched here
  /// and deliberately so: `PackageFactSnapshot` is produced for the compliance
  /// engine, not for the capture app, and an inspector's phone showing
  /// extracted facts would invite exactly the reading — "the app says the MRP
  /// is fine" — that the whole boundary exists to prevent.
  Future<UploadOutcome> submitExtractionJob(Map<String, dynamic> payload);

  /// Human-readable description of where uploads are going, shown on the
  /// queue screen so an inspector can tell a real backend from a stub.
  String get description;
}

/// In-memory transport for development and tests.
///
/// It exists so the queue, its backoff, its ordering constraints and the UI
/// can be exercised end to end with no server: it simulates latency and, at
/// [failureRate], transient failures, so the retry path is something you can
/// actually watch happen rather than something you hope works.
class FakeUploadTransport implements UploadTransport {
  FakeUploadTransport({
    this.latency = const Duration(milliseconds: 600),
    this.failureRate = 0.0,
    Random? random,
  }) : _random = random ?? Random();

  final Duration latency;

  /// 0.0 never fails, 1.0 always fails with a retryable error.
  final double failureRate;

  final Random _random;

  /// Artifact bundles this transport was handed, in order. Useful in tests.
  final List<Map<String, dynamic>> sent = <Map<String, dynamic>>[];

  /// Capture-session payloads, keyed by `capture_session_id`. A map rather
  /// than a list so a test can assert the idempotency the contract requires.
  final Map<String, Map<String, dynamic>> sessions =
      <String, Map<String, dynamic>>{};

  /// Extraction jobs submitted, in order.
  final List<Map<String, dynamic>> jobs = <Map<String, dynamic>>[];

  @override
  String get description =>
      'Local stub — nothing leaves the device (development only)';

  @override
  Future<UploadOutcome> createCaptureSession(
    Map<String, dynamic> payload,
  ) async {
    await Future<void>.delayed(latency);
    if (_shouldFail()) {
      return const UploadRetryable('Simulated network failure.');
    }
    final id = '${payload['capture_session_id']}';
    sessions[id] = payload;
    return UploadSucceeded(serverReference: 'stub-session-$id');
  }

  @override
  Future<UploadOutcome> uploadArtifact({
    required File image,
    required Map<String, dynamic> metadata,
  }) async {
    await Future<void>.delayed(latency);

    if (!await image.exists()) {
      return const UploadPermanentlyFailed(
        'The image file is missing from local storage.',
      );
    }

    if (_shouldFail()) {
      return const UploadRetryable('Simulated network failure.');
    }

    sent.add(metadata);
    return UploadSucceeded(
      serverReference: 'stub-${metadata['ids']?['captureId'] ?? 'unknown'}',
    );
  }

  @override
  Future<UploadOutcome> submitExtractionJob(
    Map<String, dynamic> payload,
  ) async {
    await Future<void>.delayed(latency);
    if (_shouldFail()) {
      return const UploadRetryable('Simulated network failure.');
    }
    jobs.add(payload);
    return UploadSucceeded(
      serverReference: 'stub-job-${payload['capture_session_id']}',
    );
  }

  bool _shouldFail() => failureRate > 0 && _random.nextDouble() < failureRate;
}
