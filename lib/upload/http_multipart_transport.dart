import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;

import 'upload_transport.dart';

/// Talks to the extraction backend over HTTP.
///
/// Wire format, so the server side has something concrete to implement
/// against:
///
/// ```
/// POST {baseUri}/capture-sessions
/// Content-Type: application/json
///   the capture-session envelope (context, client, package id)
///
/// POST {baseUri}/artifacts
/// Content-Type: multipart/form-data
///   image     — the JPEG, exactly the bytes the camera produced
///   metadata  — the per-artifact JSON bundle, as an application/json part
///
/// POST {baseUri}/extraction-jobs
/// Content-Type: application/json
///   the job payload (session id, image manifest, coverage)
/// ```
///
/// Identity travels in headers as well as the body so a server can deduplicate
/// without parsing anything: `X-Capture-Session-Id` on every request, plus
/// `X-Artifact-Id` on an artifact post. Retries reuse the same identifiers,
/// which makes every one of these requests idempotent as long as the server
/// keys on them — and it should, because the client-minted IDs are the primary
/// keys by design.
///
/// `GET /extraction-jobs/{id}/snapshot` is part of the backend contract but is
/// deliberately not implemented here. See [UploadTransport.submitExtractionJob]
/// for why the capture app does not read snapshots back.
class HttpMultipartUploadTransport implements UploadTransport {
  HttpMultipartUploadTransport({
    required this.baseUri,
    this.authorizationToken,
    this.timeout = const Duration(seconds: 60),
    http.Client? client,
  }) : _client = client ?? http.Client();

  /// Root of the evidence API, e.g. `https://example.org/api/v1`.
  final Uri baseUri;

  /// Optional bearer token. Left nullable because the auth scheme is not
  /// settled; wire it to whatever the real backend expects.
  final String? authorizationToken;

  final Duration timeout;
  final http.Client _client;

  Uri _endpoint(String segment) => baseUri.replace(
        pathSegments: <String>[
          ...baseUri.pathSegments.where((s) => s.isNotEmpty),
          segment,
        ],
      );

  @override
  String get description => 'Extraction API → $baseUri';

  Map<String, String> _headers({
    String? captureSessionId,
    String? artifactId,
    String? contentType,
  }) {
    final headers = <String, String>{};
    final token = authorizationToken;
    if (token != null) headers['Authorization'] = 'Bearer $token';
    if (contentType != null) headers['Content-Type'] = contentType;
    if (captureSessionId != null) {
      headers['X-Capture-Session-Id'] = captureSessionId;
    }
    if (artifactId != null) headers['X-Artifact-Id'] = artifactId;
    return headers;
  }

  @override
  Future<UploadOutcome> createCaptureSession(
    Map<String, dynamic> payload,
  ) =>
      _postJson(
        _endpoint('capture-sessions'),
        payload,
        captureSessionId: '${payload['capture_session_id'] ?? ''}',
      );

  @override
  Future<UploadOutcome> submitExtractionJob(Map<String, dynamic> payload) =>
      _postJson(
        _endpoint('extraction-jobs'),
        payload,
        captureSessionId: '${payload['capture_session_id'] ?? ''}',
      );

  Future<UploadOutcome> _postJson(
    Uri endpoint,
    Map<String, dynamic> payload, {
    required String captureSessionId,
  }) async {
    try {
      final response = await _client
          .post(
            endpoint,
            headers: _headers(
              captureSessionId: captureSessionId,
              contentType: 'application/json; charset=utf-8',
            ),
            body: jsonEncode(payload),
          )
          .timeout(timeout);
      return _interpret(response);
    } on SocketException catch (error) {
      return UploadRetryable(
        'No network connection (${error.osError?.message ?? error.message}).',
      );
    } on http.ClientException catch (error) {
      return UploadRetryable('Connection failed: ${error.message}');
    } on Object catch (error) {
      return UploadRetryable('Request failed: $error');
    }
  }

