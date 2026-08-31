import 'dart:async';
import 'dart:io';
import 'dart:math';

import 'package:flutter/foundation.dart';

import '../core/app_paths.dart';
import '../core/ids.dart';
import '../core/json_store.dart';
import '../models/capture_record.dart';
import '../models/upload_task.dart';
import 'upload_transport.dart';

/// Durable, self-draining upload queue.
///
/// The design constraint is that the inspector is never blocked on the
/// network. Capture writes the image to local storage and drops a task here;
/// that is the entire interaction. Whether the upload succeeds in two seconds
/// or two days changes nothing about what the inspector can do next.
///
/// Consequences of taking that seriously:
///
/// * The queue is persisted after every state change, so a task survives the
///   app being killed, the phone being rebooted, or the battery dying in a
///   cold storage aisle.
/// * Retryable failures back off exponentially with jitter and then keep
///   trying, indefinitely. Evidence is not something to give up on because a
///   premises had no signal for an hour. Only a transport-declared permanent
///   failure stops the retries, and even then the task stays visible.
/// * The local image is never deleted, not even after a successful upload.
///   Local storage is cheap; re-photographing a package after the fact is
///   impossible. Cleanup is a decision for a later, explicit retention policy.
class UploadQueue extends ChangeNotifier {
  UploadQueue({
    required this.transport,
    this.store = const JsonStore(),
    this.baseBackoff = const Duration(seconds: 5),
    this.maximumBackoff = const Duration(minutes: 10),
    Random? random,
  }) : _random = random ?? Random();

  /// Current destination. Prefer [useTransport] to change it, so listeners are
  /// notified and any waiting work is rescheduled.
  UploadTransport transport;

  final JsonStore store;
  final Random _random;

  final Duration baseBackoff;
  final Duration maximumBackoff;

  final List<UploadTask> _tasks = <UploadTask>[];
  Timer? _timer;
  bool _draining = false;
  bool _started = false;

  List<UploadTask> get tasks => List<UploadTask>.unmodifiable(_tasks);

  String get transportDescription => transport.description;

  int get pendingCount =>
      _tasks.where((t) => t.status == UploadStatus.queued).length;
  int get inFlightCount =>
      _tasks.where((t) => t.status == UploadStatus.inFlight).length;
  int get succeededCount =>
      _tasks.where((t) => t.status == UploadStatus.succeeded).length;
  int get failedCount =>
      _tasks.where((t) => t.status == UploadStatus.failedPermanent).length;

  bool get hasOutstandingWork => pendingCount > 0 || inFlightCount > 0;

  /// Swaps the destination without disturbing queued work — the tasks are
  /// transport-agnostic, so pointing at a real server later re-sends whatever
  /// the stub never delivered.
  void useTransport(UploadTransport next) {
    transport = next;
    notifyListeners();
    _scheduleNext();
  }

  Future<void> load() async {
    final stored = await store.read(AppPaths.instance.queueFile);
    _tasks
      ..clear()
      ..addAll(
        ((stored?['tasks'] as List?) ?? const [])
            .whereType<Map<String, dynamic>>()
            .map(UploadTask.fromJson),
      );

    // Anything recorded as in-flight was interrupted by the app dying. Put it
    // back in the queue rather than leaving it stuck forever.
    for (final task in _tasks) {
      if (task.status == UploadStatus.inFlight) {
        task.status = UploadStatus.queued;
        task.nextAttemptAtUtc = DateTime.now().toUtc();
      }
    }
    notifyListeners();
  }

  void start() {
    if (_started) return;
    _started = true;
    _scheduleNext();
  }

  Future<UploadTask> enqueue({
    required CaptureRecord capture,
    required Map<String, dynamic> metadata,
  }) async {
    final task = UploadTask(
      taskId: Ids.uploadTask(),
      captureId: capture.captureId,
      inspectionId: capture.inspectionId,
      productSessionId: capture.productSessionId,
      localPath: capture.localPath,
      metadata: metadata,
      createdAtUtc: DateTime.now().toUtc(),
      nextAttemptAtUtc: DateTime.now().toUtc(),
    );
    _tasks.add(task);
    await _persist();
    notifyListeners();
    _scheduleNext();
    return task;
  }

