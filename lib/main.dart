import 'package:flutter/material.dart';

import 'state/inspection_controller.dart';
import 'ui/screens/home_screen.dart';
import 'upload/upload_destination.dart';

/// Legal Metrology package-inspection capture module.
///
/// This app covers only what happens on the phone before evidence leaves it:
/// session identity, guided capture of the package faces, on-device quality
/// checks, barcode identification, and a durable upload queue.
///
/// The upload destination is chosen in one place — [UploadDestination], which
/// reads it from the build environment. It ships pointed at the extraction
/// backend over HTTP; passing `--dart-define=EVIDENCE_API_BASE_URL=stub`
/// substitutes the local stub that exercises the queue, its retries and its
/// backoff without sending anything anywhere. Nothing else in the module
/// changes either way, and anything already queued against one destination is
/// re-sent to the next — queued tasks are transport-agnostic.
void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  final controller = await InspectionController.bootstrap(
    transport: UploadDestination.create(),
  );

  runApp(CaptureApp(controller: controller));
}

class CaptureApp extends StatelessWidget {
  const CaptureApp({super.key, required this.controller});

  final InspectionController controller;

  @override
  Widget build(BuildContext context) {
    return InspectionScope(
      controller: controller,
      child: MaterialApp(
        title: 'Legal Metrology Capture',
        debugShowCheckedModeBanner: false,
        theme: ThemeData(
          colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFF14526B)),
          useMaterial3: true,
        ),
        home: const HomeScreen(),
      ),
    );
  }
}
