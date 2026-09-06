"""The Fact Resolver.

Stage 10: reduce many candidates to one authoritative fact per field. This is
the component Team 2's ruling names explicitly — "a deterministic Fact Resolver
reconciles observations before generating the PackageFactSnapshot projection" —
and the rules it enforces come straight from §15 and §13:

* Repeated values across views merge as *supporting* evidence, and near
  duplicates do not count as independent confirmation.
* A higher-confidence candidate may win only provisionally, and the rejected
  candidate and the reason are retained.
* Genuinely disagreeing values of comparable confidence become CONFLICTING.
  Nothing is silently chosen.
* A field with no candidates is UNKNOWN. Never DECLARED_ABSENCE.

The last one is the rule with teeth. It is the easiest mistake to make and the
hardest to see once made, because a DECLARED_ABSENCE reads downstream as a
finding about the package rather than a gap in the evidence.

"Deterministic" is meant literally: no model scores decide anything here, and
the same candidate set always resolves the same way. §15 forbids using "an
opaque model score as a substitute for a defined resolution policy".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..domain.facts import (
    ConfidenceBreakdown,
    ConflictDetail,
    DerivationTrace,
    FactSet,
    Provenance,
    ResolvedFact,
)
from ..domain.observation import FieldCandidate, ObservationStore, SurfaceView
from . import normalize as nz

#: Bump when resolution behaviour changes. Recorded on every run.
RESOLVER_VERSION = "resolve-1.0.0"

#: Confidence-combination policy identifier, stored on every breakdown.
#:
#: Separate from the resolver version because the weighting can be recalibrated
#: (§7 wants it fitted against held-out ground truth) without the resolution
#: *logic* changing, and results from the two should stay distinguishable.
CONFIDENCE_POLICY_VERSION = "conf-1.0.0"

#: Confidence gap above which the better candidate wins outright.
#:
#: Below it, two disagreeing readings are treated as comparably credible and
#: the field goes to CONFLICTING. Uncalibrated — §17 measures conflict
#: precision/recall and this must be re-fitted from that, not guessed at. It is
#: set wide on purpose: the cost of a spurious CONFLICTING is a human glance at
#: two photographs, and the cost of a wrong silent pick is a fact nobody checks.
DECISIVE_CONFIDENCE_GAP = 0.25

#: IoU above which two regions on the same artifact are the same detection.
DUPLICATE_REGION_IOU = 0.60


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------

def combine_confidence(
    *,
    ocr: float,
    attribution: float,
    region_quality: float,
    agreement: float,
) -> float:
    """Reduce the component scores to one fact confidence.

    Multiplicative rather than a weighted average, and that is the substantive
    choice here. These components are closer to independent conditions than to
    interchangeable opinions: a perfect OCR read of text attributed to the
    wrong field is not "quite good on average", it is wrong. A product lets any
    one weak component drag the result down, which is the behaviour we want;
    an average lets a strong OCR score paper over a weak attribution.

    ``agreement`` carries the cross-view evidence and is the one term that can
    push a result *up*, because independent views agreeing is genuinely more
    than either view alone.

    §7's warning applies to the output: this is not a probability of
    correctness and must not be read as one until calibrated.
    """
    base = ocr * attribution * max(region_quality, 0.1)
    return max(0.0, min(1.0, base * agreement))


def _agreement_factor(supporting_views: int) -> float:
    """Uplift for independent views that agreed.

    Saturating, and deliberately modest. Three photographs of the same panel
    are not three independent measurements — they share the printing, the
    lighting and the recognizer's biases — so agreement is worth something and
    much less than the count suggests.
    """
    if supporting_views <= 1:
        return 1.0
    return min(1.15, 1.0 + 0.05 * (supporting_views - 1))


# ---------------------------------------------------------------------------
# Value comparison
# ---------------------------------------------------------------------------

def values_equal(field: str, a: Any, b: Any) -> bool:
    """Whether two normalized values are the same declaration.

    Field-aware because equality is not uniform. Two quantities in different
    units can be the same declaration; two dates at different precisions
    cannot be compared as strings; two addresses differ by whitespace and OCR
    noise constantly.
    """
    if a == b:
        return True
    if a is None or b is None:
        return False

    if field == "package.net_quantity":
        return _quantities_equal(a, b)
    if field in ("package.mrp",):
        return _money_equal(a, b)
    if field == "package.unit_sale_price":
        return _unit_price_equal(a, b)
    if field in ("declarations.manufacture_date", "declarations.best_before"):
        return _dates_equal(a, b)
    if field in (
        "declarations.manufacturer",
        "declarations.packer",
        "declarations.importer",
    ):
        return _parties_equal(a, b)
    if isinstance(a, str) and isinstance(b, str):
        return nz.clean_text(a).casefold() == nz.clean_text(b).casefold()
    return False


def _money_equal(a: Any, b: Any) -> bool:
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return False
    if a.get("currency") != b.get("currency"):
        return False
    try:
        # Two paise. Prices are printed to at most two decimals, so anything
        # beyond rounding noise is a genuinely different figure.
        return abs(float(a["amount"]) - float(b["amount"])) < 0.02
    except (KeyError, TypeError, ValueError):
        return False


def _quantities_equal(a: Any, b: Any) -> bool:
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return False
    try:
        qa = nz.Quantity(value=float(a["value"]), unit=str(a["unit"]))
        qb = nz.Quantity(value=float(b["value"]), unit=str(b["unit"]))
    except (KeyError, TypeError, ValueError):
        return False

    base_a = nz.to_base_unit(qa)
    base_b = nz.to_base_unit(qb)
    if base_a is None or base_b is None:
        return qa == qb
    if base_a[0] != base_b[0]:
        return False
    # Relative tolerance: 500 g vs 0.5 kg must match through the float
    # conversion, while 500 g and 505 g must not.
    larger = max(abs(base_a[1]), abs(base_b[1]), 1e-9)
    return abs(base_a[1] - base_b[1]) / larger < 1e-6


def _unit_price_equal(a: Any, b: Any) -> bool:
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return False
    if a.get("currency") != b.get("currency"):
        return False
    pa, pb = a.get("per") or {}, b.get("per") or {}
    try:
        base_a = nz.to_base_unit(
            nz.Quantity(value=float(pa["value"]), unit=str(pa["unit"]))
        )
        base_b = nz.to_base_unit(
            nz.Quantity(value=float(pb["value"]), unit=str(pb["unit"]))
        )
        if base_a is None or base_b is None or base_a[0] != base_b[0]:
            return False
        # Normalise both to price per one base unit before comparing.
        rate_a = float(a["amount"]) / base_a[1] if base_a[1] else None
        rate_b = float(b["amount"]) / base_b[1] if base_b[1] else None
        if rate_a is None or rate_b is None:
            return False
        larger = max(abs(rate_a), abs(rate_b), 1e-9)
        return abs(rate_a - rate_b) / larger < 1e-4
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False


def _dates_equal(a: Any, b: Any) -> bool:
    """Dates match when the coarser one is a prefix of the finer.

    ``2026-07`` and ``2026-07-14`` are the same declaration read at two
    precisions — one panel printed the day and another did not — not a
    conflict. ``2026-07`` and ``2026-08`` are a conflict.
    """
    if not (isinstance(a, str) and isinstance(b, str)):
        return False
    shorter, longer = sorted((a, b), key=len)
    return longer.startswith(shorter)


def _parties_equal(a: Any, b: Any) -> bool:
    """Name equality decides; the address is allowed to differ.

    The same manufacturer is routinely printed with a full address on the back
    and a city alone on the side. Requiring both to match would manufacture a
    conflict out of the ordinary way packages are laid out.
    """
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return False
    return (
        nz.clean_text(str(a.get("name", ""))).casefold()
        == nz.clean_text(str(b.get("name", ""))).casefold()
    )


def _richer_value(field: str, a: Any, b: Any) -> Any:
    """Of two equal values, the one carrying more detail.

    Merging supporting observations should not lose information: if one panel
    printed ``2026-07-14`` and another ``2026-07``, the fact should carry the
    day. Likewise the party declaration with the fuller address.
    """
    if field in ("declarations.manufacture_date", "declarations.best_before"):
        return a if len(str(a)) >= len(str(b)) else b
    if isinstance(a, dict) and isinstance(b, dict) and "address" in a:
        return a if len(str(a.get("address", ""))) >= len(
            str(b.get("address", ""))
        ) else b
    return a


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

@dataclass
class _Group:
    """Candidates that agree on a value."""

    value: Any
    members: list[FieldCandidate]

    @property
    def artifacts(self) -> set[str]:
        return {c.artifact_id for c in self.members}

    @property
    def independent_views(self) -> int:
        """Distinct artifacts backing this value.

        Artifacts, not candidates. Two regions on one photograph reading the
        same MRP is one view of one panel, and §15 warns against counting
        repeated evidence as independent confirmation.
        """
        return len(self.artifacts)

    @property
    def best(self) -> FieldCandidate:
        return max(
            self.members,
            key=lambda c: (c.attribution_confidence * c.recognition_confidence),
        )


def _deduplicate(candidates: Sequence[FieldCandidate]) -> list[FieldCandidate]:
    """Drop candidates that are the same detection read twice.

    Two OCR engines over the same region, or two overlapping detections on one
    artifact, produce candidates that look like corroboration and are not.
    """
    kept: list[FieldCandidate] = []
    for candidate in sorted(
        candidates,
        key=lambda c: c.attribution_confidence * c.recognition_confidence,
        reverse=True,
    ):
        duplicate = False
        for existing in kept:
            if existing.artifact_id != candidate.artifact_id:
                continue
            if existing.geometry.iou(candidate.geometry) >= DUPLICATE_REGION_IOU:
                duplicate = True
                break
        if not duplicate:
            kept.append(candidate)
    return kept


def _group_by_value(
    field: str, candidates: Sequence[FieldCandidate]
) -> list[_Group]:
    groups: list[_Group] = []
    for candidate in candidates:
        for group in groups:
            if values_equal(field, group.value, candidate.normalized_value):
                group.members.append(candidate)
                group.value = _richer_value(
                    field, group.value, candidate.normalized_value
                )
                break
        else:
            groups.append(
                _Group(value=candidate.normalized_value, members=[candidate])
            )
    return groups


def _provenance_for(group: _Group) -> Provenance:
    """Primary provenance, with the agreeing observations attached."""
    best = group.best
    supporting = tuple(
        Provenance(
            artifact_id=c.artifact_id,
            region_id=c.region_id,
            ocr_run_id=c.ocr_run_id,
            geometry=c.geometry,
        )
        for c in group.members
        if c.candidate_id != best.candidate_id
    )
    return Provenance(
        artifact_id=best.artifact_id,
        region_id=best.region_id,
        ocr_run_id=best.ocr_run_id,
        geometry=best.geometry,
        supporting=supporting,
    )


def _group_confidence(group: _Group) -> tuple[float, ConfidenceBreakdown]:
    best = group.best
    breakdown = ConfidenceBreakdown(
        ocr=best.recognition_confidence,
        attribution=best.attribution_confidence,
        region_quality=best.region_quality,
        resolution=_agreement_factor(group.independent_views),
        policy_version=CONFIDENCE_POLICY_VERSION,
    )
    confidence = combine_confidence(
        ocr=breakdown.ocr,
        attribution=breakdown.attribution,
        region_quality=breakdown.region_quality,
        agreement=breakdown.resolution,
    )
    return confidence, breakdown


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve_field(field: str, candidates: Sequence[FieldCandidate]) -> ResolvedFact:
    """Reduce one field's candidates to one authoritative fact."""
    if not candidates:
        return ResolvedFact.unknown(
            field,
            note=(
                "No candidate observation was produced for this field. Absence "
                "of an observation is an extraction uncertainty, not evidence "
                "that the declaration is absent (contract v1.1 §2)."
            ),
        )

    deduped = _deduplicate(candidates)
    groups = _group_by_value(field, deduped)
    groups.sort(key=lambda g: _group_confidence(g)[0], reverse=True)

    winner = groups[0]
    winner_confidence, winner_breakdown = _group_confidence(winner)

    if len(groups) == 1:
        return ResolvedFact.observed(
            field=field,
            value=winner.value,
            confidence=winner_confidence,
            provenance=_provenance_for(winner),
            breakdown=winner_breakdown,
            note=(
                f"Agreed across {winner.independent_views} view(s)."
                if winner.independent_views > 1
                else "Single observation."
            ),
        )

    runner_up = groups[1]
    runner_confidence, _ = _group_confidence(runner_up)
    gap = winner_confidence - runner_confidence

    if gap < DECISIVE_CONFIDENCE_GAP:
        # Comparable credibility. §15: produce CONFLICTING and a null
        # projection rather than silently selecting one OCR result.
        details = [
            ConflictDetail(
                value=group.value,
                confidence=_group_confidence(group)[0],
                provenance=_provenance_for(group),
            )
            for group in groups
        ]
        return ResolvedFact.conflicting(
            field=field,
            candidates=details,
            note=(
                f"{len(groups)} incompatible values with confidences within "
                f"{DECISIVE_CONFIDENCE_GAP:.2f}; the deterministic policy does "
                "not choose between them."
            ),
        )

    # Decisive gap: the winner stands, but the rejected reading is retained on
    # the fact so the resolution stays reviewable rather than disappearing.
    return ResolvedFact.observed(
        field=field,
        value=winner.value,
        confidence=winner_confidence,
        provenance=_provenance_for(winner),
        breakdown=winner_breakdown,
        note=(
            f"Provisionally resolved over {len(groups) - 1} rejected "
            f"reading(s); confidence gap {gap:.2f}. Rejected: "
            + "; ".join(
                f"{g.value!r} @ {_group_confidence(g)[0]:.2f}" for g in groups[1:]
            )
        ),
    )


