"""The v1.1 contract rules, tested directly.

These are the tests that must never be allowed to fail. Everything else in the
pipeline is a quality question — how well it reads a panel, how good the
thresholds are — but these encode Team 2's frozen ruling, and a regression here
means the extraction layer is making claims it is not entitled to make.

Each test names the rule it defends.
"""

from __future__ import annotations

import pytest

from app.domain.facts import (
    ConflictDetail,
    DerivationTrace,
    FactSet,
    FactStatus,
    Provenance,
    ResolvedFact,
)
from app.domain.geometry import CoordinateSpace, Geometry, GeometryType
from app.domain.snapshot import (
    CommercialContext,
    Jurisdiction,
    PackageFactSnapshot,
    SnapshotConsistencyError,
    SnapshotValidationError,
    build_snapshot,
    validate_snapshot,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def a_geometry() -> Geometry:
    return Geometry.bbox(100, 200, 420, 260)


def a_provenance(artifact: str = "IMG-1") -> Provenance:
    return Provenance(
        artifact_id=artifact,
        region_id="REG-09",
        ocr_run_id="OCR-77",
        geometry=a_geometry(),
    )


def a_context() -> CommercialContext:
    return CommercialContext(
        sale_channel="RETAIL",
        is_imported=False,
        is_for_retail=True,
        is_ecommerce_listing=False,
    )


def build(facts: FactSet) -> PackageFactSnapshot:
    return build_snapshot(
        package_id="PKG-1",
        facts=facts,
        jurisdiction=Jurisdiction(),
        commercial_context=a_context(),
        extraction_run_id="RUN-1",
    )


# ---------------------------------------------------------------------------
# Rule: DECLARED_ABSENCE may never be manufactured from OCR non-detection
# ---------------------------------------------------------------------------

class TestDeclaredAbsence:
    """Contract §2: there is no legitimate code path of the form
    'OCR did not find MRP -> package.mrp = DECLARED_ABSENCE'."""

    def test_requires_an_explicit_evidence_basis(self) -> None:
        with pytest.raises(ValueError, match="explicit evidence basis"):
            ResolvedFact.declared_absent(
                field="package.mrp",
                basis="",
                provenance=a_provenance(),
                confidence=0.9,
            )

    def test_whitespace_is_not_a_basis(self) -> None:
        with pytest.raises(ValueError, match="explicit evidence basis"):
            ResolvedFact.declared_absent(
                field="package.mrp",
                basis="   ",
                provenance=a_provenance(),
                confidence=0.9,
            )

    def test_a_stated_basis_is_accepted(self) -> None:
        fact = ResolvedFact.declared_absent(
            field="package.mrp",
            basis=(
                "The principal display panel is fully legible at high "
                "resolution and carries no price declaration anywhere on it."
            ),
            provenance=a_provenance(),
            confidence=0.8,
        )
        assert fact.status is FactStatus.DECLARED_ABSENCE
        assert fact.value is None

    def test_a_field_with_no_candidates_resolves_to_unknown(self) -> None:
        from app.pipeline.resolver import resolve_field

        fact = resolve_field("package.mrp", [])
        assert fact.status is FactStatus.UNKNOWN
        assert "not evidence" in fact.note

    def test_snapshot_validation_rejects_a_basis_free_absence(self) -> None:
        # Constructed around the guard, the way a bug would.
        rogue = ResolvedFact(
            fact_id="F-x",
            field="package.mrp",
            status=FactStatus.DECLARED_ABSENCE,
            value=None,
            confidence=0.5,
            provenance=a_provenance(),
            note="",
        )
        facts = FactSet()
        facts.add(rogue)
        with pytest.raises(SnapshotValidationError, match="no stated evidence basis"):
            build(facts)


# ---------------------------------------------------------------------------
# Rule: the projection carries a value only for OBSERVED and DERIVED
# ---------------------------------------------------------------------------

class TestProjection:
    """Contract §4: UNKNOWN, DECLARED_ABSENCE and CONFLICTING project to null,
    while the authoritative status stays in facts[]."""

    @pytest.mark.parametrize(
        "make_fact",
        [
            lambda: ResolvedFact.unknown("package.mrp"),
            lambda: ResolvedFact.not_testable("package.mrp", "no scale"),
            lambda: ResolvedFact.declared_absent(
                "package.mrp", "panel legible, no price", a_provenance(), 0.8
            ),
            lambda: ResolvedFact.conflicting(
                "package.mrp",
                [
                    ConflictDetail({"amount": 120.0, "currency": "INR"}, 0.9,
                                   a_provenance("IMG-1")),
                    ConflictDetail({"amount": 180.0, "currency": "INR"}, 0.88,
                                   a_provenance("IMG-2")),
                ],
            ),
        ],
        ids=["unknown", "not_testable", "declared_absence", "conflicting"],
    )
    def test_non_value_statuses_project_to_null(self, make_fact) -> None:
        facts = FactSet()
        facts.add(make_fact())
        snapshot = build(facts)
        assert snapshot.project()["package"]["mrp"] is None

    def test_observed_projects_its_value(self) -> None:
        facts = FactSet()
        facts.add(
            ResolvedFact.observed(
                "package.mrp",
                {"amount": 120.0, "currency": "INR"},
                0.95,
                a_provenance(),
            )
        )
        snapshot = build(facts)
        assert snapshot.project()["package"]["mrp"] == {
            "amount": 120.0,
            "currency": "INR",
        }

    def test_the_status_survives_in_facts_even_when_the_value_does_not(
        self,
    ) -> None:
        """A null projection must not erase the distinction between an
        unread field and one whose readings disagreed."""
        facts = FactSet()
        facts.add(ResolvedFact.unknown("package.mrp"))
        facts.add(
            ResolvedFact.conflicting(
                "package.net_quantity",
                [
                    ConflictDetail({"value": 500, "unit": "g"}, 0.9,
                                   a_provenance("IMG-1")),
                    ConflictDetail({"value": 250, "unit": "g"}, 0.88,
                                   a_provenance("IMG-2")),
                ],
            )
        )
        payload = build(facts).to_json()

        assert payload["package"]["mrp"] is None
        assert payload["package"]["net_quantity"] is None

        by_field = {f["field"]: f for f in payload["facts"]}
        assert by_field["package.mrp"]["status"] == "UNKNOWN"
        assert by_field["package.net_quantity"]["status"] == "CONFLICTING"

    def test_not_testable_reaches_the_contract_as_unknown(self) -> None:
        """NOT_TESTABLE is internal (§10). The contract vocabulary has no such
        status, so it projects as UNKNOWN — with the finer state preserved
        alongside rather than lost."""
        facts = FactSet()
        facts.add(ResolvedFact.not_testable("package.mrp", "no validated scale"))
        entry = build(facts).to_json()["facts"][0]
        assert entry["status"] == "UNKNOWN"
        assert entry["internal_status"] == "NOT_TESTABLE"


# ---------------------------------------------------------------------------
# Rule: a projection/facts divergence quarantines, it does not self-repair
# ---------------------------------------------------------------------------

class TestQuarantine:
    """Contract §7: if the projection and facts[] disagree, fail validation and
    quarantine — do not silently repair the snapshot."""

    def test_a_divergent_projection_raises(self) -> None:
        facts = FactSet()
        facts.add(
            ResolvedFact.observed(
                "package.mrp",
                {"amount": 120.0, "currency": "INR"},
                0.95,
                a_provenance(),
            )
        )
        snapshot = build(facts)

        # Simulate the bug the rule guards against: the typed block and the
        # fact drift apart.
        object.__setattr__(
            snapshot.facts.facts["package.mrp"],
            "value",
            {"amount": 999.0, "currency": "INR"},
        )
        # project() regenerates from facts, so force a stale projection the way
        # a partial update would.
        original_project = snapshot.project

        def stale_projection() -> dict:
            result = original_project()
            result["package"]["mrp"] = {"amount": 120.0, "currency": "INR"}
            return result

        snapshot.project = stale_projection  # type: ignore[method-assign]

        with pytest.raises(SnapshotConsistencyError) as caught:
            validate_snapshot(snapshot)
        assert "package.mrp" in str(caught.value)

    def test_quarantine_record_names_the_divergences(self) -> None:
        from app.domain.snapshot import QuarantinedRun

        error = SnapshotConsistencyError(["package.mrp projects 120 but fact holds 999"])
        quarantine = QuarantinedRun.from_error(
            extraction_run_id="RUN-1", package_id="PKG-1", error=error
        )
        assert quarantine.reason == "projection_facts_divergence"
        assert quarantine.to_json()["snapshot_released"] is False
        assert len(quarantine.problems) == 1


# ---------------------------------------------------------------------------
# Rule: authoritative facts carry complete provenance
# ---------------------------------------------------------------------------

class TestProvenance:
    """Contract §17: 100% of authoritative facts link to an artifact, a region
    and a run identifier."""

    def test_an_observed_fact_cannot_be_built_without_provenance(self) -> None:
        with pytest.raises(ValueError, match="must carry provenance"):
            ResolvedFact(
                fact_id="F-1",
                field="package.mrp",
                status=FactStatus.OBSERVED,
                value={"amount": 120.0, "currency": "INR"},
                confidence=0.9,
                provenance=None,
            )

    def test_validation_catches_an_incomplete_provenance_link(self) -> None:
        facts = FactSet()
        facts.add(
            ResolvedFact.observed(
                "package.mrp",
                {"amount": 120.0, "currency": "INR"},
                0.9,
                Provenance(
                    artifact_id="IMG-1",
                    region_id="",  # missing
                    ocr_run_id="OCR-1",
                    geometry=a_geometry(),
                ),
            )
        )
        with pytest.raises(SnapshotValidationError, match="missing region_id"):
            build(facts)

    def test_a_non_value_fact_needs_no_provenance(self) -> None:
        facts = FactSet()
        facts.add(ResolvedFact.unknown("package.mrp"))
        assert build(facts).facts.get("package.mrp").provenance is None

    def test_supporting_observations_are_retained(self) -> None:
        """§15: repeated evidence across views is preserved, not collapsed."""
        primary = Provenance(
            artifact_id="IMG-1",
            region_id="REG-1",
            ocr_run_id="OCR-1",
            geometry=a_geometry(),
            supporting=(a_provenance("IMG-2"), a_provenance("IMG-3")),
        )
        fact = ResolvedFact.observed(
            "package.mrp", {"amount": 120.0, "currency": "INR"}, 0.95, primary
        )
        payload = fact.to_contract_json()
        assert len(payload["provenance"]["supporting"]) == 2


# ---------------------------------------------------------------------------
# Rule: a value only where the status permits one
# ---------------------------------------------------------------------------

class TestFactInvariants:
    def test_a_non_value_status_may_not_carry_a_value(self) -> None:
        with pytest.raises(ValueError, match="must not carry a value"):
            ResolvedFact(
                fact_id="F-1",
                field="package.mrp",
                status=FactStatus.UNKNOWN,
                value={"amount": 120.0, "currency": "INR"},
                confidence=0.0,
                provenance=None,
            )

    def test_derived_requires_a_derivation_trace(self) -> None:
        with pytest.raises(ValueError, match="requires a derivation trace"):
            ResolvedFact(
                fact_id="F-1",
                field="package.unit_sale_price",
                status=FactStatus.DERIVED,
                value={"amount": 0.24, "currency": "INR"},
                confidence=0.8,
                provenance=a_provenance(),
            )

    def test_conflicting_retains_the_disagreeing_candidates(self) -> None:
        with pytest.raises(ValueError, match="at least the two candidates"):
            ResolvedFact(
                fact_id="F-1",
                field="package.mrp",
                status=FactStatus.CONFLICTING,
                value=None,
                confidence=0.5,
                provenance=None,
                conflicts=(
                    ConflictDetail({"amount": 1.0}, 0.5, a_provenance()),
                ),
            )

    def test_confidence_must_be_a_fraction(self) -> None:
        with pytest.raises(ValueError, match="outside 0..1"):
            ResolvedFact.observed(
                "package.mrp", {"amount": 1.0}, 1.4, a_provenance()
            )

    def test_one_fact_per_field(self) -> None:
        facts = FactSet()
        facts.add(ResolvedFact.unknown("package.mrp"))
        with pytest.raises(ValueError, match="already has an authoritative fact"):
            facts.add(ResolvedFact.unknown("package.mrp"))


# ---------------------------------------------------------------------------
# Rule: the context is declared, never defaulted
# ---------------------------------------------------------------------------

class TestCommercialContext:
    def test_a_missing_applicability_flag_is_refused(self) -> None:
        with pytest.raises(ValueError, match="never defaulted"):
            CommercialContext.from_json(
                {
                    "sale_channel": "RETAIL",
                    "is_imported": True,
                    "is_for_retail": True,
                    # is_ecommerce_listing absent
                }
            )

    def test_false_is_a_real_answer_not_a_missing_one(self) -> None:
        context = CommercialContext.from_json(
            {
                "sale_channel": "RETAIL",
                "is_imported": False,
                "is_for_retail": False,
                "is_ecommerce_listing": False,
            }
        )
        assert context.is_imported is False


# ---------------------------------------------------------------------------
# Rule: richer geometry survives
# ---------------------------------------------------------------------------

class TestGeometry:
    """Contract §9: BBOX, QUADRILATERAL and POLYGON are all supported, and the
    richest available form is preserved rather than flattened at ingest."""

    def test_a_quadrilateral_keeps_its_corners(self) -> None:
        quad = Geometry.quad([(100, 200), (420, 210), (418, 262), (98, 252)])
        assert quad.type is GeometryType.QUADRILATERAL
        assert len(quad.points) == 4

    def test_a_bbox_can_be_derived_without_mutating_the_source(self) -> None:
        quad = Geometry.quad([(100, 200), (420, 210), (418, 262), (98, 252)])
        box = quad.to_bbox()
        assert box.type is GeometryType.BBOX
        # The original is untouched: a consumer wanting a box does not make
        # the polygon wrong for everyone else.
        assert quad.type is GeometryType.QUADRILATERAL

    def test_polygon_area_is_not_its_bounding_box_area(self) -> None:
        triangle = Geometry.polygon([(0, 0), (100, 0), (0, 100)])
        assert triangle.area == pytest.approx(5000.0)
        assert triangle.to_bbox().area == pytest.approx(10000.0)

    def test_rotation_is_reported_for_a_skewed_quad(self) -> None:
        quad = Geometry.quad([(0, 0), (100, 100), (90, 110), (-10, 10)])
        assert quad.rotation_degrees == pytest.approx(45.0, abs=1.0)

    def test_iou_refuses_to_compare_across_coordinate_spaces(self) -> None:
        """Comparing a crop coordinate against an original-image coordinate is
        a bug; returning a plausible overlap would hide it."""
        original = Geometry.bbox(
            0, 0, 100, 100, space=CoordinateSpace.ORIGINAL_IMAGE_PX
        )
        derived = Geometry.bbox(
            0, 0, 100, 100, space=CoordinateSpace.DERIVED_IMAGE_PX
        )
        assert original.iou(derived) == 0.0
        assert original.iou(original) == pytest.approx(1.0)

    def test_a_quadrilateral_needs_exactly_four_points(self) -> None:
        with pytest.raises(ValueError, match="exactly 4 points"):
            Geometry(
                type=GeometryType.QUADRILATERAL,
                points=(
                    Geometry.bbox(0, 0, 1, 1).points[0],
                    Geometry.bbox(0, 0, 1, 1).points[1],
                    Geometry.bbox(0, 0, 1, 1).points[2],
                ),
            )

    def test_transform_chain_maps_a_region_back_to_the_original(self) -> None:
        """§18 must be able to answer 'which pixels caused this fact' even when
        OCR ran on a cropped, scaled image."""
        from app.domain.geometry import TransformChain, TransformStep

        # Crop 50px in, then scale 2x — what a rectified ROI pass would do.
        crop = TransformStep(
            kind="crop",
            matrix=((1.0, 0.0, -50.0), (0.0, 1.0, -50.0), (0.0, 0.0, 1.0)),
        )
        scale = TransformStep(
            kind="scale",
            matrix=((2.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 1.0)),
        )
        chain = TransformChain().then(crop).then(scale)

        found_in_crop = Geometry.bbox(
            100, 100, 200, 140, space=CoordinateSpace.DERIVED_IMAGE_PX
        )
        original = chain.to_source(found_in_crop)

        left, top, right, bottom = original.bounds
        assert (left, top) == pytest.approx((100.0, 100.0))
        assert (right, bottom) == pytest.approx((150.0, 120.0))
        assert original.coordinate_space is CoordinateSpace.ORIGINAL_IMAGE_PX


# ---------------------------------------------------------------------------
# Snapshot integrity
# ---------------------------------------------------------------------------

class TestSnapshotIntegrity:
    def test_the_content_hash_ignores_the_run_id(self) -> None:
        """The run identifier names an execution, not a result, so it is not
        part of the document's identity. Note this is a weaker promise than
        stability across reruns — the per-run fact and region ids inside the
        document still move, which is what `semantic_hash` exists for."""
        def make(run_id: str) -> PackageFactSnapshot:
            facts = FactSet()
            facts.add(
                ResolvedFact(
                    fact_id="F-fixed",
                    field="package.mrp",
                    status=FactStatus.OBSERVED,
                    value={"amount": 120.0, "currency": "INR"},
                    confidence=0.95,
                    provenance=a_provenance(),
                )
            )
            return build_snapshot(
                package_id="PKG-1",
                facts=facts,
                jurisdiction=Jurisdiction(),
                commercial_context=a_context(),
                extraction_run_id=run_id,
            )

        assert make("RUN-1").content_hash() == make("RUN-2").content_hash()

    def test_a_changed_value_changes_the_hash(self) -> None:
        def make(amount: float) -> PackageFactSnapshot:
            facts = FactSet()
            facts.add(
                ResolvedFact(
                    fact_id="F-fixed",
                    field="package.mrp",
                    status=FactStatus.OBSERVED,
                    value={"amount": amount, "currency": "INR"},
                    confidence=0.95,
                    provenance=a_provenance(),
                )
            )
            return build_snapshot(
                package_id="PKG-1",
                facts=facts,
                jurisdiction=Jurisdiction(),
                commercial_context=a_context(),
                extraction_run_id="RUN-1",
            )

        assert make(120.0).content_hash() != make(180.0).content_hash()

    def test_a_run_id_is_required(self) -> None:
        facts = FactSet()
        facts.add(ResolvedFact.unknown("package.mrp"))
        with pytest.raises(SnapshotValidationError, match="extraction_run_id"):
            build_snapshot(
                package_id="PKG-1",
                facts=facts,
                jurisdiction=Jurisdiction(),
                commercial_context=a_context(),
                extraction_run_id="",
            )

    def test_an_unrecognised_field_is_refused(self) -> None:
        """A typo'd field name silently fails to project, so it is rejected
        rather than accepted and quietly dropped."""
        facts = FactSet()
        facts.add(ResolvedFact.unknown("package.mrpp"))
        with pytest.raises(SnapshotValidationError, match="not a projectable"):
            build(facts)

    def test_the_schema_version_is_the_frozen_one(self) -> None:
        facts = FactSet()
        facts.add(ResolvedFact.unknown("package.mrp"))
        assert build(facts).to_json()["schema_version"] == "package-facts/1.1"