  @override
  Future<UploadOutcome> uploadArtifact({
    required File image,
    required Map<String, dynamic> metadata,
  }) async {
    if (!await image.exists()) {
      // The queue holds a path to a file that is gone. Retrying cannot bring
      // it back, so surface it rather than looping forever.
      return const UploadPermanentlyFailed(
        'The image file is missing from local storage.',
      );
    }

    final ids = (metadata['ids'] as Map?)?.cast<String, dynamic>() ??
        const <String, dynamic>{};
    final artifactId = '${ids['artifactId'] ?? ''}';
    final sessionId = '${ids['captureSessionId'] ?? ''}';

    try {
      final request = http.MultipartRequest('POST', _endpoint('artifacts'))
        ..headers.addAll(
          _headers(captureSessionId: sessionId, artifactId: artifactId),
        )
        ..files.add(
          await http.MultipartFile.fromPath(
            'image',
            image.path,
            filename: '${ids['captureId'] ?? 'capture'}.jpg',
          ),
        )
        ..files.add(
          http.MultipartFile.fromString(
            'metadata',
            jsonEncode(metadata),
            filename: 'metadata.json',
          ),
        );

      final streamed = await _client.send(request).timeout(timeout);
      final response = await http.Response.fromStream(streamed);
      return _interpret(response);
    } on SocketException catch (error) {
      // No network. The expected case out in the field, and squarely
      // retryable — this is exactly why the queue exists.
      return UploadRetryable(
        'No network connection (${error.osError?.message ?? error.message}).',
      );
    } on http.ClientException catch (error) {
      return UploadRetryable('Connection failed: ${error.message}');
    } on Object catch (error) {
      return UploadRetryable('Upload attempt failed: $error');
    }
  }

  UploadOutcome _interpret(http.Response response) {
    final status = response.statusCode;

    if (status >= 200 && status < 300) {
      String? reference;
      try {
        final decoded = jsonDecode(response.body);
        if (decoded is Map) {
          reference = (decoded['reference'] ?? decoded['id'])?.toString();
        }
      } on FormatException {
        // A 2xx with an unparseable body still means the server took it.
      }
      return UploadSucceeded(serverReference: reference);
    }

    // 409 is *not* treated as a disguised success, though the idempotency
    // contract makes that tempting. This backend satisfies idempotency in the
    // 2xx range — a repeated capture-session post returns 201 with
    // `already_existed` — and reserves 409 for the cases where the server and
    // the device genuinely disagree: an artifact posted against a session the
    // server has no record of, or an artifact ID already stored under a
    // different hash. Reporting either as delivered would mark evidence
    // uploaded that no one holds but this phone, which is the one failure this
    // queue exists to prevent.
    //
    // It is permanent rather than retryable because the queue only attempts an
    // artifact after its session task has succeeded; if the server still
    // denies the session, waiting will not change that. The image stays on
    // disk and the task stays visible for a person to deal with.
    if (status == 409) {
      return UploadPermanentlyFailed(
        'The server rejected this as conflicting with what it already holds '
        '(HTTP 409): ${_truncate(response.body, 200)}',
      );
    }

    // Rate limiting and request timeouts are explicitly worth retrying even
    // though they sit in the 4xx range.
    if (status == 408 || status == 429) {
      return UploadRetryable('Server asked us to slow down (HTTP $status).');
    }

    if (status >= 500) {
      return UploadRetryable('Server error (HTTP $status).');
    }

    if (status == 401 || status == 403) {
      return UploadPermanentlyFailed(
        'Upload was rejected as unauthorised (HTTP $status). Credentials need '
        'attention — the evidence is safe on the device.',
      );
    }

    return UploadPermanentlyFailed(
      'Server rejected the upload (HTTP $status): '
      '${_truncate(response.body, 200)}',
    );
  }

  static String _truncate(String value, int max) =>
      value.length <= max ? value : '${value.substring(0, max)}…';

  void close() => _client.close();
}
