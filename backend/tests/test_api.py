"""The HTTP surface, exercised end to end.

Drives the four-endpoint contract the way the mobile client does: open a
session, upload artifacts, submit a job, fetch the snapshot. Uses the stub OCR
engine and filesystem storage so the whole flow runs with no external service.

The rule these tests defend hardest is §20's: no endpoint may return a
compliance verdict. The statuses are pipeline states, and a test asserts that
the vocabulary stays that way.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.api import routes  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import app  # noqa: E402
from app.pipeline.ocr import OcrLine, StubOcrEngine  # noqa: E402
from app.pipeline.runner import PipelineConfig  # noqa: E402
from app.domain.geometry import Geometry  # noqa: E402


SESSION_ID = "CS-test-1"
PACKAGE_ID = "PKG-test-1"


@pytest.fixture
def engine() -> StubOcrEngine:
    return StubOcrEngine()


@pytest.fixture
def client(tmp_path: Path, engine: StubOcrEngine, monkeypatch):
    """A client backed by a scratch database and artifact store."""
    settings = Settings(
        work_dir=tmp_path / "work",
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'test.db').as_posix()}",
        s3_endpoint="",
        ocr_engine="stub",
    )

    app.dependency_overrides[routes.get_settings] = lambda: settings
    # The stub has to be the engine the pipeline actually uses; otherwise the
    # route would fall back to tesseract and the test would depend on a binary.
    monkeypatch.setattr(
        routes, "build_pipeline_config", lambda s: PipelineConfig(engine=engine)
    )

    routes._engine_cache.clear()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    routes._engine_cache.clear()


def a_context() -> dict:
    return {
        "jurisdiction": {"country": "IN", "state": "Maharashtra"},
        "sale_channel": "RETAIL",
        "is_imported": False,
        "is_for_retail": True,
        "is_ecommerce_listing": False,
    }


def open_session(client: TestClient) -> None:
    response = client.post(
        "/v1/capture-sessions",
        json={
            "capture_session_id": SESSION_ID,
            "package_id": PACKAGE_ID,
            "client": {
                "platform": "android",
                "app_version": "1.0.0+1",
                "device_model": "test",
            },
            "context": a_context(),
        },
    )
    assert response.status_code == 201, response.text


def upload(
    client: TestClient, artifact_id: str, payload: bytes, surface: str = "front"
) -> str:
    sha = hashlib.sha256(payload).hexdigest()
    bundle = {
        "schemaVersion": "lm-capture-metadata/1.1",
        "ids": {
            "artifactId": artifact_id,
            "captureSessionId": SESSION_ID,
            "packageId": PACKAGE_ID,
            "captureId": artifact_id.removeprefix("IMG-"),
        },
        "file": {"sha256": sha},
        "surface": {"surfaceId": surface, "surfaceLabel": surface.title()},
        "quality": {"sourceWidth": 4032, "sourceHeight": 3024},
    }
    response = client.post(
        "/v1/artifacts",
        files={
            "image": (f"{artifact_id}.jpg", payload, "image/jpeg"),
            "metadata": ("metadata.json", json.dumps(bundle), "application/json"),
        },
    )
    assert response.status_code == 201, response.text
    return sha


def submit(client: TestClient, images: list[dict], complete: bool = True):
    return client.post(
        "/v1/extraction-jobs",
        json={
            "capture_session_id": SESSION_ID,
            "package_id": PACKAGE_ID,
            "context": a_context(),
            "images": images,
            "coverage": {"complete": complete, "required_surfaces": 4},
        },
    )


def line(text: str, top: int) -> OcrLine:
    return OcrLine(
        text=text,
        geometry=Geometry.bbox(100, top, 420, top + 40),
        confidence=0.96,
    )


class TestHealth:
    def test_reports_the_uncalibrated_policy_as_a_warning(
        self, client: TestClient
    ) -> None:
        """§14: the service says out loud that its thresholds are guesses."""
        body = client.get("/v1/health").json()
        assert body["status"] == "degraded"
        assert body["quality_policy_calibrated"] is False
        assert any("uncalibrated" in w.lower() for w in body["warnings"])


class TestFullFlow:
    def test_session_artifact_job_snapshot(
        self, client: TestClient, engine: StubOcrEngine
    ) -> None:
        open_session(client)
        payload = b"front-image-bytes"
        sha = upload(client, "IMG-1", payload)

        # The stub is keyed by the path the runner materialises, so script it
        # against whatever local file the job creates.
        engine.recognize = lambda path: __import__(
            "app.pipeline.ocr", fromlist=["OcrResult"]
        ).OcrResult(
            lines=(
                line("Net Quantity: 500 g", 200),
                line("MRP Rs. 120", 300),
                line("Country of Origin: India", 400),
            ),
            engine="stub",
            model_id="stub-ocr",
            model_version="1.0.0",
            languages=("en",),
        )

        response = submit(
            client,
            [{"artifact_id": "IMG-1", "sha256": sha, "sequence_index": 1}],
        )
        assert response.status_code == 202, response.text
        job = response.json()
        assert job["status"] == "EXTRACTION_READY"
        assert job["snapshot_available"] is True

        snapshot_response = client.get(
            f"/v1/extraction-jobs/{job['extraction_run_id']}/snapshot"
        )
        assert snapshot_response.status_code == 200
        snapshot = snapshot_response.json()["snapshot"]

        assert snapshot["schema_version"] == "package-facts/1.1"
        assert snapshot["package"]["mrp"] == {"amount": 120.0, "currency": "INR"}
        assert snapshot["declarations"]["country_of_origin"] == "India"
        assert snapshot["commercial_context"]["sale_channel"] == "RETAIL"

    def test_the_observation_trail_is_retrievable(
        self, client: TestClient, engine: StubOcrEngine
    ) -> None:
        open_session(client)
        sha = upload(client, "IMG-1", b"bytes")
        response = submit(
            client, [{"artifact_id": "IMG-1", "sha256": sha, "sequence_index": 1}]
        )
        run_id = response.json()["extraction_run_id"]

        trail = client.get(f"/v1/extraction-jobs/{run_id}/observations")
        assert trail.status_code == 200
        assert trail.json()["package_id"] == PACKAGE_ID


class TestContextIsRequired:
    @pytest.mark.parametrize(
        "missing",
        ["sale_channel", "is_imported", "is_for_retail", "is_ecommerce_listing"],
    )
    def test_a_missing_applicability_flag_is_rejected(
        self, client: TestClient, missing: str
    ) -> None:
        """§2: an applicability flag must be declared, never defaulted. A
        missing one is a 422, not a False."""
        context = a_context()
        del context[missing]
        response = client.post(
            "/v1/capture-sessions",
            json={
                "capture_session_id": "CS-x",
                "package_id": "PKG-x",
                "context": context,
            },
        )
        assert response.status_code == 422


class TestIdempotency:
    def test_reopening_a_session_is_not_an_error(self, client: TestClient) -> None:
        """The device's queue retries after a response it never saw."""
        open_session(client)
        response = client.post(
            "/v1/capture-sessions",
            json={
                "capture_session_id": SESSION_ID,
                "package_id": PACKAGE_ID,
                "context": a_context(),
            },
        )
        assert response.status_code == 201
        assert response.json()["already_existed"] is True

    def test_reuploading_the_same_artifact_succeeds(
        self, client: TestClient
    ) -> None:
        open_session(client)
        payload = b"same-bytes"
        first = upload(client, "IMG-1", payload)
        second = upload(client, "IMG-1", payload)
        assert first == second


