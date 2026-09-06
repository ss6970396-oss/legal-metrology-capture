import '../core/json_store.dart';

/// Where and how the package was offered for sale.
///
/// The contract sample only shows `RETAIL`. The rest of this enum is a
/// proposal: an inspector standing in a cash-and-carry needs somewhere to put
/// that, and collapsing it into `RETAIL` would be a false declaration rather
/// than a missing one. The wire names are upper-case because that is the shape
/// the contract sample uses.
///
/// UNCONFIRMED WITH TEAM 2: only `RETAIL` is attested in the supplied
/// contract. Everything else here needs their sign-off before it can be sent.
enum SaleChannel { retail, wholesale, ecommerce, institutional }

extension SaleChannelX on SaleChannel {
  String get wireName => switch (this) {
        SaleChannel.retail => 'RETAIL',
        SaleChannel.wholesale => 'WHOLESALE',
        SaleChannel.ecommerce => 'ECOMMERCE',
        SaleChannel.institutional => 'INSTITUTIONAL',
      };

  String get label => switch (this) {
        SaleChannel.retail => 'Retail sale',
        SaleChannel.wholesale => 'Wholesale / cash and carry',
        SaleChannel.ecommerce => 'E-commerce listing',
        SaleChannel.institutional => 'Institutional / not for retail sale',
      };

  /// Shown under the option in the picker. These describe what the inspector
  /// is looking at, not what the law then requires — the applicability call is
  /// the compliance engine's.
  String get hint => switch (this) {
        SaleChannel.retail =>
          'Offered to a shopper on a shelf, counter or display.',
        SaleChannel.wholesale =>
          'Sold in bulk to a trade buyer rather than to a consumer.',
        SaleChannel.ecommerce =>
          'The package backs an online listing you are inspecting.',
        SaleChannel.institutional =>
          'Supplied to an institution, or marked not for retail sale.',
      };

  static SaleChannel parse(Object? value) => SaleChannel.values.firstWhere(
        (v) => v.wireName == value || v.name == value,
        orElse: () => SaleChannel.retail,
      );
}

/// The jurisdiction the package was found in.
///
/// `country` is fixed at `IN` for this deployment. `state` is nullable in the
/// contract and stays nullable here; an inspector who does not record it
/// produces `null`, which is the contract's own representation of "not
/// specified" and is not the same as a guess.
class Jurisdiction {
  const Jurisdiction({this.country = 'IN', this.state});

  final String country;
  final String? state;

  Jurisdiction copyWith({String? country, String? state, bool clearState = false}) =>
      Jurisdiction(
        country: country ?? this.country,
        state: clearState ? null : (state ?? this.state),
      );

  Map<String, dynamic> toJson() => <String, dynamic>{
        'country': country,
        'state': state,
      };

  factory Jurisdiction.fromJson(Map<String, dynamic> json) => Jurisdiction(
        country: json['country'] as String? ?? 'IN',
        state: json['state'] as String?,
      );

  @override
  bool operator ==(Object other) =>
      other is Jurisdiction && other.country == country && other.state == state;

  @override
  int get hashCode => Object.hash(country, state);
}

/// The commercial context an inspector declares for one package.
///
/// This is the `context` block of the Team 1 input contract, and it is the one
/// part of the upload that is *declared* rather than *observed*. Nothing in
/// here comes out of a photograph. It is the inspector saying what they were
/// looking at when they took the pictures, and the compliance engine uses it
/// to decide which rules apply at all — whether a country-of-origin
/// declaration is required, whether the retail-package rules bite, whether the
/// e-commerce provisions are in play.
///
/// Because those are applicability switches, a wrong value here is worse than
/// a missing one: declaring `isImported: false` on an imported package can
/// suppress a requirement that should have been evaluated. So every flag is
/// nullable and starts null. Null means "the inspector has not said", the
/// session cannot be submitted while any flag is null, and no default is ever
/// silently substituted. The contract's booleans are only produced once a
/// person has actually answered.
class CaptureContext {
  const CaptureContext({
    this.jurisdiction = const Jurisdiction(),
    this.saleChannel,
    this.isImported,
    this.isForRetail,
    this.isEcommerceListing,
    this.declaredAtUtc,
  });

  final Jurisdiction jurisdiction;

  /// Null until the inspector picks one.
  final SaleChannel? saleChannel;

