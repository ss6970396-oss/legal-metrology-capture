import 'package:flutter/material.dart';

import 'state/inspection_controller.dart';
import 'ui/screens/home_screen.dart';
import 'upload/upload_transport.dart';

/// Legal Metrology package-inspection capture module.
///
/// This app covers only what happens on the phone before evidence leaves it:
/// session identity, guided capture of the package faces, on-device quality
/// checks, barcode identification, and a durable upload queue.
///
/// The upload destination is chosen here and nowhere else. It ships pointed at
/// [FakeUploadTransport] — a local stub that exercises the queue, its retries
/// and its backoff without sending anything anywhere. Swap in
/// [HttpMultipartUploadTransport] with a base URI when the evidence API
/// exists; nothing else in the module changes, and anything already queued
/// against the stub will be re-sent to the real endpoint.
void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  final controller = await InspectionController.bootstrap(
    // To point at a real backend:
    //
    //   transport: HttpMultipartUploadTransport(
    //     baseUri: Uri.parse('https://evidence.example.gov/api/v1'),
    //     authorizationToken: '…',
    //   ),
    transport: FakeUploadTransport(
      // A non-zero failure rate makes the retry and backoff path visible
      // during development rather than theoretical.
      failureRate: 0.25,
    ),
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