class TestOrderingAndIntegrity:
    def test_an_artifact_without_a_session_is_refused(
        self, client: TestClient
    ) -> None:
        payload = b"orphan"
        sha = hashlib.sha256(payload).hexdigest()
        bundle = {
            "ids": {
                "artifactId": "IMG-9",
                "captureSessionId": "CS-nonexistent",
                "packageId": PACKAGE_ID,
            },
            "file": {"sha256": sha},
        }
        response = client.post(
            "/v1/artifacts",
            files={
                "image": ("a.jpg", payload, "image/jpeg"),
                "metadata": ("m.json", json.dumps(bundle), "application/json"),
            },
        )
        assert response.status_code == 409

    def test_a_hash_mismatch_is_rejected(self, client: TestClient) -> None:
        """§18: evidence whose bytes changed in transit is not evidence."""
        open_session(client)
        bundle = {
            "ids": {
                "artifactId": "IMG-bad",
                "captureSessionId": SESSION_ID,
                "packageId": PACKAGE_ID,
            },
            "file": {"sha256": "0" * 64},
        }
        response = client.post(
            "/v1/artifacts",
            files={
                "image": ("a.jpg", b"real-bytes", "image/jpeg"),
                "metadata": ("m.json", json.dumps(bundle), "application/json"),
            },
        )
        assert response.status_code == 422
        assert "mismatch" in response.text

    def test_a_job_naming_an_unuploaded_artifact_is_refused(
        self, client: TestClient
    ) -> None:
        open_session(client)
        response = submit(
            client,
            [{"artifact_id": "IMG-never", "sha256": "a" * 64, "sequence_index": 1}],
        )
        assert response.status_code == 409

    def test_an_empty_image_list_is_rejected_by_the_schema(
        self, client: TestClient
    ) -> None:
        open_session(client)
        assert submit(client, []).status_code == 422


class TestNoComplianceVerdicts:
    """§20: the extraction service must not expose a legal PASS/FAIL as its own
    authoritative result."""

    def test_the_status_vocabulary_is_pipeline_states_only(
        self, client: TestClient
    ) -> None:
        open_session(client)
        sha = upload(client, "IMG-1", b"bytes")
        job = submit(
            client, [{"artifact_id": "IMG-1", "sha256": sha, "sequence_index": 1}]
        ).json()

        allowed = {
            "EXTRACTION_READY",
            "NEEDS_RECAPTURE",
            "INCOMPLETE_EVIDENCE",
            "QUARANTINED",
            "EXTRACTION_ERROR",
        }
        assert job["status"] in allowed

    def test_no_verdict_language_appears_in_a_response(
        self, client: TestClient
    ) -> None:
        open_session(client)
        sha = upload(client, "IMG-1", b"bytes")
        job = submit(
            client, [{"artifact_id": "IMG-1", "sha256": sha, "sequence_index": 1}]
        )
        body = job.text.upper()
        for forbidden in ("PASS", "FAIL", "COMPLIANT", "NOT_APPLICABLE", "VIOLATION"):
            assert forbidden not in body, f"{forbidden} leaked into a response"

    def test_incomplete_coverage_is_reported_as_a_pipeline_state(
        self, client: TestClient
    ) -> None:
        """A package the inspector could not fully photograph is
        INCOMPLETE_EVIDENCE — a statement about this system, not about the
        package's labelling."""
        open_session(client)
        sha = upload(client, "IMG-1", b"bytes")
        job = submit(
            client,
            [{"artifact_id": "IMG-1", "sha256": sha, "sequence_index": 1}],
            complete=False,
        ).json()
        assert job["status"] == "INCOMPLETE_EVIDENCE"