  /// Inspector-triggered nudge from the queue screen: clears the backoff wait
  /// and, for a permanently failed task, gives it one more go.
  void retryNow(String taskId) {
    for (final task in _tasks) {
      if (task.taskId != taskId) continue;
      if (task.status.isTerminal && task.status != UploadStatus.failedPermanent) {
        return;
      }
      task.status = UploadStatus.queued;
      task.nextAttemptAtUtc = DateTime.now().toUtc();
      task.lastError = null;
      notifyListeners();
      _scheduleNext();
      return;
    }
  }

  void retryAllFailed() {
    var changed = false;
    final now = DateTime.now().toUtc();
    for (final task in _tasks) {
      if (task.status != UploadStatus.failedPermanent) continue;
      task.status = UploadStatus.queued;
      task.nextAttemptAtUtc = now;
      task.lastError = null;
      changed = true;
    }
    if (changed) {
      notifyListeners();
      _scheduleNext();
    }
  }

  void _scheduleNext() {
    if (!_started || _draining) return;
    _timer?.cancel();

    final now = DateTime.now().toUtc();
    Duration? soonest;
    for (final task in _tasks) {
      if (task.status != UploadStatus.queued) continue;
      final next = task.nextAttemptAtUtc ?? now;
      final wait = next.isAfter(now) ? next.difference(now) : Duration.zero;
      if (soonest == null || wait < soonest) soonest = wait;
    }

    if (soonest == null) return;
    _timer = Timer(soonest, _drain);
  }

  Future<void> _drain() async {
    if (_draining) return;
    _draining = true;
    try {
      // One at a time. Inspection photographs at full sensor resolution are
      // several megabytes each; parallel uploads on a shop-floor connection
      // mostly produce timeouts.
      while (true) {
        final now = DateTime.now().toUtc();
        UploadTask? due;
        for (final task in _tasks) {
          if (task.isDue(now)) {
            due = task;
            break;
          }
        }
        if (due == null) break;
        await _attempt(due);
      }
    } finally {
      _draining = false;
      _scheduleNext();
    }
  }

  Future<void> _attempt(UploadTask task) async {
    task.status = UploadStatus.inFlight;
    task.attemptCount += 1;
    notifyListeners();
    await _persist();

    final outcome = await transport.upload(
      image: File(task.localPath),
      metadata: task.metadata,
    );

    switch (outcome) {
      case UploadSucceeded(:final serverReference):
        task.status = UploadStatus.succeeded;
        task.completedAtUtc = DateTime.now().toUtc();
        task.serverReference = serverReference;
        task.lastError = null;
        task.nextAttemptAtUtc = null;
      case UploadRetryable(:final reason):
        task.status = UploadStatus.queued;
        task.lastError = reason;
        task.nextAttemptAtUtc =
            DateTime.now().toUtc().add(_backoffFor(task.attemptCount));
      case UploadPermanentlyFailed(:final reason):
        task.status = UploadStatus.failedPermanent;
        task.lastError = reason;
        task.nextAttemptAtUtc = null;
    }

    notifyListeners();
    await _persist();
  }

  /// Exponential backoff with full jitter. The jitter matters more than it
  /// looks: a van full of inspectors regaining signal at the same moment would
  /// otherwise hit the server in lockstep and stay in lockstep through every
  /// retry.
  Duration _backoffFor(int attemptCount) {
    final exponent = (attemptCount - 1).clamp(0, 20);
    final scaled = baseBackoff.inMilliseconds * pow(2, exponent);
    final capped = min(scaled.toDouble(), maximumBackoff.inMilliseconds.toDouble());
    // Full jitter across the whole window, floored so we never busy-loop.
    final jittered = max(1000.0, _random.nextDouble() * capped);
    return Duration(milliseconds: jittered.round());
  }

  Future<void> _persist() async {
    await store.write(AppPaths.instance.queueFile, <String, dynamic>{
      'savedAtUtc': encodeTime(DateTime.now().toUtc()),
      'tasks': _tasks.map((t) => t.toJson()).toList(),
    });
  }

  @override
  void dispose() {
    _timer?.cancel();
    _started = false;
    super.dispose();
  }
}
