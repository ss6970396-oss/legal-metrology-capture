"""End-to-end extraction, from artifacts to a validated snapshot.

Uses the stub OCR engine so the whole pipeline — quality, detection,
recognition, attribution, normalization, resolution, projection, validation —
runs deterministically without a model. The image bytes are real files (the
runner hashes them and the integrity check must pass) but their content does
not matter, because the recognizer's output is scripted.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.domain.facts import FactStatus
from app.domain.geometry import Geometry
from app.domain.observation import SurfaceView
from app.domain.snapshot import CommercialContext, Jurisdiction
from app.pipeline.ocr import OcrLine, StubOcrEngine
from app.pipeline.runner import (
    ArtifactInput,
    ExtractionError,
    ExtractionRequest,
    PipelineConfig,
    run_extraction,
)


def write_image(directory: Path, name: str) -> tuple[Path, str]:
    """Create a file and return it with its hash."""
    path = directory / name
    payload = f"fake-jpeg-bytes-for-{name}".encode()
    path.write_bytes(payload)
    return path, hashlib.sha256(payload).hexdigest()


def line(text: str, top: int, left: int = 100) -> OcrLine:
    """A recognised line at a given position, 40px tall."""
    return OcrLine(
        text=text,
        geometry=Geometry.bbox(left, top, left + 320, top + 40),
        confidence=0.96,
    )


def a_request(artifacts: list[ArtifactInput], **overrides) -> ExtractionRequest:
    return ExtractionRequest(
        capture_session_id=overrides.get("session", "CS-1"),
        package_id=overrides.get("package", "PKG-1"),
        jurisdiction=Jurisdiction(country="IN"),
        commercial_context=CommercialContext(
            sale_channel="RETAIL",
            is_imported=overrides.get("imported", False),
            is_for_retail=True,
            is_ecommerce_listing=False,
        ),
        artifacts=artifacts,
        coverage=overrides.get("coverage", {"complete": True}),
    )


def an_artifact(
    path: Path, sha: str, *, artifact_id: str = "IMG-1", index: int = 1,
    surface: SurfaceView = SurfaceView.FRONT,
) -> ArtifactInput:
    return ArtifactInput(
        artifact_id=artifact_id,
        sha256=sha,
        local_path=path,
        sequence_index=index,
        captured_at=datetime.now(timezone.utc),
        width_px=4032,
        height_px=3024,
        surface=surface,
    )


@pytest.fixture
def engine() -> StubOcrEngine:
    return StubOcrEngine()


@pytest.fixture
def config(engine: StubOcrEngine) -> PipelineConfig:
    return PipelineConfig(engine=engine)


class TestHappyPath:
    def test_a_labelled_panel_produces_observed_facts(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        path, sha = write_image(tmp_path, "front.jpg")
        engine.script(
            str(path),
            [
                line("Refined Sunflower Oil", 100),
                line("Net Quantity: 500 g", 200),
                line("MRP Rs. 120 (incl. of all taxes)", 300),
                line("Mfg Date: 07/2026", 400),
                line("Best Before: 01/2027", 500),
                line("Manufactured by: Acme Foods Ltd, Pune 411001", 600),
                line("Country of Origin: India", 700),
            ],
        )

        result = run_extraction(a_request([an_artifact(path, sha)]), config)

        assert result.released
        snapshot = result.snapshot.to_json()
        assert snapshot["package"]["mrp"] == {"amount": 120.0, "currency": "INR"}
        assert snapshot["package"]["net_quantity"] == {"value": 500, "unit": "g"}
        assert snapshot["declarations"]["country_of_origin"] == "India"
        assert snapshot["declarations"]["manufacture_date"] == "2026-07"
        assert snapshot["declarations"]["best_before"] == "2027-01"
        assert snapshot["declarations"]["manufacturer"]["name"] == "Acme Foods Ltd"

    def test_every_fact_carries_provenance_back_to_pixels(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        """§17: 100% provenance completeness on authoritative facts."""
        path, sha = write_image(tmp_path, "front.jpg")
        engine.script(
            str(path),
            [line("MRP Rs. 120", 300), line("Net Quantity: 500 g", 200)],
        )

        result = run_extraction(a_request([an_artifact(path, sha)]), config)

        for fact in result.snapshot.to_json()["facts"]:
            if fact["status"] in ("OBSERVED", "DERIVED"):
                provenance = fact["provenance"]
                assert provenance["artifact_id"]
                assert provenance["region_id"]
                assert provenance["ocr_run_id"]
                assert provenance["geometry"]["coordinates"]

    def test_the_observation_trail_survives_the_run(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        """The snapshot says what was concluded; the store says what from."""
        path, sha = write_image(tmp_path, "front.jpg")
        engine.script(
            str(path),
            [line("MRP Rs. 120", 300), line("Some brand text", 50)],
        )

        result = run_extraction(a_request([an_artifact(path, sha)]), config)
        payload = result.store.to_json()

        assert len(payload["artifacts"]) == 1
        assert len(payload["regions"]) == 2
        assert len(payload["observations"]) == 2
        # The brand line produced no candidate but is still on the record.
        raw = {o["raw_text"] for o in payload["observations"]}
        assert "Some brand text" in raw

    def test_derived_unit_price_appears_when_none_is_printed(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        path, sha = write_image(tmp_path, "front.jpg")
        engine.script(
            str(path),
            [line("MRP Rs. 120", 300), line("Net Quantity: 500 g", 200)],
        )

        result = run_extraction(a_request([an_artifact(path, sha)]), config)
        facts = {f["field"]: f for f in result.snapshot.to_json()["facts"]}
        unit_price = facts["package.unit_sale_price"]

        assert unit_price["status"] == "DERIVED"
        assert unit_price["value"]["amount"] == pytest.approx(0.24)
        assert unit_price["derivation"]["rule_id"]


class TestMultipleViews:
    def test_two_faces_merge_into_one_fact_set(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        front, front_sha = write_image(tmp_path, "front.jpg")
        back, back_sha = write_image(tmp_path, "back.jpg")
        engine.script(str(front), [line("Net Quantity: 500 g", 200)])
        engine.script(
            str(back),
            [
                line("MRP Rs. 120", 300),
                line("Manufactured by: Acme Foods Ltd, Pune", 400),
            ],
        )

        result = run_extraction(
            a_request(
                [
                    an_artifact(front, front_sha, artifact_id="IMG-1", index=1),
                    an_artifact(
                        back, back_sha, artifact_id="IMG-2", index=2,
                        surface=SurfaceView.BACK,
                    ),
                ]
            ),
            config,
        )

        snapshot = result.snapshot.to_json()
        assert snapshot["package"]["net_quantity"] == {"value": 500, "unit": "g"}
        assert snapshot["package"]["mrp"]["amount"] == 120.0

    def test_disagreeing_panels_produce_a_null_projection(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        """§15's worked example: two views, two MRPs, no silent pick."""
        front, front_sha = write_image(tmp_path, "front.jpg")
        back, back_sha = write_image(tmp_path, "back.jpg")
        engine.script(str(front), [line("MRP Rs. 120", 300)])
        engine.script(str(back), [line("MRP Rs. 180", 300)])

        result = run_extraction(
            a_request(
                [
                    an_artifact(front, front_sha, artifact_id="IMG-1", index=1),
                    an_artifact(
                        back, back_sha, artifact_id="IMG-2", index=2,
                        surface=SurfaceView.BACK,
                    ),
                ]
            ),
            config,
        )

        snapshot = result.snapshot.to_json()
        assert snapshot["package"]["mrp"] is None

        mrp = next(f for f in snapshot["facts"] if f["field"] == "package.mrp")
        assert mrp["status"] == "CONFLICTING"
        assert len(mrp["conflicts"]) == 2

    def test_near_duplicate_photographs_are_flagged(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        first, first_sha = write_image(tmp_path, "a.jpg")
        second, second_sha = write_image(tmp_path, "b.jpg")
        same = [line("MRP Rs. 120", 300), line("Net Quantity: 500 g", 200)]
        engine.script(str(first), same)
        engine.script(str(second), same)

        result = run_extraction(
            a_request(
                [
                    an_artifact(first, first_sha, artifact_id="IMG-1", index=1),
                    an_artifact(second, second_sha, artifact_id="IMG-2", index=2),
                ]
            ),
            config,
        )
        assert result.diagnostics["near_duplicate_artifacts"]


class TestUnreadEvidence:
    def test_an_unread_panel_yields_unknown_not_absence(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        """The rule with the most downstream consequence: nothing read is not
        the same as nothing printed."""
        path, sha = write_image(tmp_path, "blurred.jpg")
        engine.script(str(path), [])

        result = run_extraction(a_request([an_artifact(path, sha)]), config)

        statuses = {
            f["field"]: f["status"] for f in result.snapshot.to_json()["facts"]
        }
        assert statuses["package.mrp"] == "UNKNOWN"
        assert "DECLARED_ABSENCE" not in statuses.values()

    def test_a_price_with_no_mrp_label_is_not_attributed(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        """§5: price strings elsewhere on a package are not necessarily MRP."""
        path, sha = write_image(tmp_path, "front.jpg")
        engine.script(
            str(path),
            [line("Special offer Rs. 99", 300), line("Contest prize Rs 5000", 900)],
        )

        result = run_extraction(a_request([an_artifact(path, sha)]), config)
        assert result.snapshot.to_json()["package"]["mrp"] is None

    def test_a_neighbouring_label_does_reach_a_value(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        """Packaging separates a label from its value constantly."""
        path, sha = write_image(tmp_path, "front.jpg")
        engine.script(
            str(path),
            [line("M.R.P.", 300), line("Rs. 120", 340)],
        )

        result = run_extraction(a_request([an_artifact(path, sha)]), config)
        assert result.snapshot.to_json()["package"]["mrp"]["amount"] == 120.0

    def test_not_for_retail_sale_is_observed_without_becoming_absence(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        """§5.1: the statement is recorded; it does not turn every field into
        a declared absence."""
        path, sha = write_image(tmp_path, "front.jpg")
        engine.script(str(path), [line("NOT FOR RETAIL SALE", 100)])

        result = run_extraction(a_request([an_artifact(path, sha)]), config)

        assert "not_for_retail_sale" in result.diagnostics["observed_statements"]
        statuses = {
            f["status"] for f in result.snapshot.to_json()["facts"]
        }
        assert "DECLARED_ABSENCE" not in statuses


class TestIntegrityAndValidation:
    def test_a_tampered_artifact_is_rejected(self, tmp_path: Path, config) -> None:
        """§18's immutability control is only worth having if it is checked."""
        path, _ = write_image(tmp_path, "front.jpg")
        wrong_hash = "0" * 64

        with pytest.raises(ExtractionError, match="integrity check"):
            run_extraction(
                a_request([an_artifact(path, wrong_hash)]), config
            )

    def test_a_job_with_no_artifacts_is_refused(self, config) -> None:
        with pytest.raises(ExtractionError, match="at least one artifact"):
            run_extraction(a_request([]), config)

    def test_a_duplicated_artifact_id_is_refused(
        self, tmp_path: Path, config
    ) -> None:
        path, sha = write_image(tmp_path, "front.jpg")
        with pytest.raises(ExtractionError, match="appears twice"):
            run_extraction(
                a_request(
                    [
                        an_artifact(path, sha, artifact_id="IMG-1", index=1),
                        an_artifact(path, sha, artifact_id="IMG-1", index=2),
                    ]
                ),
                config,
            )


class TestRunMetadata:
    def test_every_component_version_is_recorded(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        """§18: a run is only reproducible if you know what produced it."""
        path, sha = write_image(tmp_path, "front.jpg")
        engine.script(str(path), [line("MRP Rs. 120", 300)])

        diagnostics = run_extraction(
            a_request([an_artifact(path, sha)]), config
        ).diagnostics

        for key in (
            "pipeline_version",
            "normalizer_version",
            "attribution_version",
            "resolver_version",
            "quality_pipeline_version",
            "ocr_engine",
            "ocr_model_id",
            "ocr_model_version",
        ):
            assert diagnostics[key], f"{key} was not recorded"

    def test_an_uncalibrated_quality_policy_is_flagged_on_every_run(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        """§14: unvalidated thresholds must not quietly become acceptance
        criteria."""
        path, sha = write_image(tmp_path, "front.jpg")
        engine.script(str(path), [line("MRP Rs. 120", 300)])

        diagnostics = run_extraction(
            a_request([an_artifact(path, sha)]), config
        ).diagnostics

        assert diagnostics["quality_policy"]["calibrated"] is False
        assert "uncalibrated" in diagnostics["quality_policy_warning"].lower()

    def test_the_semantic_hash_is_stable_across_reruns(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        """Same evidence, same result — which is the property a regression
        suite compares. The content hash differs because it covers the
        per-run identifiers; the semantic hash is what answers "did the
        result move"."""
        path, sha = write_image(tmp_path, "front.jpg")
        engine.script(str(path), [line("MRP Rs. 120", 300)])

        first = run_extraction(a_request([an_artifact(path, sha)]), config)
        second = run_extraction(a_request([an_artifact(path, sha)]), config)

        assert first.extraction_run_id != second.extraction_run_id
        assert first.snapshot.semantic_hash() == second.snapshot.semantic_hash()
        # The document hashes differ: each names a distinct run.
        assert first.snapshot.content_hash() != second.snapshot.content_hash()

    def test_a_changed_reading_moves_the_semantic_hash(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        path, sha = write_image(tmp_path, "front.jpg")

        engine.script(str(path), [line("MRP Rs. 120", 300)])
        before = run_extraction(a_request([an_artifact(path, sha)]), config)

        engine.script(str(path), [line("MRP Rs. 180", 300)])
        after = run_extraction(a_request([an_artifact(path, sha)]), config)

        assert before.snapshot.semantic_hash() != after.snapshot.semantic_hash()

    def test_the_declared_context_passes_through_untouched(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        """Applicability flags are the inspector's declaration; extraction has
        no business deducing or altering them."""
        path, sha = write_image(tmp_path, "front.jpg")
        engine.script(str(path), [line("Country of Origin: Germany", 300)])

        result = run_extraction(
            a_request([an_artifact(path, sha)], imported=True), config
        )
        context = result.snapshot.to_json()["commercial_context"]
        assert context["is_imported"] is True
        assert context["sale_channel"] == "RETAIL"


class TestFreeTextAttribution:
    """A free-text field must take its value from the labelled region only.

    Regression: consumer care was parsed from the region's text *concatenated
    with its neighbours*, so every region near the consumer-care line produced
    a differently-joined string. Those read as disagreeing values and a package
    with one printed consumer-care line resolved to CONFLICTING.
    """

    def test_one_consumer_care_line_does_not_conflict_with_itself(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        path, sha = write_image(tmp_path, "back.jpg")
        engine.script(
            str(path),
            [
                line("Mfg Date: 07/2026", 260),
                line("Best Before: 01/2027", 340),
                line("Consumer Care: care@acme.example, 1800 123 4567", 420),
                line("MRP Rs. 120", 500),
            ],
        )

        result = run_extraction(a_request([an_artifact(path, sha)]), config)
        care = next(
            f
            for f in result.snapshot.to_json()["facts"]
            if f["field"] == "declarations.consumer_care"
        )

        assert care["status"] == "OBSERVED"
        assert care["value"] == "Consumer Care: care@acme.example, 1800 123 4567"

    def test_neighbouring_regions_do_not_claim_the_field(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        """Only the region carrying the label may propose a value."""
        path, sha = write_image(tmp_path, "back.jpg")
        engine.script(
            str(path),
            [
                line("Consumer Care: care@acme.example", 400),
                line("MRP Rs. 120", 440),
            ],
        )

        result = run_extraction(a_request([an_artifact(path, sha)]), config)
        proposals = [
            c
            for c in result.store.candidates
            if c.field == "declarations.consumer_care"
        ]
        assert len(proposals) == 1

    def test_a_label_and_its_contact_on_separate_lines_still_resolve(
        self, tmp_path: Path, engine: StubOcrEngine, config: PipelineConfig
    ) -> None:
        """Packaging splits a label from its value constantly; joining to the
        one neighbour that completes it must still work."""
        path, sha = write_image(tmp_path, "back.jpg")
        engine.script(
            str(path),
            [
                line("Consumer Care:", 400),
                line("care@acme.example", 440),
            ],
        )

        result = run_extraction(a_request([an_artifact(path, sha)]), config)
        care = next(
            f
            for f in result.snapshot.to_json()["facts"]
            if f["field"] == "declarations.consumer_care"
        )
        assert care["status"] == "OBSERVED"
        assert "care@acme.example" in care["value"]
