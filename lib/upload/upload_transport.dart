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

/// The seam between this module and whatever ends up receiving the evidence.
///
/// Everything above this interface — capture, quality checks, the durable
/// queue, backoff — is finished and does not care what is on the other side.
abstract class UploadTransport {
  /// Sends one image and its metadata bundle. Implementations must not throw;
  /// a failure is an [UploadOutcome], because the queue has to be able to
  /// record and reschedule it.
  Future<UploadOutcome> upload({
    required File image,
    required Map<String, dynamic> metadata,
  });

  /// Human-readable description of where uploads are going, shown on the
  /// queue screen so an inspector can tell a real backend from a stub.
  String get description;
}

/// In-memory transport for development and tests.
///
/// It exists so the queue, its backoff, and the UI can be exercised end to end
/// with no server: it simulates latency and, at [failureRate], transient
/// failures, so the retry path is something you can actually watch happen
/// rather than something you hope works.
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

  /// Bundles this transport was handed, in order. Useful in tests.
  final List<Map<String, dynamic>> sent = <Map<String, dynamic>>[];

  @override
  String get description =>
      'Local stub — nothing leaves the device (development only)';

  @override
  Future<UploadOutcome> upload({
    required File image,
    required Map<String, dynamic> metadata,
  }) async {
    await Future<void>.delayed(latency);

    if (!await image.exists()) {
      return const UploadPermanentlyFailed(
        'The image file is missing from local storage.',
      );
    }

    if (failureRate > 0 && _random.nextDouble() < failureRate) {
      return const UploadRetryable('Simulated network failure.');
    }

    sent.add(metadata);
    return UploadSucceeded(
      serverReference: 'stub-${metadata['ids']?['captureId'] ?? 'unknown'}',
    );
  }
}
