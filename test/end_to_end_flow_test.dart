import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:legal_metrology_capture/core/app_paths.dart';
import 'package:legal_metrology_capture/core/device_identity.dart';
import 'package:legal_metrology_capture/main.dart';
import 'package:legal_metrology_capture/models/capture_context.dart';
import 'package:legal_metrology_capture/models/surface_step.dart';
import 'package:legal_metrology_capture/state/inspection_controller.dart';
import 'package:legal_metrology_capture/upload/extraction_contract.dart';
import 'package:legal_metrology_capture/upload/upload_queue.dart';
import 'package:legal_metrology_capture/upload/upload_transport.dart';

/// Drives the app the way an inspector would, from launch to a finished
/// package, on the real widget tree that `main.dart` builds.
///
/// Everything except the camera itself is exercised: session identifiers,
/// navigation, the guided sequence, the "not accessible" route, the coverage
/// indicator, and the rule that nothing reports complete until every required
/// step is resolved. The camera is the one piece a host machine cannot stand
/// in for, so this test resolves the surfaces the other way — by recording a
/// reason — which is a path a real inspection uses too.
/// Stands in for the real build descriptor. The values are arbitrary; what
/// matters is that a controller can be built without a platform channel.
const ClientDescriptor testClient = ClientDescriptor(
  platform: 'android',
  appVersion: 'test',
  deviceModel: 'test-device',
);

/// Answers every context question the way an inspector would for an ordinary
/// domestic retail package.
///
/// Called explicitly in each test rather than defaulted in the model, which is
/// the point of the design: there is no path that reaches a complete package
/// without someone having answered these.
Future<void> declareRetailContext(InspectionController controller) async {
  await controller.updateContext(
    saleChannel: SaleChannel.retail,
    isImported: false,
    isForRetail: true,
    isEcommerceListing: false,
  );
}

