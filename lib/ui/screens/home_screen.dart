import 'package:flutter/material.dart';

import '../../core/device_identity.dart';
import '../../models/product_session.dart';
import '../../state/inspection_controller.dart';
import '../widgets/coverage_indicator.dart';
import 'product_flow_screen.dart';
import 'upload_queue_screen.dart';

/// Entry point of the capture module: start a visit, then work through the
/// packages in it.
class HomeScreen extends StatelessWidget {
  const HomeScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final controller = InspectionScope.of(context);
    final inspection = controller.inspection;

    return Scaffold(
      appBar: AppBar(
        title: const Text('Package inspection'),
        actions: <Widget>[
          IconButton(
            tooltip: 'Upload queue',
            icon: Badge(
              isLabelVisible: controller.uploadQueue.hasOutstandingWork,
              label: Text('${controller.uploadQueue.pendingCount}'),
              child: const Icon(Icons.cloud_upload_outlined),
            ),
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute<void>(
                builder: (_) => const UploadQueueScreen(),
              ),
            ),
          ),
        ],
      ),
      body: inspection == null
          ? const _StartInspectionPanel()
          : const _InspectionPanel(),
      floatingActionButton: const AddPackageButton(),
    );
  }
}

class _StartInspectionPanel extends StatefulWidget {
  const _StartInspectionPanel();

  @override
  State<_StartInspectionPanel> createState() => _StartInspectionPanelState();
}

class _StartInspectionPanelState extends State<_StartInspectionPanel> {
  final TextEditingController _premises = TextEditingController();

  @override
  void dispose() {
    _premises.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final device = DeviceIdentity.current;

    return ListView(
      padding: const EdgeInsets.all(20),
      children: <Widget>[
        Text(
          'Start an inspection',
          style: theme.textTheme.headlineSmall
              ?.copyWith(fontWeight: FontWeight.w700),
        ),
        const SizedBox(height: 8),
        Text(
          'An inspection ID is generated on this device the moment you start, '
          'and every photograph, scan and quality score in the visit carries '
          'it. Nothing here needs a network connection.',
          style: theme.textTheme.bodyMedium?.copyWith(height: 1.4),
        ),
        const SizedBox(height: 22),
        TextField(
          controller: _premises,
          textCapitalization: TextCapitalization.words,
          decoration: const InputDecoration(
            labelText: 'Premises (optional)',
            border: OutlineInputBorder(),
            helperText: 'You can fill this in later — it never blocks capture.',
          ),
        ),
        const SizedBox(height: 18),
        FilledButton.icon(
          onPressed: () {
            InspectionScope.read(context)
                .startInspection(premisesLabel: _premises.text);
          },
          style: FilledButton.styleFrom(
            padding: const EdgeInsets.symmetric(vertical: 15),
          ),
          icon: const Icon(Icons.play_arrow),
          label: const Text('Start inspection'),
        ),
        const SizedBox(height: 28),
        Text(
          'Device ${device.deviceId}\n${device.model} · ${device.osVersion}',
          style: theme.textTheme.bodySmall,
        ),
      ],
    );
  }
}

class _InspectionPanel extends StatelessWidget {
  const _InspectionPanel();

  @override
  Widget build(BuildContext context) {
    final controller = InspectionScope.of(context);
    final inspection = controller.inspection!;
    final theme = Theme.of(context);

    return Column(
      children: <Widget>[
        Container(
          width: double.infinity,
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 14),
          color: theme.colorScheme.surfaceContainerHighest,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text(
                inspection.premisesLabel ?? 'Inspection in progress',
                style: const TextStyle(fontWeight: FontWeight.w700),
              ),
              const SizedBox(height: 3),
              SelectableText(
                'inspectionId ${inspection.inspectionId}',
                style: theme.textTheme.bodySmall,
              ),
              Text(
                'Started ${inspection.startedAtUtc.toLocal()} · '
                '${inspection.products.length} package'
                '${inspection.products.length == 1 ? '' : 's'} · '
                '${inspection.completeProductCount} complete',
                style: theme.textTheme.bodySmall,
              ),
            ],
          ),
        ),
        Expanded(
          child: inspection.products.isEmpty
              ? const Center(
                  child: Padding(
                    padding: EdgeInsets.all(28),
                    child: Text(
                      'No packages yet. Add the first one to begin the '
                      'front / back / side sequence.',
                      textAlign: TextAlign.center,
                    ),
                  ),
                )
              : ListView.builder(
                  padding: const EdgeInsets.fromLTRB(16, 12, 16, 90),
                  itemCount: inspection.products.length,
                  itemBuilder: (context, index) => _ProductCard(
                    product: inspection.products[index],
                    index: index,
                  ),
                ),
        ),
      ],
    );
  }
}

class _ProductCard extends StatelessWidget {
  const _ProductCard({required this.product, required this.index});

  final ProductSession product;
  final int index;

  @override
  Widget build(BuildContext context) {
    final controller = InspectionScope.of(context);
    final theme = Theme.of(context);

    return Card(
      margin: const EdgeInsets.only(bottom: 12),
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: () {
          controller.openProductSession(product.productSessionId);
          Navigator.of(context).push(
            MaterialPageRoute<void>(
              builder: (_) => const ProductFlowScreen(),
            ),
          );
        },
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Row(
                children: <Widget>[
                  Expanded(
                    child: Text(
                      product.productLabel ??
                          product.identification?.rawValue ??
                          'Package ${index + 1}',
                      style: const TextStyle(fontWeight: FontWeight.w600),
                    ),
                  ),
                  if (product.hasManualReviewFlags)
                    const Icon(Icons.flag, size: 18, color: Color(0xFF9A6400)),
                ],
              ),
              const SizedBox(height: 3),
              SelectableText(
                'productSessionId ${product.productSessionId}',
                style: theme.textTheme.bodySmall,
              ),
              const SizedBox(height: 12),
              CoverageIndicator(product: product),
            ],
          ),
        ),
      ),
    );
  }
}

/// The "add a package" action, kept out of [HomeScreen] so it can sit in a
/// floating action button without rebuilding the list.
class AddPackageButton extends StatelessWidget {
  const AddPackageButton({super.key});

  @override
  Widget build(BuildContext context) {
    final controller = InspectionScope.of(context);
    if (!controller.hasActiveInspection) return const SizedBox.shrink();

    return FloatingActionButton.extended(
      onPressed: () {
        controller.startProductSession();
        Navigator.of(context).push(
          MaterialPageRoute<void>(
            builder: (_) => const ProductFlowScreen(),
          ),
        );
      },
      icon: const Icon(Icons.add),
      label: const Text('Add package'),
    );
  }
}
