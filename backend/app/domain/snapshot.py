"""PackageFactSnapshot v1.1 — construction, projection and validation.

The frozen ruling reduces to four rules, and this module is where all four are
mechanically enforced rather than trusted:

1. ``facts[]`` is the sole authoritative source of package facts.
2. Top-level typed fields are deterministic *projections* generated from
   ``facts[]``. No extraction code writes them independently.
3. If a projection and ``facts[]`` disagree, that is an internal consistency
   error: quarantine the run, do not silently repair it.
4. UNKNOWN, DECLARED_ABSENCE and CONFLICTING project to ``null``, while the
   authoritative status stays in ``facts[]``.

Rule 3 is the interesting one. A validator that repaired mismatches would make
the bug it detects invisible, so :func:`validate_snapshot` raises and the
caller quarantines. The projection is generated from the same fact set it is
then checked against, which sounds circular — it is not. The check catches the
class of bug where the projection *path* and the fact path diverge: a field
mapping that reads the wrong fact, a value transformed on one side only, a
partial update that touched the typed block. That is exactly the bug the
ruling is guarding against, and it is the kind that otherwise ships silently.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field as dataclass_field
from datetime import date, datetime, timezone
from typing import Any, Mapping

from .facts import FactSet, FactStatus, ResolvedFact

SCHEMA_VERSION = "package-facts/1.1"

#: Every field the projection knows how to render, mapped to its position in
#: the snapshot's typed blocks.
#:
#: This table is the contract surface. A field absent from it is a field the
#: extraction layer may still resolve internally but will not project — which
#: is the correct behaviour for anything Team 2 has not formally added, since
#: §4 forbids the extraction layer silently adding fields to the contract.
PROJECTION_MAP: dict[str, tuple[str, ...]] = {
    "commodity.name": ("commodity", "name"),
    "commodity.classification": ("commodity", "classification"),
    "package.is_prepackaged": ("package", "is_prepackaged"),
    "package.package_type": ("package", "package_type"),
    "package.net_quantity": ("package", "net_quantity"),
    "package.mrp": ("package", "mrp"),
    "package.unit_sale_price": ("package", "unit_sale_price"),
    "package.dimensions": ("package", "dimensions"),
    "declarations.manufacturer": ("declarations", "manufacturer"),
    "declarations.packer": ("declarations", "packer"),
    "declarations.importer": ("declarations", "importer"),
    "declarations.country_of_origin": ("declarations", "country_of_origin"),
    "declarations.generic_name": ("declarations", "generic_name"),
    "declarations.manufacture_date": ("declarations", "manufacture_date"),
    "declarations.best_before": ("declarations", "best_before"),
    "declarations.consumer_care": ("declarations", "consumer_care"),
}

#: Fields the extraction layer resolves for its own diagnostics but which have
#: no place in the typed projection. Kept explicit so that a field missing from
#: PROJECTION_MAP is a deliberate omission rather than an oversight nobody
#: notices until a snapshot silently loses it.
INTERNAL_ONLY_FIELDS: frozenset[str] = frozenset(
    {
        # §10: not authoritative in v1, and not a contract field.
        "presentation.letter_height_mm",
        "presentation.principal_display_panel_area_mm2",
    }
)


class SnapshotConsistencyError(Exception):
    """The generated projection disagrees with ``facts[]``.

    Per §7's quarantine rule this is never repaired in place. It carries the
    specific divergences so the run can be quarantined with a diagnosis
    attached rather than just a failure.
    """

    def __init__(self, divergences: list[str]) -> None:
        self.divergences = divergences
        super().__init__(
            "Snapshot projection diverges from facts[] in "
            f"{len(divergences)} place(s): " + "; ".join(divergences)
        )


class SnapshotValidationError(Exception):
    """The snapshot is not well-formed against the v1.1 contract."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(
            f"Snapshot failed {len(problems)} contract check(s): "
            + "; ".join(problems)
        )


@dataclass(frozen=True)
class Jurisdiction:
    country: str = "IN"
    state: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"country": self.country, "state": self.state}


@dataclass(frozen=True)
class CommercialContext:
    """The declared context, passed through from the capture session.

    Passed through, never inferred. These are the inspector's declarations
    about how the package was offered; the extraction layer has no business
    deducing them from photographs and §2's boundary puts their interpretation
    on Team 2's side regardless.
    """

    sale_channel: str
    is_imported: bool
    is_for_retail: bool
    is_ecommerce_listing: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "sale_channel": self.sale_channel,
            "is_imported": self.is_imported,
            "is_for_retail": self.is_for_retail,
            "is_ecommerce_listing": self.is_ecommerce_listing,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "CommercialContext":
        missing = [
            k
            for k in (
                "sale_channel",
                "is_imported",
                "is_for_retail",
                "is_ecommerce_listing",
            )
            if raw.get(k) is None
        ]
        if missing:
            # The capture client refuses to emit a partial context, so a
            # partial one arriving here means something upstream defaulted a
            # flag. Refusing is the only safe response: guessing an
            # applicability switch is how a requirement stops being evaluated.
            raise ValueError(
                "commercial context is incomplete "
                f"({', '.join(missing)}); applicability flags must be declared, "
                "never defaulted"
            )
        return cls(
            sale_channel=str(raw["sale_channel"]),
            is_imported=bool(raw["is_imported"]),
            is_for_retail=bool(raw["is_for_retail"]),
            is_ecommerce_listing=bool(raw["is_ecommerce_listing"]),
        )


