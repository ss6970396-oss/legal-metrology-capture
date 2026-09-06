"""Multi-image fact resolution.

§15's rules, tested: repeated values merge as supporting evidence, near
duplicates do not count as independent confirmation, and genuine disagreement
becomes CONFLICTING rather than a silent pick.
"""

from __future__ import annotations

import pytest

from app.domain.facts import FactStatus
from app.domain.geometry import Geometry
from app.domain.observation import (
    FieldCandidate,
    ObservationStore,
    SurfaceView,
)
from app.pipeline import resolver


def candidate(
    field: str,
    value,
    *,
    artifact: str = "IMG-1",
    region: str = "REG-1",
    attribution: float = 0.94,
    recognition: float = 0.97,
    geometry: Geometry | None = None,
    surface: SurfaceView = SurfaceView.FRONT,
) -> FieldCandidate:
    return FieldCandidate(
        candidate_id=f"CAND-{artifact}-{region}-{field}",
        field=field,
        observation_id=f"OBS-{artifact}-{region}",
        region_id=region,
        artifact_id=artifact,
        ocr_run_id=f"OCR-{artifact}",
        geometry=geometry or Geometry.bbox(100, 200, 420, 260),
        raw_text=str(value),
        normalized_value=value,
        attribution_confidence=attribution,
        recognition_confidence=recognition,
        normalization_rule="test",
        region_quality=1.0,
        surface=surface,
    )


MRP_120 = {"amount": 120.0, "currency": "INR"}
MRP_180 = {"amount": 180.0, "currency": "INR"}


class TestAgreement:
    def test_the_same_value_across_views_becomes_one_observed_fact(self) -> None:
        fact = resolver.resolve_field(
            "package.mrp",
            [
                candidate("package.mrp", MRP_120, artifact="IMG-A"),
                candidate("package.mrp", MRP_120, artifact="IMG-B"),
            ],
        )
        assert fact.status is FactStatus.OBSERVED
        assert fact.value == MRP_120

    def test_agreeing_views_are_retained_as_supporting_provenance(self) -> None:
        """§15: all source evidence is preserved, not collapsed to one."""
        fact = resolver.resolve_field(
            "package.mrp",
            [
                candidate("package.mrp", MRP_120, artifact="IMG-A"),
                candidate("package.mrp", MRP_120, artifact="IMG-B"),
                candidate("package.mrp", MRP_120, artifact="IMG-C"),
            ],
        )
        assert len(fact.provenance.supporting) == 2

    def test_agreement_raises_confidence_above_a_single_view(self) -> None:
        single = resolver.resolve_field(
            "package.mrp", [candidate("package.mrp", MRP_120, artifact="IMG-A")]
        )
        triple = resolver.resolve_field(
            "package.mrp",
            [
                candidate("package.mrp", MRP_120, artifact="IMG-A"),
                candidate("package.mrp", MRP_120, artifact="IMG-B"),
                candidate("package.mrp", MRP_120, artifact="IMG-C"),
            ],
        )
        assert triple.confidence > single.confidence

    def test_two_regions_on_one_photograph_are_not_independent(self) -> None:
        """§15 warns against counting repeated evidence as independent
        confirmation. Two readings of one panel is one view."""
        same_image = resolver.resolve_field(
            "package.mrp",
            [
                candidate(
                    "package.mrp", MRP_120, artifact="IMG-A", region="REG-1",
                    geometry=Geometry.bbox(0, 0, 100, 50),
                ),
                candidate(
                    "package.mrp", MRP_120, artifact="IMG-A", region="REG-2",
                    geometry=Geometry.bbox(500, 500, 600, 550),
                ),
            ],
        )
        two_images = resolver.resolve_field(
            "package.mrp",
            [
                candidate("package.mrp", MRP_120, artifact="IMG-A"),
                candidate("package.mrp", MRP_120, artifact="IMG-B"),
            ],
        )
        assert same_image.confidence < two_images.confidence

    def test_an_overlapping_redetection_is_dropped(self) -> None:
        """Two engines over the same region look like corroboration and are
        not."""
        fact = resolver.resolve_field(
            "package.mrp",
            [
                candidate(
                    "package.mrp", MRP_120, artifact="IMG-A", region="REG-1",
                    geometry=Geometry.bbox(100, 200, 420, 260),
                ),
                candidate(
                    "package.mrp", MRP_120, artifact="IMG-A", region="REG-2",
                    geometry=Geometry.bbox(102, 201, 418, 262),
                ),
            ],
        )
        assert fact.provenance.supporting == ()


