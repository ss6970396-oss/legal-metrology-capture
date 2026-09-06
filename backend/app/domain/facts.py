"""Fact status, provenance and the resolved-fact record.

This module is where the v1.1 epistemic model lives. Two rules from Team 2's
frozen ruling shape everything here, and both are enforced in code rather than
left to convention:

1. ``facts[]`` is the sole authoritative source. Nothing else writes a package
   fact. The typed top-level fields in the snapshot are projections generated
   from these records — see ``snapshot.py``.

2. ``DECLARED_ABSENCE`` may never be manufactured from OCR non-detection.
   §2 of the contract states there is "no legitimate code path of the form
   'OCR did not find MRP -> package.mrp = DECLARED_ABSENCE'". So the
   constructor for that status demands an explicit evidence basis and refuses
   to build one without it. Absence of an observation is ``UNKNOWN``.

The distinction between the five statuses must not collapse into a generic
null. A null projection is a *presentation* of a resolved state; the state
itself stays here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from .geometry import Geometry


class FactStatus(str, Enum):
    """The epistemic state of one field, per the frozen v1.1 model."""

    #: A visually supported declaration is present in the evidence.
    OBSERVED = "OBSERVED"

    #: Deterministically computed from supported observations. Must carry a
    #: derivation trace back to those observations.
    DERIVED = "DERIVED"

    #: Evidence is insufficient to determine the field. This is the correct
    #: answer for "we did not capture that face", "the panel was unreadable"
    #: and "OCR found nothing here" alike.
    UNKNOWN = "UNKNOWN"

    #: The evidence itself establishes that the declaration is absent. Only
    #: producible through :meth:`ResolvedFact.declared_absent`, which requires
    #: a stated basis.
    DECLARED_ABSENCE = "DECLARED_ABSENCE"

    #: Credible observations disagree and the deterministic policy cannot
    #: safely pick one.
    CONFLICTING = "CONFLICTING"

    #: The field is not measurable by this system at all — the physical
    #: text-height case from §10.
    #:
    #: NOT IN THE FROZEN v1.1 CONTRACT. It is emitted only into the internal
    #: observation record and the extraction diagnostics, never into a
    #: snapshot's facts[]; ``project_status`` maps it to UNKNOWN at the
    #: boundary. Kept as a distinct internal state because "we cannot measure
    #: this" and "we could not read it this time" are different engineering
    #: facts, and flattening them would lose the distinction §10 insists on.
    NOT_TESTABLE = "NOT_TESTABLE"

    @property
    def carries_value(self) -> bool:
        """Whether a top-level projection may show this fact's value.

        Only OBSERVED and DERIVED do. Everything else projects to null, per
        the projection rule in §4 of the contract.
        """
        return self in (FactStatus.OBSERVED, FactStatus.DERIVED)

    @property
    def is_contract_status(self) -> bool:
        """Whether this status may appear in a snapshot's ``facts[]``."""
        return self is not FactStatus.NOT_TESTABLE

    def for_contract(self) -> "FactStatus":
        """Map an internal status onto the frozen contract's vocabulary."""
        if self is FactStatus.NOT_TESTABLE:
            return FactStatus.UNKNOWN
        return self


@dataclass(frozen=True)
class Provenance:
    """Where a fact's value came from, in pixels.

    §17 requires 100% of authoritative facts to link to an artifact, a region
    and a run identifier. That is checked at snapshot validation time; this
    type is what makes it possible to check.

    ``supporting`` holds the other observations that agreed. The contract's
    sample shows a single provenance block, so the primary one is what
    projects into ``facts[]`` — but the agreeing views are retained here
    because §15 forbids treating repeated evidence as if only one photograph
    had ever existed.
    """

    artifact_id: str
    region_id: str
    ocr_run_id: str
    geometry: Geometry
    supporting: tuple["Provenance", ...] = ()

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "artifact_id": self.artifact_id,
            "region_id": self.region_id,
            "ocr_run_id": self.ocr_run_id,
            "geometry": self.geometry.to_json(),
        }
        if self.supporting:
            payload["supporting"] = [p.to_json() for p in self.supporting]
        return payload