void main() {
  late Directory sandbox;

  setUpAll(() async {
    TestWidgetsFlutterBinding.ensureInitialized();

    sandbox = await Directory.systemTemp.createTemp('lm_capture_test');
    // path_provider has no registered implementation inside flutter_test, so
    // stand in for its platform channel and point storage at a scratch dir.
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(
      const MethodChannel('plugins.flutter.io/path_provider'),
      (MethodCall call) async => sandbox.path,
    );

    await AppPaths.ensureInitialized();
    await DeviceIdentity.load();
  });

  tearDownAll(() async {
    if (sandbox.existsSync()) {
      await sandbox.delete(recursive: true);
    }
  });

  testWidgets('launches and runs a package through to completion',
      (WidgetTester tester) async {
    // A tall surface so the whole step list is on screen without scrolling.
    tester.view.physicalSize = const Size(1100, 2600);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    // Loading the queue touches the real file system, which does not complete
    // inside the fake-async zone testWidgets runs in.
    late InspectionController controller;
    await tester.runAsync(() async {
      final queue = UploadQueue(
        transport: FakeUploadTransport(latency: Duration.zero),
      );
      await queue.load();
      controller = InspectionController(
        uploadQueue: queue,
        client: testClient,
      );
    });
    addTearDown(controller.dispose);

    await tester.pumpWidget(CaptureApp(controller: controller));
    await tester.pumpAndSettle();

    // 1. Launch lands on the start screen.
    expect(find.text('Start an inspection'), findsOneWidget);

    // 2. Starting a visit mints an inspection ID on the device, offline.
    await tester.tap(find.text('Start inspection'));
    await tester.pumpAndSettle();

    final inspection = controller.inspection;
    expect(inspection, isNotNull);
    expect(inspection!.inspectionId, isNotEmpty);
    expect(find.textContaining('inspectionId'), findsOneWidget);

    // 3. Adding a package mints a nested product session ID and opens the
    //    guided sequence.
    await tester.tap(find.text('Add package'));
    await tester.pumpAndSettle();

    final product = controller.product;
    expect(product, isNotNull);
    expect(product!.inspectionId, inspection.inspectionId);
    expect(product.productSessionId, isNot(inspection.inspectionId));

    expect(find.text('0 of 4 captured'), findsOneWidget);
    expect(find.text('Front — Principal Display Panel'), findsOneWidget);
    expect(find.text('Back'), findsOneWidget);
    expect(find.text('Side 1'), findsOneWidget);
    expect(find.text('Side 2'), findsOneWidget);

    // 4. Nothing is complete while steps are pending — the finish button is
    //    disabled and says how many are left.
    // Four surfaces plus four unanswered context questions.
    expect(find.text('8 items left'), findsOneWidget);
    expect(_finishButton(tester, '8 items left').onPressed, isNull);

    // 5. Resolve each surface by recording why it has no photograph.
    for (var i = 0; i < 4; i++) {
      // Target the button specifically: once a step is skipped its status
      // line reads "Not accessible" too, so a plain text finder would start
      // matching the status of an already-resolved step.
      await tester.tap(
        find.widgetWithText(TextButton, 'Not accessible').first,
      );
      await tester.pumpAndSettle();

      // Scope to the sheet: an already-skipped card displays its recorded
      // reason using the same wording as the radio option.
      await tester.tap(
        find.descendant(
          of: find.byType(BottomSheet),
          matching: find.text(SkipReason.packageNotAccessible.label),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.text('Record reason'));
      await tester.pumpAndSettle();

      expect(find.text('${i + 1} of 4 captured'), findsOneWidget);
    }

    // 6. Coverage is resolved, but the package is NOT complete: the context
    //    questions are unanswered and the finish button stays disabled. This
    //    is the guarantee that an applicability flag is never defaulted.
    expect(product.isCoverageComplete, isTrue);
    expect(product.isComplete, isFalse);
    expect(product.context.missingFields, hasLength(4));
    expect(find.text('4 items left'), findsOneWidget);
    expect(_finishButton(tester, '4 items left').onPressed, isNull);

    // 7. Declaring the context is what completes the package.
    //
    // Deliberately not awaited. The controller awaits its persistence chain,
    // and that chain already holds links created by the skip taps above —
    // real file writes issued inside the widget tester's fake-async zone,
    // which only completes when the clock is pumped. Awaiting from here would
    // wait on the pump while holding the pump. The state change and the
    // listener notification are synchronous, so pumping is all the assertions
    // below need.
    unawaited(declareRetailContext(controller));
    await tester.pumpAndSettle();

    expect(product.isComplete, isTrue);
    expect(find.text('Finish this package'), findsOneWidget);
    expect(_finishButton(tester, 'Finish this package').onPressed, isNotNull);

    // 8. Finishing returns to the visit, which now lists the package.
    await tester.tap(find.text('Finish this package'));
    await tester.pumpAndSettle();

    expect(find.text('4 of 4 captured'), findsOneWidget);
    expect(inspection.products.length, 1);
    expect(inspection.completeProductCount, 1);
  });

  test('the visit manifest reaches disk with both nested session IDs',
      () async {
    final queue = UploadQueue(
      transport: FakeUploadTransport(latency: Duration.zero),
    );
    await queue.load();
    final controller = InspectionController(
      uploadQueue: queue,
      client: testClient,
    );
    addTearDown(controller.dispose);

    final inspection = controller.startInspection(premisesLabel: 'Test shop');
    final product = controller.startProductSession();
    for (final step in product.steps) {
      await controller.skipStep(
        step,
        reason: SkipReason.packageNotAccessible,
      );
    }
    await declareRetailContext(controller);

    final manifest =
        AppPaths.instance.inspectionManifest(inspection.inspectionId);
    expect(manifest.existsSync(), isTrue);

    final written = manifest.readAsStringSync();
    // Both identifiers, and the instruction that the server keeps them as-is.
    expect(written, contains(inspection.inspectionId));
    expect(written, contains(product.productSessionId));
    expect(written, contains('uuid-v4-client-generated'));
    expect(written, contains('"isComplete": true'));
  });
}

FilledButton _finishButton(WidgetTester tester, String label) =>
    tester.widget<FilledButton>(
      find.ancestor(
        of: find.text(label),
        matching: find.byType(FilledButton),
      ),
    );