class TestConflict:
    def test_comparable_disagreement_becomes_conflicting(self) -> None:
        fact = resolver.resolve_field(
            "package.mrp",
            [
                candidate("package.mrp", MRP_120, artifact="IMG-A",
                          recognition=0.98),
                candidate("package.mrp", MRP_180, artifact="IMG-B",
                          recognition=0.95),
            ],
        )
        assert fact.status is FactStatus.CONFLICTING
        assert fact.value is None

    def test_a_conflict_retains_every_candidate(self) -> None:
        """§15: the conflict must remain inspectable rather than being hidden
        by whichever OCR result ran last."""
        fact = resolver.resolve_field(
            "package.mrp",
            [
                candidate("package.mrp", MRP_120, artifact="IMG-A"),
                candidate("package.mrp", MRP_180, artifact="IMG-B"),
            ],
        )
        values = {c.value["amount"] for c in fact.conflicts}
        assert values == {120.0, 180.0}
        assert all(c.provenance is not None for c in fact.conflicts)

    def test_a_decisive_gap_resolves_but_records_the_rejected_reading(
        self,
    ) -> None:
        fact = resolver.resolve_field(
            "package.mrp",
            [
                candidate("package.mrp", MRP_120, artifact="IMG-A",
                          recognition=0.99, attribution=0.97),
                candidate("package.mrp", MRP_180, artifact="IMG-B",
                          recognition=0.30, attribution=0.40),
            ],
        )
        assert fact.status is FactStatus.OBSERVED
        assert fact.value == MRP_120
        assert "180" in fact.note and "rejected" in fact.note.lower()

    def test_equivalent_units_are_not_a_conflict(self) -> None:
        """0.5 kg on the front and 500 g on the back is one declaration read
        twice, not a disagreement."""
        fact = resolver.resolve_field(
            "package.net_quantity",
            [
                candidate("package.net_quantity", {"value": 0.5, "unit": "kg"},
                          artifact="IMG-A"),
                candidate("package.net_quantity", {"value": 500, "unit": "g"},
                          artifact="IMG-B"),
            ],
        )
        assert fact.status is FactStatus.OBSERVED

    def test_different_date_precisions_are_not_a_conflict(self) -> None:
        fact = resolver.resolve_field(
            "declarations.manufacture_date",
            [
                candidate("declarations.manufacture_date", "2026-07",
                          artifact="IMG-A"),
                candidate("declarations.manufacture_date", "2026-07-14",
                          artifact="IMG-B"),
            ],
        )
        assert fact.status is FactStatus.OBSERVED
        # The richer reading survives the merge.
        assert fact.value == "2026-07-14"

    def test_different_months_are_a_conflict(self) -> None:
        fact = resolver.resolve_field(
            "declarations.manufacture_date",
            [
                candidate("declarations.manufacture_date", "2026-07",
                          artifact="IMG-A"),
                candidate("declarations.manufacture_date", "2026-08",
                          artifact="IMG-B"),
            ],
        )
        assert fact.status is FactStatus.CONFLICTING

    def test_the_same_party_with_different_address_detail_merges(self) -> None:
        fact = resolver.resolve_field(
            "declarations.manufacturer",
            [
                candidate(
                    "declarations.manufacturer",
                    {"name": "Acme Foods Ltd", "address": "Pune 411001"},
                    artifact="IMG-A",
                ),
                candidate(
                    "declarations.manufacturer",
                    {"name": "Acme Foods Ltd", "address": "Pune"},
                    artifact="IMG-B",
                ),
            ],
        )
        assert fact.status is FactStatus.OBSERVED
        assert fact.value["address"] == "Pune 411001"