@dataclass(frozen=True)
class ConfidenceBreakdown:
    """The component scores behind a fact's confidence.

    §7 is explicit that OCR confidence, field-attribution confidence and
    resolved-fact confidence are different quantities and that the system
    "must not simply copy OCR confidence into the final fact confidence". So
    the components are kept separately and the combination is a named, versioned
    policy rather than an arithmetic accident.

    The resulting number is not a probability of correctness and nothing
    downstream should read it as one until it has been calibrated against
    held-out ground truth. ``policy_version`` is what makes a later
    recalibration detectable: scores produced under different policies are not
    comparable, and a stored score that does not say which policy produced it
    is not re-fittable.
    """

    ocr: float
    attribution: float
    region_quality: float
    resolution: float
    policy_version: str

    def to_json(self) -> dict[str, Any]:
        return {
            "ocr": round(self.ocr, 4),
            "attribution": round(self.attribution, 4),
            "region_quality": round(self.region_quality, 4),
            "resolution": round(self.resolution, 4),
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True)
class DerivationTrace:
    """How a DERIVED value was computed, and from what.

    Required for every DERIVED fact. §8 allows a unit sale price to be derived
    from net quantity and MRP, but only as a traceable derivation that never
    overwrites an observed declaration — this is the record that makes the
    derivation auditable after the fact.
    """

    rule_id: str
    rule_version: str
    source_fact_ids: tuple[str, ...]
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "source_fact_ids": list(self.source_fact_ids),
            "note": self.note,
        }


@dataclass(frozen=True)
class ConflictDetail:
    """One of the disagreeing candidates behind a CONFLICTING fact.

    §15 requires that a conflict stay inspectable rather than being hidden by
    whichever OCR result happened to run last, and §13 of the first report
    requires the rejected candidate and the reason to be retained. Both are
    satisfied by attaching these to the fact itself, so a reviewer sees the
    disagreement without having to query the observation store.
    """

    value: Any
    confidence: float
    provenance: Provenance

    def to_json(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "confidence": round(self.confidence, 4),
            "provenance": self.provenance.to_json(),
        }


def new_fact_id() -> str:
    return f"F-{uuid.uuid4()}"


