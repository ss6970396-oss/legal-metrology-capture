import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;

import 'upload_transport.dart';

/// Posts one capture to an HTTP endpoint as `multipart/form-data`.
///
/// Wire format, so the server side has something concrete to implement
/// against:
///
/// ```
/// POST {baseUri}/captures
/// Content-Type: multipart/form-data
///
///   image     — the JPEG, exactly the bytes the camera produced
///   metadata  — the JSON bundle, as an application/json part
/// ```
///
/// Two headers carry identity out of band so a server can deduplicate without
/// parsing the body: `X-Capture-Id` and `X-Inspection-Id`. Retries reuse the
/// same capture ID, which makes the request idempotent as long as the server
/// keys on it — and it should, because the client-minted ID is the primary key
/// by design.
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

  Uri get _endpoint => baseUri.replace(
        pathSegments: <String>[
          ...baseUri.pathSegments.where((s) => s.isNotEmpty),
          'captures',
        ],
      );

  @override
  String get description => 'HTTP multipart → $_endpoint';

  @override
  Future<UploadOutcome> upload({
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

    try {
      final request = http.MultipartRequest('POST', _endpoint)
        ..headers.addAll(<String, String>{
          if (authorizationToken != null)
            'Authorization': 'Bearer $authorizationToken',
          'X-Capture-Id': '${ids['captureId'] ?? ''}',
          'X-Inspection-Id': '${ids['inspectionId'] ?? ''}',
        })
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
      return UploadRetryable('No network connection (${error.osError?.message ?? error.message}).');
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
