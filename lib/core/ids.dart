import 'package:uuid/uuid.dart';

/// Client-side identifier generation.
///
/// Every identifier in this module is a UUIDv4 minted on the device at the
/// moment the thing it names comes into existence. Nothing here touches the
/// network: an inspector who walks into a premises with no signal gets the
/// same identifiers they would get online.
///
/// The server is expected to persist the client-supplied identifier verbatim
/// rather than reissuing one of its own. Reissuing would break the link
/// between evidence already written to disk on the device and the record the
/// server holds, and would make an offline-first inspection unreconcilable.
class Ids {
  const Ids._();

  static const Uuid _uuid = Uuid();

  /// One per inspection visit to a premises.
  static String inspection() => _uuid.v4();

  /// One per package examined within a visit.
  static String productSession() => _uuid.v4();

  /// One per shutter press, including attempts later rejected in favour of a
  /// retake. Rejected attempts keep their identifier so the attempt history
  /// stays auditable.
  static String capture() => _uuid.v4();

  /// One per barcode read, whether scanned live or keyed by hand.
  static String scan() => _uuid.v4();

  /// One per queued upload.
  static String uploadTask() => _uuid.v4();

  /// Installation identity. Minted once on first launch and persisted; see
  /// [DeviceIdentity].
  static String device() => _uuid.v4();
}
