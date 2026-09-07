import 'http_multipart_transport.dart';
import 'upload_transport.dart';

/// Chooses where evidence is uploaded, from the build environment.
///
/// The destination is a build-time constant rather than a setting, and
/// deliberately: an extraction run has to be traceable to the client that
/// produced it, and a base URL an inspector could edit at runtime would make
/// "which server did this evidence go to" a question with no reliable answer.
/// It moves with the binary, the same way [InspectionController.appVersion]
/// does.
///
/// Override it per build:
///
/// ```
/// flutter run --dart-define=EVIDENCE_API_BASE_URL=http://192.168.1.42:8000/v1
/// flutter run --dart-define=EVIDENCE_API_BASE_URL=stub   # back to the stub
/// ```
///
/// Repeated flags live more comfortably in a JSON file that is not committed:
///
/// ```
/// flutter run --dart-define-from-file=dev-backend.json
/// ```
abstract final class UploadDestination {
  /// Root of the evidence API, including the `/v1` prefix the backend mounts
  /// its router under (see `backend/app/main.py`).
  ///
  /// The default points at a LAN development backend and goes stale whenever
  /// the laptop's address changes — pass `--dart-define` rather than editing
  /// this line, so a machine-specific address never lands in a commit.
  static const String baseUrl = String.fromEnvironment(
    'EVIDENCE_API_BASE_URL',
    defaultValue: 'http://192.168.29.231:8000/v1',
  );

  /// Bearer token, if the deployment wants one. The development backend does
  /// not authenticate, so this is empty by default and no header is sent.
  static const String authorizationToken = String.fromEnvironment(
    'EVIDENCE_API_TOKEN',
  );

  /// Whether this build talks to the in-memory stub instead of a server.
  static bool get usesStub =>
      baseUrl.isEmpty || baseUrl == 'stub' || baseUrl == 'none';

  /// The transport this build uploads through.
  static UploadTransport create() {
    if (usesStub) {
      return FakeUploadTransport(
        // A non-zero failure rate makes the retry and backoff path visible
        // during development rather than theoretical.
        failureRate: 0.25,
      );
    }
    return HttpMultipartUploadTransport(
      baseUri: Uri.parse(baseUrl),
      authorizationToken:
          authorizationToken.isEmpty ? null : authorizationToken,
    );
  }
}