@dataclass(frozen=True)
class ResolvedFact:
    """One authoritative fact about a package.

    Construct these through the classmethods rather than the initialiser. They
    are what enforce the invariants — a value only where the status permits
    one, a derivation trace on every DERIVED fact, an explicit basis on every
    DECLARED_ABSENCE — and going around them is how a contract violation gets
    into the snapshot builder without anything noticing.
    """

    fact_id: str
    field: str
    status: FactStatus
    value: Any
    confidence: float
    provenance: Provenance | None
    confidence_breakdown: ConfidenceBreakdown | None = None
    derivation: DerivationTrace | None = None
    conflicts: tuple[ConflictDetail, ...] = ()
    #: Free-text engineering note. Never legal reasoning.
    note: str = ""

    def __post_init__(self) -> None:
        if self.status.carries_value:
            if self.value is None:
                raise ValueError(
                    f"{self.field}: a {self.status.value} fact must carry a value"
                )
            if self.provenance is None:
                raise ValueError(
                    f"{self.field}: a {self.status.value} fact must carry provenance; "
                    "§17 requires 100% provenance completeness on authoritative facts"
                )
        elif self.value is not None:
            raise ValueError(
                f"{self.field}: a {self.status.value} fact must not carry a value — "
                "the semantic state is the answer, and a value beside it would be "
                "read as one"
            )

        if self.status is FactStatus.DERIVED and self.derivation is None:
            raise ValueError(
                f"{self.field}: a DERIVED fact requires a derivation trace"
            )

        if self.status is FactStatus.CONFLICTING and len(self.conflicts) < 2:
            raise ValueError(
                f"{self.field}: a CONFLICTING fact must retain at least the two "
                "candidates that disagreed"
            )

        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"{self.field}: confidence {self.confidence} is outside 0..1"
            )

    # -- constructors ----------------------------------------------------

    @classmethod
    def observed(
        cls,
        field: str,
        value: Any,
        confidence: float,
        provenance: Provenance,
        breakdown: ConfidenceBreakdown | None = None,
        note: str = "",
    ) -> "ResolvedFact":
        return cls(
            fact_id=new_fact_id(),
            field=field,
            status=FactStatus.OBSERVED,
            value=value,
            confidence=confidence,
            provenance=provenance,
            confidence_breakdown=breakdown,
            note=note,
        )

    @classmethod
    def derived(
        cls,
        field: str,
        value: Any,
        confidence: float,
        provenance: Provenance,
        derivation: DerivationTrace,
        breakdown: ConfidenceBreakdown | None = None,
        note: str = "",
    ) -> "ResolvedFact":
        return cls(
            fact_id=new_fact_id(),
            field=field,
            status=FactStatus.DERIVED,
            value=value,
            confidence=confidence,
            provenance=provenance,
            confidence_breakdown=breakdown,
            derivation=derivation,
            note=note,
        )

    @classmethod
    def unknown(cls, field: str, note: str = "") -> "ResolvedFact":
        """The default answer whenever evidence does not settle a field.

        Confidence is 0 and that is not a placeholder: there is no evidence,
        so there is nothing to be confident about. Anything else would invite
        a downstream reader to treat an unread field as a weak observation.
        """
        return cls(
            fact_id=new_fact_id(),
            field=field,
            status=FactStatus.UNKNOWN,
            value=None,
            confidence=0.0,
            provenance=None,
            note=note,
        )

    @classmethod
    def not_testable(cls, field: str, reason: str) -> "ResolvedFact":
        """The field cannot be determined by this system at all.

        For physical text-height under §10: v1 preserves the text evidence and
        surfaces the measurement as not-testable rather than inventing a
        millimetre figure from pixels. Projects to UNKNOWN at the contract
        boundary; the distinct internal state is what stops a later reader
        concluding the panel merely photographed badly.
        """
        return cls(
            fact_id=new_fact_id(),
            field=field,
            status=FactStatus.NOT_TESTABLE,
            value=None,
            confidence=0.0,
            provenance=None,
            note=reason,
        )

    @classmethod
    def declared_absent(
        cls,
        field: str,
        basis: str,
        provenance: Provenance,
        confidence: float,
    ) -> "ResolvedFact":
        """The evidence affirmatively establishes the declaration is absent.

        Guarded deliberately. This status is the one with the most downstream
        consequence and the easiest to produce by accident, so it demands two
        things that a non-detection cannot supply: a written basis describing
        what in the evidence establishes the absence, and provenance pointing
        at the pixels that show it.

        "OCR returned nothing for this field" satisfies neither. Use
        :meth:`unknown`.
        """
        if not basis or not basis.strip():
            raise ValueError(
                f"{field}: DECLARED_ABSENCE requires an explicit evidence basis. "
                "Absence of an OCR observation is UNKNOWN, not absence of a "
                "declaration (contract v1.1 §2)."
            )
        return cls(
            fact_id=new_fact_id(),
            field=field,
            status=FactStatus.DECLARED_ABSENCE,
            value=None,
            confidence=confidence,
            provenance=provenance,
            note=basis,
        )

    @classmethod
    def conflicting(
        cls,
        field: str,
        candidates: Sequence[ConflictDetail],
        note: str = "",
    ) -> "ResolvedFact":
        """Credible observations disagree and no rule safely picks one.

        Confidence is the *highest* candidate confidence, not an average.
        The number describes how good the best reading was, which is what a
        reviewer triaging conflicts wants to know; averaging would make a
        confident disagreement look like a weak one.
        """
        best = max((c.confidence for c in candidates), default=0.0)
        return cls(
            fact_id=new_fact_id(),
            field=field,
            status=FactStatus.CONFLICTING,
            value=None,
            confidence=best,
            provenance=None,
            conflicts=tuple(candidates),
            note=note,
        )

    # -- serialisation ---------------------------------------------------

    def to_contract_json(self) -> dict[str, Any]:
        """One entry of the snapshot's ``facts[]``."""
        payload: dict[str, Any] = {
            "fact_id": self.fact_id,
            "field": self.field,
            "value": self.value,
            "status": self.status.for_contract().value,
            "confidence": round(self.confidence, 4),
        }
        payload["provenance"] = (
            self.provenance.to_json() if self.provenance is not None else None
        )
        if self.derivation is not None:
            payload["derivation"] = self.derivation.to_json()
        if self.conflicts:
            payload["conflicts"] = [c.to_json() for c in self.conflicts]
        if self.confidence_breakdown is not None:
            payload["confidence_breakdown"] = self.confidence_breakdown.to_json()
        if self.note:
            payload["note"] = self.note
        if self.status is not self.status.for_contract():
            # Do not silently drop the finer internal state. A reader who only
            # knows the contract sees UNKNOWN; one who cares can see why.
            payload["internal_status"] = self.status.value
        return payload


@dataclass
class FactSet:
    """The authoritative facts for one package, keyed by field.

    One fact per field, which is what makes the projection deterministic. The
    resolver is responsible for reducing many observations to that one fact;
    by the time facts reach here the reconciliation has already happened.
    """

    facts: dict[str, ResolvedFact] = field(default_factory=dict)

    def add(self, fact: ResolvedFact) -> None:
        if fact.field in self.facts:
            raise ValueError(
                f"{fact.field} already has an authoritative fact. Reconciliation "
                "belongs in the resolver, not in the fact set."
            )
        self.facts[fact.field] = fact

    def get(self, field_name: str) -> ResolvedFact | None:
        return self.facts.get(field_name)

    def value_of(self, field_name: str) -> Any:
        """The projected value for a field: its value, or None.

        This is the single place the projection rule is applied, so the
        snapshot builder cannot accidentally project a CONFLICTING value by
        reading ``.value`` directly.
        """
        fact = self.facts.get(field_name)
        if fact is None or not fact.status.carries_value:
            return None
        return fact.value

    def ordered(self) -> list[ResolvedFact]:
        """Facts in a stable field order, so snapshot hashes are comparable."""
        return [self.facts[k] for k in sorted(self.facts)]

    def __len__(self) -> int:
        return len(self.facts)

    def __contains__(self, field_name: object) -> bool:
        return field_name in self.facts