class TestUnknownNeverBecomesAbsence:
    def test_no_candidates_yields_unknown(self) -> None:
        fact = resolver.resolve_field("package.mrp", [])
        assert fact.status is FactStatus.UNKNOWN

    def test_a_full_resolve_emits_unknown_for_every_unseen_field(self) -> None:
        """§2: absence of an observation is extraction uncertainty. Nothing in
        a resolve over an empty store may produce DECLARED_ABSENCE."""
        store = ObservationStore(extraction_run_id="RUN-1", package_id="PKG-1")
        facts = resolver.resolve(store)

        statuses = {f.status for f in facts.ordered()}
        assert FactStatus.DECLARED_ABSENCE not in statuses
        for field in resolver.ALWAYS_RESOLVED:
            assert facts.get(field).status is FactStatus.UNKNOWN


class TestDerivedUnitSalePrice:
    def _store_with(self, *candidates: FieldCandidate) -> ObservationStore:
        store = ObservationStore(extraction_run_id="RUN-1", package_id="PKG-1")
        for c in candidates:
            store.add_candidate(c)
        return store

    def test_derives_when_nothing_is_printed(self) -> None:
        store = self._store_with(
            candidate("package.mrp", MRP_120),
            candidate("package.net_quantity", {"value": 500, "unit": "g"}),
        )
        fact = resolver.resolve(store).get("package.unit_sale_price")
        assert fact.status is FactStatus.DERIVED
        assert fact.value["amount"] == pytest.approx(0.24)

    def test_a_derived_fact_carries_a_trace_to_both_sources(self) -> None:
        store = self._store_with(
            candidate("package.mrp", MRP_120),
            candidate("package.net_quantity", {"value": 500, "unit": "g"}),
        )
        facts = resolver.resolve(store)
        fact = facts.get("package.unit_sale_price")
        assert fact.derivation is not None
        assert set(fact.derivation.source_fact_ids) == {
            facts.get("package.mrp").fact_id,
            facts.get("package.net_quantity").fact_id,
        }

    def test_a_printed_declaration_is_never_overwritten(self) -> None:
        """§8: a computed value must not replace an observed declaration, even
        when they disagree."""
        printed = {
            "amount": 0.30,
            "currency": "INR",
            "per": {"value": 1, "unit": "g"},
        }
        store = self._store_with(
            candidate("package.mrp", MRP_120),
            candidate("package.net_quantity", {"value": 500, "unit": "g"}),
            candidate("package.unit_sale_price", printed),
        )
        fact = resolver.resolve(store).get("package.unit_sale_price")
        assert fact.status is FactStatus.OBSERVED
        assert fact.value["amount"] == pytest.approx(0.30)

    def test_a_conflicting_printed_price_is_not_papered_over(self) -> None:
        store = self._store_with(
            candidate("package.mrp", MRP_120),
            candidate("package.net_quantity", {"value": 500, "unit": "g"}),
            candidate(
                "package.unit_sale_price",
                {"amount": 0.30, "currency": "INR",
                 "per": {"value": 1, "unit": "g"}},
                artifact="IMG-A",
            ),
            candidate(
                "package.unit_sale_price",
                {"amount": 0.50, "currency": "INR",
                 "per": {"value": 1, "unit": "g"}},
                artifact="IMG-B",
            ),
        )
        fact = resolver.resolve(store).get("package.unit_sale_price")
        assert fact.status is FactStatus.CONFLICTING

    def test_derived_confidence_never_exceeds_its_weakest_input(self) -> None:
        store = self._store_with(
            candidate("package.mrp", MRP_120, recognition=0.99),
            candidate(
                "package.net_quantity", {"value": 500, "unit": "g"},
                recognition=0.55, attribution=0.60,
            ),
        )
        facts = resolver.resolve(store)
        derived = facts.get("package.unit_sale_price")
        assert derived.confidence <= facts.get("package.net_quantity").confidence