  /// Whether the package appears to be imported. Drives the country-of-origin
  /// requirement downstream, which is why it is not defaulted.
  final bool? isImported;

  /// Whether the package is intended for retail sale.
  final bool? isForRetail;

  /// Whether this package backs an e-commerce listing under inspection.
  final bool? isEcommerceListing;

  /// When the inspector completed the declaration. Part of the audit trail:
  /// context declared before the photographs and context declared after them
  /// are different acts.
  final DateTime? declaredAtUtc;

  /// Every flag answered. The upload path checks this; see [missingFields] for
  /// what to tell the inspector when it is false.
  bool get isComplete =>
      saleChannel != null &&
      isImported != null &&
      isForRetail != null &&
      isEcommerceListing != null;

  /// Inspector-facing names of the unanswered questions, in the order the form
  /// presents them.
  List<String> get missingFields => <String>[
        if (saleChannel == null) 'How the package was offered for sale',
        if (isImported == null) 'Whether the package is imported',
        if (isForRetail == null) 'Whether the package is for retail sale',
        if (isEcommerceListing == null)
          'Whether this backs an e-commerce listing',
      ];

  CaptureContext copyWith({
    Jurisdiction? jurisdiction,
    SaleChannel? saleChannel,
    bool? isImported,
    bool? isForRetail,
    bool? isEcommerceListing,
  }) {
    final next = CaptureContext(
      jurisdiction: jurisdiction ?? this.jurisdiction,
      saleChannel: saleChannel ?? this.saleChannel,
      isImported: isImported ?? this.isImported,
      isForRetail: isForRetail ?? this.isForRetail,
      isEcommerceListing: isEcommerceListing ?? this.isEcommerceListing,
      declaredAtUtc: declaredAtUtc,
    );
    // The declaration timestamp is stamped at the moment the last answer
    // lands, not on every keystroke before it.
    if (next.isComplete && declaredAtUtc == null) {
      return next._withDeclaredAt(DateTime.now().toUtc());
    }
    return next;
  }

  CaptureContext _withDeclaredAt(DateTime value) => CaptureContext(
        jurisdiction: jurisdiction,
        saleChannel: saleChannel,
        isImported: isImported,
        isForRetail: isForRetail,
        isEcommerceListing: isEcommerceListing,
        declaredAtUtc: value,
      );

  /// The contract's `context` block.
  ///
  /// Throws when called on an incomplete context. That is deliberate: there is
  /// no correct way to render an unanswered applicability flag as a boolean,
  /// so the only safe thing to do is refuse rather than emit a plausible
  /// falsehood. Callers gate on [isComplete].
  Map<String, dynamic> toContractJson() {
    if (!isComplete) {
      throw StateError(
        'The capture context is incomplete (${missingFields.join(', ')}). '
        'An applicability flag must not be defaulted — ask the inspector.',
      );
    }
    return <String, dynamic>{
      'jurisdiction': jurisdiction.toJson(),
      'sale_channel': saleChannel!.wireName,
      'is_imported': isImported!,
      'is_for_retail': isForRetail!,
      'is_ecommerce_listing': isEcommerceListing!,
    };
  }

  /// Local persistence, which unlike [toContractJson] must be able to round
  /// trip a half-finished declaration — an inspector interrupted mid-form
  /// should find their answers where they left them.
  Map<String, dynamic> toJson() => <String, dynamic>{
        'jurisdiction': jurisdiction.toJson(),
        'saleChannel': saleChannel?.wireName,
        'isImported': isImported,
        'isForRetail': isForRetail,
        'isEcommerceListing': isEcommerceListing,
        'declaredAtUtc':
            declaredAtUtc == null ? null : encodeTime(declaredAtUtc!),
        'complete': isComplete,
      };

  factory CaptureContext.fromJson(Map<String, dynamic> json) => CaptureContext(
        jurisdiction: Jurisdiction.fromJson(
          (json['jurisdiction'] as Map?)?.cast<String, dynamic>() ??
              const <String, dynamic>{},
        ),
        saleChannel: json['saleChannel'] == null
            ? null
            : SaleChannelX.parse(json['saleChannel']),
        isImported: json['isImported'] as bool?,
        isForRetail: json['isForRetail'] as bool?,
        isEcommerceListing: json['isEcommerceListing'] as bool?,
        declaredAtUtc: decodeTimeOrNull(json['declaredAtUtc']),
      );
}