#: Fields the resolver always emits, so a snapshot says something about every
#: contract field rather than staying silent about the ones nothing matched.
#:
#: Silence and UNKNOWN are different claims downstream: a missing field could
#: mean the pipeline does not support it, while UNKNOWN says the pipeline
#: looked and did not establish a value.
ALWAYS_RESOLVED: tuple[str, ...] = (
    "package.mrp",
    "package.net_quantity",
    "package.unit_sale_price",
    "declarations.manufacturer",
    "declarations.packer",
    "declarations.importer",
    "declarations.country_of_origin",
    "declarations.generic_name",
    "declarations.manufacture_date",
    "declarations.best_before",
    "declarations.consumer_care",
)


def resolve(store: ObservationStore) -> FactSet:
    """Resolve every field for one package.

    Runs the printed-declaration pass first, then the derivation pass, in that
    order and never the reverse: §8 forbids a computed unit price replacing an
    observed one, and the only robust way to honour that is to know what was
    observed before deriving anything.
    """
    facts = FactSet()

    for field in ALWAYS_RESOLVED:
        facts.add(resolve_field(field, store.candidates_for(field)))

    _derive_unit_sale_price(facts)
    _resolve_physical_measurements(facts)

    return facts


def _derive_unit_sale_price(facts: FactSet) -> None:
    """Fill in unit sale price by derivation, only where none was printed.

    Replaces the existing fact only when that fact is UNKNOWN. An OBSERVED
    printed declaration is never overwritten (§8), and neither is a
    CONFLICTING one — a package whose panels print different unit prices has a
    problem that a calculation would paper over.
    """
    existing = facts.get("package.unit_sale_price")
    if existing is None or existing.status.carries_value:
        return
    if existing.status.value == "CONFLICTING":
        return

    mrp_fact = facts.get("package.mrp")
    qty_fact = facts.get("package.net_quantity")
    if mrp_fact is None or qty_fact is None:
        return
    if not (mrp_fact.status.carries_value and qty_fact.status.carries_value):
        return

    try:
        money = nz.Money(
            amount=float(mrp_fact.value["amount"]),
            currency=str(mrp_fact.value["currency"]),
        )
        quantity = nz.Quantity(
            value=float(qty_fact.value["value"]),
            unit=str(qty_fact.value["unit"]),
        )
    except (KeyError, TypeError, ValueError):
        return

    derived = nz.derive_unit_sale_price(money, quantity)
    if derived is None:
        return
    if mrp_fact.provenance is None:
        return

    # The derived fact inherits the MRP's provenance: those are the pixels
    # that most directly caused the number. The derivation trace names both
    # source facts, so the net-quantity contribution is not lost.
    facts.facts["package.unit_sale_price"] = ResolvedFact.derived(
        field="package.unit_sale_price",
        value=derived.to_json(),
        # Never more confident than the weaker of its inputs.
        confidence=min(mrp_fact.confidence, qty_fact.confidence),
        provenance=mrp_fact.provenance,
        derivation=DerivationTrace(
            rule_id="unit_sale_price/mrp-over-net-quantity",
            rule_version=nz.NORMALIZER_VERSION,
            source_fact_ids=(mrp_fact.fact_id, qty_fact.fact_id),
            note=(
                "Computed because the package carries no printed unit-sale-price "
                "declaration. A printed declaration always takes precedence "
                "(contract v1.1 §8)."
            ),
        ),
        note="Derived, not observed on the package.",
    )