class TestPhysicalMeasurement:
    def test_letter_height_is_reported_not_testable(self) -> None:
        """§10: v1 preserves the text evidence and surfaces the measurement as
        not testable rather than estimating millimetres from pixels."""
        store = ObservationStore(extraction_run_id="RUN-1", package_id="PKG-1")
        facts = resolver.resolve(store)
        fact = facts.get("presentation.letter_height_mm")
        assert fact.status is FactStatus.NOT_TESTABLE
        assert "scale" in fact.note.lower()

    def test_a_focal_length_alone_does_not_enable_measurement(self) -> None:
        from app.pipeline.physical_measurement import assess_measurability

        assessment = assess_measurability({"focal_length_mm": 4.25})
        assert assessment.testable is False

    def test_an_unvalidated_scale_is_refused(self) -> None:
        from app.pipeline.physical_measurement import (
            ScaleEstimate,
            ScaleSource,
            assess_measurability,
        )

        assessment = assess_measurability(
            {},
            ScaleEstimate(
                mm_per_pixel=0.05,
                source=ScaleSource.ARCORE_DEPTH,
                error_bound_mm=0.2,
                validated=False,
            ),
        )
        assert assessment.testable is False
        assert "validation gate" in assessment.reason

    def test_a_validated_scale_returns_a_value_with_its_error_bound(self) -> None:
        from app.pipeline.physical_measurement import (
            ScaleEstimate,
            ScaleSource,
            assess_measurability,
            measure_letter_height_mm,
        )

        assessment = assess_measurability(
            {},
            ScaleEstimate(
                mm_per_pixel=0.05,
                source=ScaleSource.REFERENCE_OBJECT,
                error_bound_mm=0.2,
                validated=True,
            ),
        )
        assert assessment.testable is True
        measured = measure_letter_height_mm(40.0, assessment)
        assert measured == (pytest.approx(2.0), pytest.approx(0.2))


class TestCrossArtifactChecks:
    def test_incompatible_quantities_raise_a_package_identity_warning(self) -> None:
        """§13: images that appear to show different packages must not have
        their facts merged."""
        store = ObservationStore(extraction_run_id="RUN-1", package_id="PKG-1")
        store.add_candidate(
            candidate("package.net_quantity", {"value": 500, "unit": "g"},
                      artifact="IMG-A")
        )
        store.add_candidate(
            candidate("package.net_quantity", {"value": 250, "unit": "g"},
                      artifact="IMG-B")
        )
        warnings = resolver.check_package_identity(store)
        assert warnings
        assert set(warnings[0].artifact_ids) == {"IMG-A", "IMG-B"}

    def test_a_consistent_capture_set_raises_no_warning(self) -> None:
        store = ObservationStore(extraction_run_id="RUN-1", package_id="PKG-1")
        store.add_candidate(
            candidate("package.net_quantity", {"value": 500, "unit": "g"},
                      artifact="IMG-A")
        )
        store.add_candidate(
            candidate("package.net_quantity", {"value": 500, "unit": "g"},
                      artifact="IMG-B")
        )
        assert resolver.check_package_identity(store) == []


class TestConfidence:
    def test_a_weak_component_drags_the_result_down(self) -> None:
        """A perfect OCR read attributed to the wrong field is not 'good on
        average' — it is wrong."""
        strong = resolver.combine_confidence(
            ocr=0.99, attribution=0.95, region_quality=1.0, agreement=1.0
        )
        bad_attribution = resolver.combine_confidence(
            ocr=0.99, attribution=0.20, region_quality=1.0, agreement=1.0
        )
        assert bad_attribution < strong / 3

    def test_the_result_stays_within_zero_and_one(self) -> None:
        assert resolver.combine_confidence(
            ocr=1.0, attribution=1.0, region_quality=1.0, agreement=1.15
        ) <= 1.0

    def test_the_breakdown_records_its_policy_version(self) -> None:
        fact = resolver.resolve_field(
            "package.mrp", [candidate("package.mrp", MRP_120)]
        )
        assert fact.confidence_breakdown is not None
        assert fact.confidence_breakdown.policy_version == (
            resolver.CONFIDENCE_POLICY_VERSION
        )
