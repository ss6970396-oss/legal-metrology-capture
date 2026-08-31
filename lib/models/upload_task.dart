import '../core/json_store.dart';

enum UploadStatus {
  /// Waiting for its turn, or waiting out a backoff delay.
  queued,

  /// Currently on the wire.
  inFlight,

  /// The server acknowledged it.
  succeeded,

  /// The server rejected it in a way retrying will not fix. Needs a person.
  failedPermanent,
}

extension UploadStatusX on UploadStatus {
  String get label => switch (this) {
        UploadStatus.queued => 'Queued',
        UploadStatus.inFlight => 'Uploading',
        UploadStatus.succeeded => 'Uploaded',
        UploadStatus.failedPermanent => 'Rejected — needs attention',
      };

  bool get isTerminal =>
      this == UploadStatus.succeeded || this == UploadStatus.failedPermanent;

  static UploadStatus parse(Object? value) => UploadStatus.values.firstWhere(
        (v) => v.name == value,
        orElse: () => UploadStatus.queued,
      );
}

/// One image and its metadata bundle, waiting to reach the server.
///
/// The queue is durable across app restarts, which is the whole point: an
/// inspector finishes a day's visits on a phone with no signal, and the
/// evidence uploads itself when the phone next sees a network — without
/// anyone having to remember to reopen a screen.
class UploadTask {
  UploadTask({
    required this.taskId,
    required this.captureId,
    required this.inspectionId,
    required this.productSessionId,
    required this.localPath,
    required this.metadata,
    required this.createdAtUtc,
    this.status = UploadStatus.queued,
    this.attemptCount = 0,
    this.nextAttemptAtUtc,
    this.lastError,
    this.completedAtUtc,
    this.serverReference,
  });

  final String taskId;
  final String captureId;
  final String inspectionId;
  final String productSessionId;
  final String localPath;

  /// The full bundle sent alongside the image bytes.
  final Map<String, dynamic> metadata;

  final DateTime createdAtUtc;

  UploadStatus status;
  int attemptCount;
  DateTime? nextAttemptAtUtc;
  String? lastError;
  DateTime? completedAtUtc;

  /// Identifier the server returned. The server is expected to store the
  /// client's [captureId] as-is; this is any additional reference of its own.
  String? serverReference;

  bool isDue(DateTime now) {
    if (status != UploadStatus.queued) return false;
    final next = nextAttemptAtUtc;
    return next == null || !next.isAfter(now);
  }

  Map<String, dynamic> toJson() => <String, dynamic>{
        'taskId': taskId,
        'captureId': captureId,
        'inspectionId': inspectionId,
        'productSessionId': productSessionId,
        'localPath': localPath,
        'metadata': metadata,
        'createdAtUtc': encodeTime(createdAtUtc),
        'status': status.name,
        'attemptCount': attemptCount,
        'nextAttemptAtUtc':
            nextAttemptAtUtc == null ? null : encodeTime(nextAttemptAtUtc!),
        'lastError': lastError,
        'completedAtUtc':
            completedAtUtc == null ? null : encodeTime(completedAtUtc!),
        'serverReference': serverReference,
      };

  factory UploadTask.fromJson(Map<String, dynamic> json) => UploadTask(
        taskId: json['taskId'] as String? ?? '',
        captureId: json['captureId'] as String? ?? '',
        inspectionId: json['inspectionId'] as String? ?? '',
        productSessionId: json['productSessionId'] as String? ?? '',
        localPath: json['localPath'] as String? ?? '',
        metadata: (json['metadata'] as Map?)?.cast<String, dynamic>() ??
            <String, dynamic>{},
        createdAtUtc: decodeTime(json['createdAtUtc']),
        status: UploadStatusX.parse(json['status']),
        attemptCount: (json['attemptCount'] as num?)?.toInt() ?? 0,
        nextAttemptAtUtc: decodeTimeOrNull(json['nextAttemptAtUtc']),
        lastError: json['lastError'] as String?,
        completedAtUtc: decodeTimeOrNull(json['completedAtUtc']),
        serverReference: json['serverReference'] as String?,
      );
}
