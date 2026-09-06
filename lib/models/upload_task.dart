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

/// Which step of the input contract a task performs.
///
/// The three are ordered, and the queue enforces that ordering: a package's
/// session must exist before its artifacts can be attributed to it, and every
/// artifact must have landed before asking for the set to be extracted. An
/// extraction run over a partially arrived set would resolve fields against
/// evidence that was merely late, which is precisely the kind of silent wrong
/// answer the whole architecture is built to avoid.
enum UploadTaskKind {
  /// Opens the capture session and registers the declared context.
  captureSession,

  /// Sends one accepted image and its metadata bundle.
  artifact,

  /// Asks the backend to extract facts from the session's artifacts.
  extractionJob,
}

extension UploadTaskKindX on UploadTaskKind {
  String get label => switch (this) {
        UploadTaskKind.captureSession => 'Open capture session',
        UploadTaskKind.artifact => 'Image',
        UploadTaskKind.extractionJob => 'Request extraction',
      };

  /// Position in the contract sequence. Lower runs first.
  int get order => switch (this) {
        UploadTaskKind.captureSession => 0,
        UploadTaskKind.artifact => 1,
        UploadTaskKind.extractionJob => 2,
      };

  static UploadTaskKind parse(Object? value) =>
      UploadTaskKind.values.firstWhere(
        (v) => v.name == value,
        // Queue files written before the contract reshape hold only artifact
        // uploads, and that is what an entry with no `kind` must decode as.
        orElse: () => UploadTaskKind.artifact,
      );
}

/// One contract call, waiting to reach the server.
///
/// The queue is durable across app restarts, which is the whole point: an
/// inspector finishes a day's visits on a phone with no signal, and the
/// evidence uploads itself when the phone next sees a network — without
/// anyone having to remember to reopen a screen.
class UploadTask {
  UploadTask({
    required this.taskId,
    required this.kind,
    required this.inspectionId,
    required this.productSessionId,
    required this.payload,
    required this.createdAtUtc,
    this.captureId = '',
    this.localPath = '',
    this.status = UploadStatus.queued,
    this.attemptCount = 0,
    this.nextAttemptAtUtc,
    this.lastError,
    this.completedAtUtc,
    this.serverReference,
  });

  final String taskId;
  final UploadTaskKind kind;
  final String inspectionId;
  final String productSessionId;

  /// Empty for session and job tasks, which are about a package rather than
  /// about one photograph.
  final String captureId;

  /// Empty for anything but an artifact upload.
  final String localPath;

  /// The JSON body for this call: the session envelope, the per-artifact
  /// metadata bundle, or the extraction job.
  final Map<String, dynamic> payload;

  final DateTime createdAtUtc;

  UploadStatus status;
  int attemptCount;
  DateTime? nextAttemptAtUtc;
  String? lastError;
  DateTime? completedAtUtc;

  /// Identifier the server returned. The server is expected to store the
  /// client's own identifiers as-is; this is any additional reference of its
  /// own.
  String? serverReference;

  /// Whether the backoff has elapsed. Says nothing about whether this task's
  /// prerequisites have landed — that is [UploadQueue]'s call, because only
  /// the queue can see the other tasks.
  bool isDue(DateTime now) {
    if (status != UploadStatus.queued) return false;
    final next = nextAttemptAtUtc;
    return next == null || !next.isAfter(now);
  }

  /// Description for the queue screen.
  String get label => switch (kind) {
        UploadTaskKind.captureSession => 'Capture session',
        UploadTaskKind.artifact =>
          '${payload['surface']?['surfaceLabel'] ?? 'Image'}',
        UploadTaskKind.extractionJob => 'Extraction request',
      };

  Map<String, dynamic> toJson() => <String, dynamic>{
        'taskId': taskId,
        'kind': kind.name,
        'captureId': captureId,
        'inspectionId': inspectionId,
        'productSessionId': productSessionId,
        'localPath': localPath,
        'payload': payload,
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
        kind: UploadTaskKindX.parse(json['kind']),
        captureId: json['captureId'] as String? ?? '',
        inspectionId: json['inspectionId'] as String? ?? '',
        productSessionId: json['productSessionId'] as String? ?? '',
        localPath: json['localPath'] as String? ?? '',
        // `metadata` is the pre-reshape field name. Reading it keeps a queue
        // that was persisted by an older build drainable across the upgrade
        // rather than stranding evidence that is already on disk.
        payload: (json['payload'] as Map?)?.cast<String, dynamic>() ??
            (json['metadata'] as Map?)?.cast<String, dynamic>() ??
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