@dataclass
class PackageFactSnapshot:
    """The object handed to the compliance engine.

    Build it with :func:`build_snapshot`, which is the only path that applies
    the projection rules. Constructing one directly bypasses them.
    """

    package_id: str
    as_of: date
    jurisdiction: Jurisdiction
    commercial_context: CommercialContext
    facts: FactSet
    #: Confidence that the commodity classification is right, when the
    #: pipeline produced one. Separate from fact confidence.
    classification_confidence: float | None = None
    extraction_run_id: str = ""
    schema_version: str = SCHEMA_VERSION
    #: Generated by :meth:`project`; never written by extraction code.
    _projection: dict[str, Any] = dataclass_field(default_factory=dict)

    # -- projection ------------------------------------------------------

    def project(self) -> dict[str, Any]:
        """Generate the typed top-level blocks from ``facts[]``.

        Deterministic: same fact set, same output, every time. Fields with no
        fact at all project to ``null`` exactly like fields whose fact is
        UNKNOWN, because from the contract's point of view they are the same
        claim — the extraction layer did not establish a value.
        """
        typed: dict[str, dict[str, Any]] = {
            "commodity": {},
            "package": {},
            "declarations": {},
        }

        for field_name, path in PROJECTION_MAP.items():
            block, key = path
            typed[block][key] = self.facts.value_of(field_name)

        if self.classification_confidence is not None:
            typed["commodity"]["classification_confidence"] = round(
                self.classification_confidence, 4
            )

        return typed

    # -- serialisation ---------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        typed = self.project()
        return {
            "schema_version": self.schema_version,
            "package_id": self.package_id,
            "as_of": self.as_of.isoformat(),
            "jurisdiction": self.jurisdiction.to_json(),
            "commercial_context": self.commercial_context.to_json(),
            "commodity": typed["commodity"],
            "package": typed["package"],
            "declarations": typed["declarations"],
            "facts": [f.to_contract_json() for f in self.facts.ordered()],
            "extraction_run_id": self.extraction_run_id,
        }

    def content_hash(self) -> str:
        """SHA-256 of the exact snapshot document.

        §16's control: a snapshot handed to the compliance engine can be shown
        later to be the one that was produced, byte for byte. Everything is
        covered except ``extraction_run_id`` itself, which names the run rather
        than describing its result.

        This hash **does** cover the per-run identifiers inside the document —
        fact ids, region ids, OCR run ids — so two runs over identical evidence
        produce different content hashes. That is correct for this hash's
        purpose (identifying a document) and useless for the other question
        anyone asks of a hash here: "did the extraction result change?" For
        that, use :meth:`semantic_hash`.
        """
        payload = self.to_json()
        payload.pop("extraction_run_id", None)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def semantic_hash(self) -> str:
        """SHA-256 of what the snapshot *claims*, ignoring run-scoped ids.

        Fact ids, region ids and OCR run ids are minted fresh on every run, so
        they say which execution produced a document rather than what it says.
        Stripping them makes the hash stable across reruns of the same evidence
        and therefore able to answer the question that matters when a model,
        a threshold or a normalization rule changes: did the *result* move?

        A regression suite compares these. Two runs whose semantic hashes match
        resolved every field to the same value with the same status from the
        same pixels; two that differ changed something a reviewer should look
        at. Comparing :meth:`content_hash` for that purpose would report a
        difference on every single rerun and so report nothing at all.
        """
        payload = self.to_json()
        payload.pop("extraction_run_id", None)
        canonical = json.dumps(
            _strip_run_ids(payload), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_snapshot(
    *,
    package_id: str,
    facts: FactSet,
    jurisdiction: Jurisdiction,
    commercial_context: CommercialContext,
    extraction_run_id: str,
    classification_confidence: float | None = None,
    as_of: date | None = None,
) -> PackageFactSnapshot:
    """Assemble a snapshot and prove it is internally consistent.

    Raises :class:`SnapshotConsistencyError` or
    :class:`SnapshotValidationError` rather than returning something
    questionable. A caller that catches either should quarantine the run — see
    §7: "do not silently repair the snapshot".
    """
    snapshot = PackageFactSnapshot(
        package_id=package_id,
        as_of=as_of or datetime.now(timezone.utc).date(),
        jurisdiction=jurisdiction,
        commercial_context=commercial_context,
        facts=facts,
        classification_confidence=classification_confidence,
        extraction_run_id=extraction_run_id,
    )
    validate_snapshot(snapshot)
    return snapshot


def validate_snapshot(snapshot: PackageFactSnapshot) -> None:
    """Run the contract checks. Raises on the first category that fails.

    Consistency is checked before well-formedness on purpose: a projection that
    disagrees with its facts is a different and more serious problem than a
    missing provenance link, and reporting the shallower issue first would bury
    it.
    """
    _check_projection_consistency(snapshot)
    _check_contract_wellformedness(snapshot)


def _check_projection_consistency(snapshot: PackageFactSnapshot) -> None:
    """Rule 3: the typed projection must agree with ``facts[]``."""
    typed = snapshot.project()
    divergences: list[str] = []

    for field_name, (block, key) in PROJECTION_MAP.items():
        fact = snapshot.facts.get(field_name)
        projected = typed[block].get(key)

        if fact is None:
            if projected is not None:
                divergences.append(
                    f"{block}.{key} projects {projected!r} but no fact backs it"
                )
            continue

        if fact.status.carries_value:
            if projected != fact.value:
                divergences.append(
                    f"{block}.{key} projects {projected!r} but fact "
                    f"{fact.fact_id} holds {fact.value!r}"
                )
        elif projected is not None:
            divergences.append(
                f"{block}.{key} projects {projected!r} but fact {fact.fact_id} is "
                f"{fact.status.value}, which must project to null"
            )

    if divergences:
        raise SnapshotConsistencyError(divergences)


def _check_contract_wellformedness(snapshot: PackageFactSnapshot) -> None:
    """Structural checks against the frozen v1.1 shape."""
    problems: list[str] = []

    if snapshot.schema_version != SCHEMA_VERSION:
        problems.append(
            f"schema_version is {snapshot.schema_version!r}, expected "
            f"{SCHEMA_VERSION!r}"
        )

    if not snapshot.package_id:
        problems.append("package_id is empty")

    if not snapshot.extraction_run_id:
        problems.append(
            "extraction_run_id is empty; §18 requires every fact to be traceable "
            "to a processing run"
        )

    seen_fields: set[str] = set()
    for fact in snapshot.facts.ordered():
        if fact.field in seen_fields:
            problems.append(f"{fact.field} appears more than once in facts[]")
        seen_fields.add(fact.field)

        # §17: 100% of authoritative facts link to artifact + region + run.
        if fact.status.carries_value:
            if fact.provenance is None:
                problems.append(
                    f"{fact.field} is {fact.status.value} without provenance"
                )
            else:
                for attr in ("artifact_id", "region_id", "ocr_run_id"):
                    if not getattr(fact.provenance, attr):
                        problems.append(
                            f"{fact.field} provenance is missing {attr}"
                        )

        if fact.status is FactStatus.DECLARED_ABSENCE and not fact.note:
            problems.append(
                f"{fact.field} is DECLARED_ABSENCE with no stated evidence basis"
            )

        if (
            fact.field not in PROJECTION_MAP
            and fact.field not in INTERNAL_ONLY_FIELDS
        ):
            # Not fatal, but worth refusing: an unrecognised field name is
            # usually a typo, and a typo'd field silently fails to project.
            problems.append(
                f"{fact.field} is not a projectable contract field and is not "
                "declared internal-only"
            )

    if problems:
        raise SnapshotValidationError(problems)


#: Identifiers minted per run rather than derived from the evidence.
#:
#: Excluded from the semantic hash. They are genuinely useful — they are how a
#: fact is traced back to the pixels that produced it — but they carry no
#: information about what the snapshot claims, and including them would make
#: every rerun look like a changed result.
_RUN_SCOPED_KEYS: frozenset[str] = frozenset(
    {"fact_id", "region_id", "ocr_run_id"}
)


def _strip_run_ids(value: Any) -> Any:
    """Recursively drop run-scoped identifiers from a snapshot payload."""
    if isinstance(value, dict):
        return {
            k: _strip_run_ids(v)
            for k, v in value.items()
            if k not in _RUN_SCOPED_KEYS
        }
    if isinstance(value, list):
        return [_strip_run_ids(v) for v in value]
    return value


@dataclass(frozen=True)
class QuarantinedRun:
    """A run whose snapshot could not be trusted.

    Produced instead of a snapshot when validation fails. It is a record, not
    an error path to be swallowed: §7 wants the inconsistency exposed for
    engineering diagnosis, and a run that vanished into a log line is not
    exposed.
    """

    extraction_run_id: str
    package_id: str
    reason: str
    problems: tuple[str, ...]
    quarantined_at: datetime

    @classmethod
    def from_error(
        cls,
        *,
        extraction_run_id: str,
        package_id: str,
        error: Exception,
    ) -> "QuarantinedRun":
        if isinstance(error, SnapshotConsistencyError):
            reason = "projection_facts_divergence"
            problems = tuple(error.divergences)
        elif isinstance(error, SnapshotValidationError):
            reason = "contract_validation_failure"
            problems = tuple(error.problems)
        else:
            reason = "unexpected_error"
            problems = (str(error),)

        return cls(
            extraction_run_id=extraction_run_id,
            package_id=package_id,
            reason=reason,
            problems=problems,
            quarantined_at=datetime.now(timezone.utc),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "extraction_run_id": self.extraction_run_id,
            "package_id": self.package_id,
            "reason": self.reason,
            "problems": list(self.problems),
            "quarantined_at": self.quarantined_at.isoformat(),
            "snapshot_released": False,
        }
