import 'package:flutter/material.dart';

import '../../models/upload_task.dart';
import '../../state/inspection_controller.dart';

/// Visibility into the background upload queue.
///
/// Purely informational — nothing here gates capture. It exists so an
/// inspector can answer "has my morning actually reached the office" without
/// having to trust that it did, and so a stuck task is discoverable rather
/// than silent.
class UploadQueueScreen extends StatelessWidget {
  const UploadQueueScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final queue = InspectionScope.of(context).uploadQueue;
    final theme = Theme.of(context);
    final tasks = queue.tasks.reversed.toList();

    return Scaffold(
      appBar: AppBar(
        title: const Text('Upload queue'),
        actions: <Widget>[
          if (queue.failedCount > 0)
            TextButton(
              onPressed: queue.retryAllFailed,
              child: const Text('Retry all'),
            ),
        ],
      ),
      body: Column(
        children: <Widget>[
          Container(
            width: double.infinity,
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 14),
            color: theme.colorScheme.surfaceContainerHighest,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Text(
                  '${queue.pendingCount} waiting · ${queue.inFlightCount} '
                  'uploading · ${queue.succeededCount} sent · '
                  '${queue.failedCount} need attention',
                  style: const TextStyle(fontWeight: FontWeight.w600),
                ),
                const SizedBox(height: 4),
                Text(
                  'Destination: ${queue.transportDescription}',
                  style: theme.textTheme.bodySmall,
                ),
                const SizedBox(height: 4),
                Text(
                  'Uploads run in the background and retry with backoff. '
                  'Images stay on this device whether or not they upload.',
                  style: theme.textTheme.bodySmall,
                ),
              ],
            ),
          ),
          Expanded(
            child: tasks.isEmpty
                ? const Center(child: Text('Nothing queued yet.'))
                : ListView.separated(
                    itemCount: tasks.length,
                    separatorBuilder: (_, _) => const Divider(height: 1),
                    itemBuilder: (context, index) =>
                        _TaskTile(task: tasks[index]),
                  ),
          ),
        ],
      ),
    );
  }
}

class _TaskTile extends StatelessWidget {
  const _TaskTile({required this.task});

  final UploadTask task;

  Color _colour(BuildContext context) => switch (task.status) {
        UploadStatus.succeeded => const Color(0xFF1B7F4B),
        UploadStatus.inFlight => Theme.of(context).colorScheme.primary,
        UploadStatus.failedPermanent => const Color(0xFFB3261E),
        UploadStatus.queued => const Color(0xFF9A6400),
      };

  @override
  Widget build(BuildContext context) {
    final queue = InspectionScope.of(context).uploadQueue;
    final theme = Theme.of(context);
    final surface = (task.metadata['surface'] as Map?)?['surfaceLabel'];

    return ListTile(
      leading: Icon(
        switch (task.status) {
          UploadStatus.succeeded => Icons.cloud_done_outlined,
          UploadStatus.inFlight => Icons.cloud_upload_outlined,
          UploadStatus.failedPermanent => Icons.cloud_off_outlined,
          UploadStatus.queued => Icons.schedule,
        },
        color: _colour(context),
      ),
      title: Text(
        '${surface ?? 'Capture'} · ${task.status.label}',
        style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w600),
      ),
      subtitle: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Text(
            'capture ${_short(task.captureId)} · attempt ${task.attemptCount}',
            style: theme.textTheme.bodySmall,
          ),
          if (task.lastError != null)
            Text(
              task.lastError!,
              style: theme.textTheme.bodySmall
                  ?.copyWith(color: theme.colorScheme.error),
            ),
          if (task.nextAttemptAtUtc != null &&
              task.status == UploadStatus.queued)
            Text(
              'next try ${task.nextAttemptAtUtc!.toLocal()}',
              style: theme.textTheme.bodySmall,
            ),
        ],
      ),
      isThreeLine: task.lastError != null,
      trailing: task.status.isTerminal && task.status != UploadStatus.succeeded
          ? IconButton(
              tooltip: 'Retry now',
              icon: const Icon(Icons.refresh),
              onPressed: () => queue.retryNow(task.taskId),
            )
          : null,
    );
  }

  static String _short(String id) =>
      id.length <= 8 ? id : '${id.substring(0, 8)}…';
}