def _resolve_physical_measurements(facts: FactSet) -> None:
    """Record that physical text measurement was not attempted.

    §10's v1 decision: no automated font-size compliance from uncontrolled
    photographs. The architecture asks the system to "surface the measurement
    as UNKNOWN/NOT_TESTABLE where a physical scale cannot be established"
    rather than staying silent, so the pipeline emits an explicit
    NOT_TESTABLE fact.

    These are internal-only fields; they never reach ``facts[]`` in the
    snapshot. Their purpose is to make the absence deliberate and visible in
    the extraction record, so nobody later reads a missing measurement as one
    that was attempted and failed.
    """
    reason = (
        "No validated pixel-to-millimetre scale exists for an uncontrolled "
        "handheld capture. Physical letter-height measurement is out of scope "
        "for v1 (contract v1.1 §10); the text evidence is preserved and the "
        "measurement is reported as not testable rather than estimated."
    )
    for field in (
        "presentation.letter_height_mm",
        "presentation.principal_display_panel_area_mm2",
    ):
        if field not in facts:
            facts.add(ResolvedFact.not_testable(field, reason))


# ---------------------------------------------------------------------------
# Cross-artifact checks
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PackageIdentityWarning:
    """Evidence that the artifacts may not all show the same package.

    §13 requires a package-identity conflict rather than a merge when images
    appear to contain different packages. Merging facts across two packages
    would produce a snapshot describing neither.
    """

    reason: str
    artifact_ids: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {"reason": self.reason, "artifact_ids": list(self.artifact_ids)}


