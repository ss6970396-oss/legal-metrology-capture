import 'dart:io';

import 'package:device_info_plus/device_info_plus.dart';

import 'app_paths.dart';
import 'ids.dart';
import 'json_store.dart';

/// Identity of the handset that produced a piece of evidence.
///
/// [deviceId] is a UUIDv4 minted on first launch and persisted. It is
/// deliberately *not* a hardware serial, IMEI, or advertising identifier:
/// those are either unavailable without privileged permissions or carry
/// privacy obligations that inspection evidence does not need. What the
/// record has to support is "which handset took this photo, and were the
/// other photos in this set taken by the same one" — a stable random
/// identifier answers that.
///
/// The model and OS strings travel with the evidence because lens and sensor
/// characteristics matter when someone later disputes a quality score.
class DeviceIdentity {
  const DeviceIdentity({
    required this.deviceId,
    required this.platform,
    required this.model,
    required this.osVersion,
  });

  final String deviceId;
  final String platform;
  final String model;
  final String osVersion;

  static DeviceIdentity? _cached;

  static DeviceIdentity get current =>
      _cached ??
      const DeviceIdentity(
        deviceId: 'unresolved',
        platform: 'unknown',
        model: 'unknown',
        osVersion: 'unknown',
      );

  static Future<DeviceIdentity> load() async {
    final cached = _cached;
    if (cached != null) return cached;

    const store = JsonStore();
    final file = AppPaths.instance.deviceFile;
    final stored = await store.read(file);

    final deviceId = (stored?['deviceId'] as String?) ?? Ids.device();
    final descriptor = await _describeHardware();

    final identity = DeviceIdentity(
      deviceId: deviceId,
      platform: descriptor.platform,
      model: descriptor.model,
      osVersion: descriptor.osVersion,
    );

    if (stored == null || stored['deviceId'] != deviceId) {
      await store.write(file, identity.toJson());
    }
    _cached = identity;
    return identity;
  }

  static Future<_Hardware> _describeHardware() async {
    final plugin = DeviceInfoPlugin();
    try {
      if (Platform.isAndroid) {
        final info = await plugin.androidInfo;
        return _Hardware(
          platform: 'android',
          model: '${info.manufacturer} ${info.model}',
          osVersion: 'Android ${info.version.release} (SDK ${info.version.sdkInt})',
        );
      }
      if (Platform.isIOS) {
        final info = await plugin.iosInfo;
        return _Hardware(
          platform: 'ios',
          model: info.utsname.machine,
          osVersion: '${info.systemName} ${info.systemVersion}',
        );
      }
    } on Object {
      // Hardware description is metadata, not evidence. If the platform
      // channel is unavailable the capture still proceeds with 'unknown'.
    }
    return _Hardware(
      platform: Platform.operatingSystem,
      model: 'unknown',
      osVersion: Platform.operatingSystemVersion,
    );
  }

  Map<String, dynamic> toJson() => <String, dynamic>{
        'deviceId': deviceId,
        'platform': platform,
        'model': model,
        'osVersion': osVersion,
      };
}

class _Hardware {
  const _Hardware({
    required this.platform,
    required this.model,
    required this.osVersion,
  });

  final String platform;
  final String model;
  final String osVersion;
}
