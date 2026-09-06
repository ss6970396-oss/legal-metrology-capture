"""The extraction API.

§13's endpoint separation: session creation, artifact upload, job submission,
job status, snapshot retrieval. The split keeps the mobile client away from the
internals — it never learns which OCR engine ran or how long a job took — and
lets extraction be asynchronous without the external contract changing.

One rule governs every response here. §20: **the extraction service must not
expose a legal PASS/FAIL as its own authoritative result.** It reports pipeline
states — EXTRACTION_READY, NEEDS_RECAPTURE, INCOMPLETE_EVIDENCE, QUARANTINED,
EXTRACTION_ERROR — and those are states of this system, not statements about a
package's compliance. Nothing in this module may return a verdict, a score
implying one, or a field the client could reasonably read as one.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from .. import telemetry
from ..config import Settings, get_settings
from ..domain.observation import SurfaceView
from ..domain.snapshot import CommercialContext, Jurisdiction
from ..pipeline import ocr, quality
from ..pipeline.runner import (
    ArtifactInput,
    ExtractionError,
    ExtractionRequest,
    ExtractionResult,
    PipelineConfig,
    run_extraction,
)
from ..storage import db
from ..storage.artifacts import (
    ArtifactIntegrityError,
    StoredArtifact,
    build_store,
)
from .schemas import (
    ArtifactOut,
    CaptureSessionIn,
    CaptureSessionOut,
    ExtractionJobIn,
    ExtractionJobOut,
    HealthOut,
    QuarantineOut,
    SnapshotOut,
)

log = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

_engine_cache: dict[str, Any] = {}


def get_db(settings: Settings = Depends(get_settings)):  # noqa: ANN201
    key = f"db::{settings.database_url}"
    if key not in _engine_cache:
        _engine_cache[key] = db.build_engine(settings.database_url)
    return _engine_cache[key]


def get_artifact_store(settings: Settings = Depends(get_settings)):  # noqa: ANN201
    key = f"store::{settings.s3_endpoint}::{settings.s3_bucket}"
    if key not in _engine_cache:
        _engine_cache[key] = build_store(settings)
    return _engine_cache[key]


def build_pipeline_config(settings: Settings) -> PipelineConfig:
    """Assemble the pipeline from configuration.

    Falls back to Tesseract when the configured engine is unavailable, and
    says so loudly. A run that silently used a different recognizer than the
    one configured would produce results nobody could compare against a
    benchmark — so the substitution is logged and lands in the run's
    diagnostics through the engine identifiers.
    """
    engine_name = settings.ocr_engine
    try:
        engine = ocr.build_engine(
            engine_name, languages=settings.ocr_languages
        )
        if not engine.is_available():
            raise RuntimeError(f"{engine_name} is not installed")
    except (KeyError, RuntimeError) as exc:
        log.error(
            "configured OCR engine %r unavailable (%s); falling back to "
            "tesseract. Results are NOT comparable to a %s benchmark.",
            engine_name,
            exc,
            engine_name,
        )
        engine = ocr.build_engine("tesseract")

    policy = quality.QualityPolicy(
        calibrated=settings.quality_policy_calibrated
    )
    return PipelineConfig(
        engine=engine,
        quality_policy=policy,
        skip_ocr_on_retake=settings.skip_ocr_on_retake,
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/health", response_model=HealthOut)
def health(settings: Settings = Depends(get_settings)) -> HealthOut:
    warnings: list[str] = []
    if not settings.quality_policy_calibrated:
        warnings.append(
            "Quality thresholds are uncalibrated starting values; RETAKE "
            "verdicts are advisory and must not be treated as acceptance "
            "criteria (contract v1.1 §14)."
        )

    available = ocr.available_engines()
    if settings.ocr_engine not in available:
        warnings.append(
            f"Configured OCR engine {settings.ocr_engine!r} is not available; "
            f"installed: {available or 'none'}."
        )

    return HealthOut(
        status="degraded" if warnings else "ok",
        service=settings.service_name,
        version="1.0.0",
        ocr_engines_available=available,
        quality_policy_calibrated=settings.quality_policy_calibrated,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Capture sessions
# ---------------------------------------------------------------------------

@router.post("/capture-sessions", response_model=CaptureSessionOut, status_code=201)
def create_capture_session(
    payload: CaptureSessionIn,
    engine=Depends(get_db),  # noqa: ANN001
) -> CaptureSessionOut:
    """Open a capture session for one package.

    Idempotent on ``capture_session_id``, because the device's upload queue
    retries and a retry after an unseen response is indistinguishable from a
    first attempt. A repeat returns 201 with ``already_existed`` set rather
    than a conflict — the client's goal ("this session exists") is satisfied
    either way, and a 4xx would send it into a pointless backoff.

    A repeat does **not** overwrite the stored context. The applicability flags
    recorded when the session opened are the ones the artifacts were captured
    under.
    """
    with telemetry.span(
        "capture_session.create",
        capture_session_id=payload.capture_session_id,
        package_id=payload.package_id,
    ):
        existing = db.get_session(engine, payload.capture_session_id)
        if existing is not None:
            return CaptureSessionOut(
                capture_session_id=payload.capture_session_id,
                package_id=payload.package_id,
                created=False,
                already_existed=True,
            )

        db.record_capture_session(
            engine,
            capture_session_id=payload.capture_session_id,
            package_id=payload.package_id,
            client=payload.client.model_dump(),
            context=payload.context.model_dump(),
        )
        return CaptureSessionOut(
            capture_session_id=payload.capture_session_id,
            package_id=payload.package_id,
            created=True,
        )


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

@router.post("/artifacts", response_model=ArtifactOut, status_code=201)
async def upload_artifact(
    image: UploadFile = File(...),
    metadata: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
    engine=Depends(get_db),  # noqa: ANN001
    store=Depends(get_artifact_store),  # noqa: ANN001
) -> ArtifactOut:
    """Store one image and its metadata bundle.

    The bundle is the device's richer record — attempt history, quality
    diagnostics, EXIF, the inspection identifier. §3 keeps all of that on this
    side of the compliance boundary, so it is stored whole and never forwarded
    to Team 2.
    """
    import json

    raw_metadata = await metadata.read()
    try:
        bundle = json.loads(raw_metadata)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, f"metadata is not valid JSON: {exc}") from exc

    ids = bundle.get("ids") or {}
    artifact_id = str(ids.get("artifactId") or "")
    session_id = str(ids.get("captureSessionId") or "")
    package_id = str(ids.get("packageId") or "")
    declared_sha = str((bundle.get("file") or {}).get("sha256") or "")

    if not artifact_id or not session_id:
        raise HTTPException(
            400, "metadata must carry ids.artifactId and ids.captureSessionId"
        )

    if db.get_session(engine, session_id) is None:
        # Ordering is the client's responsibility and its queue enforces it.
        # Reaching here means the session upload failed permanently, and
        # accepting the artifact would orphan it.
        raise HTTPException(
            409,
            f"capture session {session_id} does not exist; open it before "
            "uploading artifacts",
        )

    data = await image.read()
    if len(data) > settings.max_artifact_bytes:
        raise HTTPException(
            413,
            f"artifact is {len(data)} bytes, over the "
            f"{settings.max_artifact_bytes} limit",
        )
    if not data:
        raise HTTPException(400, "artifact is empty")

    with telemetry.span(
        "artifact.upload", artifact_id=artifact_id, package_id=package_id
    ):
        try:
            stored: StoredArtifact = store.put(artifact_id, data, declared_sha)
        except ArtifactIntegrityError as exc:
            # Not retryable: the bytes that arrived are not the bytes the
            # device hashed. It still holds the original.
            raise HTTPException(422, str(exc)) from exc

        quality_block = bundle.get("quality") or {}
        surface_block = bundle.get("surface") or {}

        try:
            db.record_artifact(
                engine,
                artifact_id=artifact_id,
                capture_session_id=session_id,
                package_id=package_id,
                sha256=stored.sha256,
                uri=stored.uri,
                size_bytes=stored.size_bytes,
                client_metadata=bundle,
                width_px=quality_block.get("sourceWidth"),
                height_px=quality_block.get("sourceHeight"),
                surface=surface_block.get("surfaceId"),
                surface_label=surface_block.get("surfaceLabel"),
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return ArtifactOut(
        artifact_id=artifact_id,
        sha256=stored.sha256,
        uri=stored.uri,
        size_bytes=stored.size_bytes,
    )


# ---------------------------------------------------------------------------
# Extraction jobs
# ---------------------------------------------------------------------------

@router.post("/extraction-jobs", response_model=ExtractionJobOut, status_code=202)
def submit_extraction_job(
    payload: ExtractionJobIn,
    settings: Settings = Depends(get_settings),
    engine=Depends(get_db),  # noqa: ANN001
    store=Depends(get_artifact_store),  # noqa: ANN001
) -> ExtractionJobOut:
    """Run extraction over a session's artifacts.

    Synchronous for now, which §13 permits for short jobs. The response shape
    is already the asynchronous one — a run id the client polls — so moving the
    body onto a worker later changes nothing the client can see.
    """
    if db.get_session(engine, payload.capture_session_id) is None:
        raise HTTPException(
            409, f"capture session {payload.capture_session_id} does not exist"
        )

    if len(payload.images) > settings.max_artifacts_per_job:
        raise HTTPException(
            413,
            f"{len(payload.images)} artifacts exceeds the per-job limit of "
            f"{settings.max_artifacts_per_job}",
        )

    stored_rows = {
        row["artifact_id"]: row
        for row in db.get_session_artifacts(engine, payload.capture_session_id)
    }

    work_dir = settings.work_dir / "runs" / uuid.uuid4().hex
    work_dir.mkdir(parents=True, exist_ok=True)

    artifact_inputs: list[ArtifactInput] = []
    for entry in payload.images:
        row = stored_rows.get(entry.artifact_id)
        if row is None:
            raise HTTPException(
                409,
                f"artifact {entry.artifact_id} was listed in the job but has "
                "not been uploaded",
            )
        if row["sha256"].lower() != entry.sha256.lower():
            raise HTTPException(
                422,
                f"artifact {entry.artifact_id} hash in the job does not match "
                "the stored artifact",
            )

        local = work_dir / f"{entry.artifact_id}.bin"
        try:
            store.fetch_to(
                StoredArtifact(
                    artifact_id=entry.artifact_id,
                    sha256=row["sha256"],
                    uri=row["uri"],
                    size_bytes=row["size_bytes"],
                ),
                local,
            )
        except ArtifactIntegrityError as exc:
            raise HTTPException(500, f"stored artifact is corrupt: {exc}") from exc

        artifact_inputs.append(
            ArtifactInput(
                artifact_id=entry.artifact_id,
                sha256=row["sha256"],
                local_path=local,
                sequence_index=entry.sequence_index,
                captured_at=entry.captured_at
                or row.get("captured_at")
                or datetime.now(timezone.utc),
                width_px=entry.capture_metadata.width_px
                or (row.get("width_px") or 0),
                height_px=entry.capture_metadata.height_px
                or (row.get("height_px") or 0),
                surface=SurfaceView.parse(
                    entry.surface or row.get("surface")
                ),
                surface_label=entry.surface_label
                or (row.get("surface_label") or ""),
                orientation=entry.capture_metadata.orientation,
                focal_length_mm=entry.capture_metadata.focal_length_mm,
                uri=row["uri"],
                client_quality=(row.get("client_metadata") or {}).get(
                    "quality", {}
                ),
            )
        )

    request = ExtractionRequest(
        capture_session_id=payload.capture_session_id,
        package_id=payload.package_id,
        jurisdiction=Jurisdiction(
            country=payload.context.jurisdiction.country,
            state=payload.context.jurisdiction.state,
        ),
        commercial_context=CommercialContext(
            sale_channel=payload.context.sale_channel,
            is_imported=payload.context.is_imported,
            is_for_retail=payload.context.is_for_retail,
            is_ecommerce_listing=payload.context.is_ecommerce_listing,
        ),
        artifacts=artifact_inputs,
        coverage=payload.coverage.model_dump(),
    )

    config = build_pipeline_config(settings)

    with telemetry.span(
        "extraction.run",
        capture_session_id=payload.capture_session_id,
        package_id=payload.package_id,
    ):
        try:
            result = run_extraction(request, config)
        except ExtractionError as exc:
            telemetry.record_exception(exc)
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            telemetry.record_exception(exc)
            log.exception("extraction failed")
            raise HTTPException(500, f"extraction failed: {exc}") from exc

    db.record_run(engine, result, payload.capture_session_id)

    return ExtractionJobOut(
        extraction_run_id=result.extraction_run_id,
        capture_session_id=payload.capture_session_id,
        package_id=payload.package_id,
        status=_status_for(result, payload),
        snapshot_available=result.released,
        message=_message_for(result),
    )


def _status_for(result: ExtractionResult, payload: ExtractionJobIn) -> str:
    """Map a run onto one of §20's pipeline states.

    Pipeline states only. None of these says anything about whether the
    package complies with anything — INCOMPLETE_EVIDENCE means this system did
    not get enough photographs, not that a declaration is missing.
    """
    if result.quarantine is not None:
        return "QUARANTINED"
    if result.snapshot is None:
        return "EXTRACTION_ERROR"

    quality_verdicts = [r.verdict.value for r in result.quality_reports]
    if quality_verdicts and all(v == "RETAKE" for v in quality_verdicts):
        return "NEEDS_RECAPTURE"

    if not payload.coverage.complete:
        return "INCOMPLETE_EVIDENCE"

    return "EXTRACTION_READY"


def _message_for(result: ExtractionResult) -> str:
    if result.quarantine is not None:
        return (
            "The generated snapshot did not pass contract validation and has "
            "been quarantined for engineering review. No snapshot was "
            "released."
        )
    counts = result.diagnostics.get("fact_status_counts", {})
    return "Extraction complete. Fact statuses: " + (
        ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
    )


@router.get("/extraction-jobs/{run_id}/snapshot", response_model=SnapshotOut)
def get_snapshot(
    run_id: str,
    engine=Depends(get_db),  # noqa: ANN001
) -> SnapshotOut:
    """Retrieve a released PackageFactSnapshot.

    A quarantined run returns 409 carrying its problems, never a partial
    snapshot. §7: the inconsistency is exposed for diagnosis, not repaired and
    shipped.
    """
    row = db.get_run(engine, run_id)
    if row is None:
        raise HTTPException(404, f"no extraction run {run_id}")

    if row["status"] == "quarantined":
        quarantines = {
            q["extraction_run_id"]: q for q in db.list_quarantined(engine)
        }
        detail = quarantines.get(run_id, {})
        raise HTTPException(
            409,
            {
                "error": "snapshot_quarantined",
                "extraction_run_id": run_id,
                "reason": detail.get("reason", "unknown"),
                "problems": detail.get("problems", []),
                "snapshot_released": False,
            },
        )

    if not row.get("snapshot"):
        raise HTTPException(
            409, f"run {run_id} has status {row['status']} and no snapshot"
        )

    return SnapshotOut(
        extraction_run_id=run_id,
        package_id=row["package_id"],
        snapshot_hash=row["snapshot_hash"] or "",
        snapshot=row["snapshot"],
    )


@router.get("/extraction-jobs/{run_id}/observations")
def get_observations(
    run_id: str,
    engine=Depends(get_db),  # noqa: ANN001
) -> dict[str, Any]:
    """The full evidence trail for one run.

    Not part of the compliance contract — this is the audit surface §18 needs,
    and the answer to "which pixels caused this fact". Available for a
    quarantined run too, which is when it is most needed.
    """
    payload = db.get_observations(engine, run_id)
    if payload is None:
        raise HTTPException(404, f"no observation record for run {run_id}")
    return payload


@router.get("/quarantine", response_model=list[QuarantineOut])
def list_quarantine(engine=Depends(get_db)) -> list[QuarantineOut]:  # noqa: ANN001
    """Runs awaiting engineering review."""
    return [
        QuarantineOut(
            extraction_run_id=row["extraction_run_id"],
            package_id=row["package_id"],
            reason=row["reason"],
            problems=list(row["problems"] or []),
            quarantined_at=row["quarantined_at"],
        )
        for row in db.list_quarantined(engine)
    ]