def check_package_identity(
    store: ObservationStore,
) -> list[PackageIdentityWarning]:
    """Flag signs that the capture set spans more than one package.

    Conservative: it reports, it does not block. A genuine sticker overlay
    produces the same signal as two different packages, and §13 puts the
    interpretation of a sticker outside extraction. What the resolver can do
    is refuse to let the ambiguity pass unnoticed.
    """
    warnings: list[PackageIdentityWarning] = []

    for field in ("package.net_quantity", "declarations.generic_name"):
        candidates = _deduplicate(store.candidates_for(field))
        groups = _group_by_value(field, candidates)
        # Two different net quantities across two artifacts, each seen clearly,
        # is a stronger signal of two packages than of one misread panel.
        multi_artifact = [g for g in groups if g.independent_views >= 1]
        if len(multi_artifact) > 1:
            artifacts = sorted(
                {a for g in multi_artifact for a in g.artifacts}
            )
            if len(artifacts) > 1:
                warnings.append(
                    PackageIdentityWarning(
                        reason=(
                            f"{len(multi_artifact)} incompatible {field} values "
                            "across different artifacts; the capture set may "
                            "show more than one package"
                        ),
                        artifact_ids=tuple(artifacts),
                    )
                )

    return warnings


def duplicate_artifacts(store: ObservationStore) -> list[tuple[str, str]]:
    """Pairs of artifacts that appear to be near-duplicates of one another.

    §16 asks for near-duplicate detection so repeated evidence is not counted
    as independent confirmation. Compared by normalized text overlap rather
    than by image hashing: two shots of the same panel differ in every pixel
    and carry the same words, which is the similarity that actually matters
    for fact resolution.
    """
    by_artifact: dict[str, set[str]] = {}
    for observation in store.observations.values():
        text = nz.clean_text(observation.normalized_text).casefold()
        if len(text) >= 4:
            by_artifact.setdefault(observation.artifact_id, set()).add(text)

    pairs: list[tuple[str, str]] = []
    artifact_ids = sorted(by_artifact)
    for i, a in enumerate(artifact_ids):
        for b in artifact_ids[i + 1:]:
            texts_a, texts_b = by_artifact[a], by_artifact[b]
            if not texts_a or not texts_b:
                continue
            overlap = len(texts_a & texts_b) / min(len(texts_a), len(texts_b))
            if overlap >= 0.85:
                pairs.append((a, b))
    return pairs


def surface_coverage(store: ObservationStore) -> dict[str, Any]:
    """Which package faces the capture set actually contains."""
    seen = {a.surface for a in store.artifacts.values()}
    return {
        "surfaces_present": sorted(s.value for s in seen),
        "has_principal_display_panel": SurfaceView.FRONT in seen,
        "artifact_count": len(store.artifacts),
    }
