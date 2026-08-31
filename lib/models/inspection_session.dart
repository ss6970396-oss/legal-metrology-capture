import '../core/json_store.dart';
import 'product_session.dart';

/// One inspection visit.
///
/// The identifier nesting is the point of this class: an [inspectionId] per
/// visit, a [ProductSession.productSessionId] per package within it, and both
/// stamped on every image, scan and check result. That is what lets a reviewer
/// take any single evidence file and place it — this package, this visit —
/// without needing an index to be intact.
class InspectionSession {
  InspectionSession({
    required this.inspectionId,
    required this.startedAtUtc,
    required this.deviceId,
    required this.products,
    this.premisesLabel,
    this.inspectorReference,
    this.closedAtUtc,
  });

  final String inspectionId;
  final DateTime startedAtUtc;
  final String deviceId;
  final List<ProductSession> products;

  /// Free text describing where the visit took place. Optional, because an
  /// inspector should never be blocked from capturing evidence by a form
  /// field.
  String? premisesLabel;

  /// Optional inspector or case reference from the parent system.
  String? inspectorReference;

  DateTime? closedAtUtc;

  bool get isClosed => closedAtUtc != null;

  int get completeProductCount =>
      products.where((p) => p.isComplete).length;

  bool get hasIncompleteProducts => products.any((p) => !p.isComplete);

  ProductSession? productById(String productSessionId) {
    for (final product in products) {
      if (product.productSessionId == productSessionId) return product;
    }
    return null;
  }

  factory InspectionSession.start({
    required String inspectionId,
    required String deviceId,
  }) =>
      InspectionSession(
        inspectionId: inspectionId,
        startedAtUtc: DateTime.now().toUtc(),
        deviceId: deviceId,
        products: <ProductSession>[],
      );

  Map<String, dynamic> toJson() => <String, dynamic>{
        'inspectionId': inspectionId,
        'premisesLabel': premisesLabel,
        'inspectorReference': inspectorReference,
        'deviceId': deviceId,
        'startedAtUtc': encodeTime(startedAtUtc),
        'closedAtUtc': closedAtUtc == null ? null : encodeTime(closedAtUtc!),
        'productCount': products.length,
        'completeProductCount': completeProductCount,
        // Stated explicitly for the server: these identifiers were minted on
        // the device and are to be stored as-is, not reissued.
        'idScheme': 'uuid-v4-client-generated',
        'products': products.map((p) => p.toJson()).toList(),
      };

  factory InspectionSession.fromJson(Map<String, dynamic> json) =>
      InspectionSession(
        inspectionId: json['inspectionId'] as String? ?? '',
        premisesLabel: json['premisesLabel'] as String?,
        inspectorReference: json['inspectorReference'] as String?,
        deviceId: json['deviceId'] as String? ?? 'unknown',
        startedAtUtc: decodeTime(json['startedAtUtc']),
        closedAtUtc: decodeTimeOrNull(json['closedAtUtc']),
        products: ((json['products'] as List?) ?? const [])
            .whereType<Map<String, dynamic>>()
            .map(ProductSession.fromJson)
            .toList(),
      );
}
