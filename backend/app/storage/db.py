"""Operational metadata: sessions, artifacts, runs, snapshots, quarantines.

§12 puts "queryable run metadata, observations, reconciliation state and audit
indices" in PostgreSQL. The schema below is that, with one bias running through
it: **rows are append-mostly**. An extraction run is a historical event, and a
schema that let a later run overwrite an earlier one's record would destroy the
audit trail §18 requires.

So a re-extraction of the same package writes a new run row. Nothing updates a
completed run in place.

SQLAlchemy Core rather than the ORM. The access patterns here are "insert a
run, fetch a run, list runs for a package" — an ORM's identity map and lazy
loading buy nothing against that, and the explicit SQL keeps what actually hits
the database visible.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    insert,
    select,
)
from sqlalchemy.engine import Engine

metadata = MetaData()


capture_sessions = Table(
    "capture_sessions",
    metadata,
    Column("capture_session_id", String(128), primary_key=True),
    Column("package_id", String(128), nullable=False, index=True),
    Column("client_platform", String(32)),
    Column("client_app_version", String(64)),
    Column("client_device_model", String(128)),
    # The declared context, stored whole. Applicability flags are the
    # inspector's declaration and belong together as one record rather than
    # spread across columns that could be updated independently.
    Column("context", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


artifacts = Table(
    "artifacts",
    metadata,
    Column("artifact_id", String(128), primary_key=True),
    Column(
        "capture_session_id",
        String(128),
        ForeignKey("capture_sessions.capture_session_id"),
        nullable=False,
        index=True,
    ),
    Column("package_id", String(128), nullable=False, index=True),
    Column("sha256", String(64), nullable=False),
    Column("uri", Text, nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("width_px", Integer),
    Column("height_px", Integer),
    Column("sequence_index", Integer),
    Column("surface", String(32)),
    Column("surface_label", String(128)),
    Column("orientation", Integer, default=1),
    Column("focal_length_mm", Float, nullable=True),
    Column("captured_at", DateTime(timezone=True)),
    Column("received_at", DateTime(timezone=True), nullable=False),
    # The per-artifact bundle from the device, kept whole. It is richer than
    # the contract and §3 keeps it on this side of the boundary.
    Column("client_metadata", JSON),
)

Index("ix_artifacts_session_sequence", artifacts.c.capture_session_id,
      artifacts.c.sequence_index)


extraction_runs = Table(
    "extraction_runs",
    metadata,
    Column("extraction_run_id", String(128), primary_key=True),
    Column("capture_session_id", String(128), nullable=False, index=True),
    Column("package_id", String(128), nullable=False, index=True),
    Column("status", String(32), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Column("duration_ms", Integer),
    # Every version that shaped the result. §18: a run is only reproducible if
    # you know what produced it.
    Column("pipeline_version", String(64)),
    Column("ocr_engine", String(64)),
    Column("ocr_model_id", String(128)),
    Column("ocr_model_version", String(64)),
    Column("normalizer_version", String(64)),
    Column("attribution_version", String(64)),
    Column("resolver_version", String(64)),
    Column("quality_policy_version", String(64)),
    Column("quality_policy_calibrated", Boolean, default=False),
    Column("snapshot_hash", String(64)),
    Column("snapshot", JSON),
    Column("diagnostics", JSON),
    Column("error", Text),
)


#: The observation trail: regions, OCR results and the candidates that lost.
#:
#: Stored as one document per run rather than shredded into tables. It is
#: written once, read whole when someone is investigating a fact, and never
#: queried field by field — normalising it would buy nothing and would make
#: reassembling a run's evidence a join across four tables.
observation_records = Table(
    "observation_records",
    metadata,
    Column("extraction_run_id", String(128), primary_key=True),
    Column("package_id", String(128), nullable=False, index=True),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


quarantined_runs = Table(
    "quarantined_runs",
    metadata,
    Column("extraction_run_id", String(128), primary_key=True),
    Column("package_id", String(128), nullable=False, index=True),
    Column("reason", String(64), nullable=False),
    Column("problems", JSON, nullable=False),
    Column("quarantined_at", DateTime(timezone=True), nullable=False),
    # Set when an engineer has looked at it. Never set by the pipeline —
    # §7 wants the inconsistency exposed for diagnosis, and a run that
    # cleared itself would defeat that.
    Column("reviewed_at", DateTime(timezone=True)),
    Column("review_note", Text),
)


compliance_submissions = Table(
    "compliance_submissions",
    metadata,
    Column("submission_id", String(128), primary_key=True),
    Column("extraction_run_id", String(128), nullable=False, index=True),
    Column("package_id", String(128), nullable=False, index=True),
    Column("snapshot_hash", String(64), nullable=False),
    Column("submitted_at", DateTime(timezone=True), nullable=False),
    Column("response_status", Integer),
    Column("response_body", Text),
)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def build_engine(database_url: str) -> Engine:
    """Create the engine and ensure the schema exists.

    ``create_all`` is fine for this stage and will not stay fine: it cannot
    express a column type change or a backfill. Move to Alembic before the
    first schema change lands on a database holding real evidence, because at
    that point dropping and recreating stops being an option.
    """
    engine = create_engine(database_url, future=True)
    metadata.create_all(engine)
    return engine


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def record_capture_session(
    engine: Engine,
    *,
    capture_session_id: str,
    package_id: str,
    client: dict[str, Any],
    context: dict[str, Any],
) -> None:
    """Insert a capture session, ignoring a repeat of the same one.

    The upload queue retries after a response the device never received, so a
    duplicate create is the ordinary case. It is not an error and must not
    overwrite: the context recorded when the session opened is the declaration
    the artifacts were captured under.
    """
    with engine.begin() as connection:
        existing = connection.execute(
            select(capture_sessions.c.capture_session_id).where(
                capture_sessions.c.capture_session_id == capture_session_id
            )
        ).first()
        if existing is not None:
            return

        connection.execute(
            insert(capture_sessions).values(
                capture_session_id=capture_session_id,
                package_id=package_id,
                client_platform=client.get("platform"),
                client_app_version=client.get("app_version"),
                client_device_model=client.get("device_model"),
                context=context,
                created_at=_now(),
            )
        )


def record_artifact(
    engine: Engine,
    *,
    artifact_id: str,
    capture_session_id: str,
    package_id: str,
    sha256: str,
    uri: str,
    size_bytes: int,
    client_metadata: dict[str, Any],
    width_px: int | None = None,
    height_px: int | None = None,
    sequence_index: int | None = None,
    surface: str | None = None,
    surface_label: str | None = None,
    orientation: int = 1,
    focal_length_mm: float | None = None,
    captured_at: datetime | None = None,
) -> None:
    """Insert an artifact row, ignoring an idempotent repeat."""
    with engine.begin() as connection:
        existing = connection.execute(
            select(artifacts.c.artifact_id, artifacts.c.sha256).where(
                artifacts.c.artifact_id == artifact_id
            )
        ).first()
        if existing is not None:
            if existing.sha256 != sha256:
                raise ValueError(
                    f"artifact {artifact_id} is already recorded with a "
                    "different hash; artifacts are immutable"
                )
            return

        connection.execute(
            insert(artifacts).values(
                artifact_id=artifact_id,
                capture_session_id=capture_session_id,
                package_id=package_id,
                sha256=sha256,
                uri=uri,
                size_bytes=size_bytes,
                width_px=width_px,
                height_px=height_px,
                sequence_index=sequence_index,
                surface=surface,
                surface_label=surface_label,
                orientation=orientation,
                focal_length_mm=focal_length_mm,
                captured_at=captured_at,
                received_at=_now(),
                client_metadata=client_metadata,
            )
        )


def record_run(engine: Engine, result: Any, capture_session_id: str) -> None:
    """Persist a finished run: metadata, observations and outcome.

    One transaction. A run whose snapshot was recorded but whose observation
    trail was not would be a fact set nobody could audit, which §18 treats as
    equivalent to having no provenance at all.
    """
    diagnostics = result.diagnostics or {}
    snapshot_json = result.snapshot.to_json() if result.snapshot else None
    snapshot_hash = result.snapshot.content_hash() if result.snapshot else None

    if result.quarantine is not None:
        status = "quarantined"
    elif result.snapshot is not None:
        status = "completed"
    else:
        status = "failed"

    with engine.begin() as connection:
        connection.execute(
            insert(extraction_runs).values(
                extraction_run_id=result.extraction_run_id,
                capture_session_id=capture_session_id,
                package_id=result.store.package_id,
                status=status,
                started_at=_now(),
                finished_at=_now(),
                duration_ms=diagnostics.get("duration_ms"),
                pipeline_version=diagnostics.get("pipeline_version"),
                ocr_engine=diagnostics.get("ocr_engine"),
                ocr_model_id=diagnostics.get("ocr_model_id"),
                ocr_model_version=diagnostics.get("ocr_model_version"),
                normalizer_version=diagnostics.get("normalizer_version"),
                attribution_version=diagnostics.get("attribution_version"),
                resolver_version=diagnostics.get("resolver_version"),
                quality_policy_version=(
                    diagnostics.get("quality_policy", {}) or {}
                ).get("version"),
                quality_policy_calibrated=(
                    diagnostics.get("quality_policy", {}) or {}
                ).get("calibrated", False),
                snapshot_hash=snapshot_hash,
                snapshot=snapshot_json,
                diagnostics=diagnostics,
            )
        )

        connection.execute(
            insert(observation_records).values(
                extraction_run_id=result.extraction_run_id,
                package_id=result.store.package_id,
                payload=result.store.to_json(),
                created_at=_now(),
            )
        )

        if result.quarantine is not None:
            quarantine = result.quarantine
            connection.execute(
                insert(quarantined_runs).values(
                    extraction_run_id=quarantine.extraction_run_id,
                    package_id=quarantine.package_id,
                    reason=quarantine.reason,
                    problems=list(quarantine.problems),
                    quarantined_at=quarantine.quarantined_at,
                )
            )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_run(engine: Engine, extraction_run_id: str) -> dict[str, Any] | None:
    with engine.connect() as connection:
        row = connection.execute(
            select(extraction_runs).where(
                extraction_runs.c.extraction_run_id == extraction_run_id
            )
        ).mappings().first()
        return dict(row) if row else None


def get_observations(
    engine: Engine, extraction_run_id: str
) -> dict[str, Any] | None:
    with engine.connect() as connection:
        row = connection.execute(
            select(observation_records.c.payload).where(
                observation_records.c.extraction_run_id == extraction_run_id
            )
        ).first()
        if row is None:
            return None
        payload = row[0]
        return json.loads(payload) if isinstance(payload, str) else payload


def get_session(
    engine: Engine, capture_session_id: str
) -> dict[str, Any] | None:
    with engine.connect() as connection:
        row = connection.execute(
            select(capture_sessions).where(
                capture_sessions.c.capture_session_id == capture_session_id
            )
        ).mappings().first()
        return dict(row) if row else None


def get_session_artifacts(
    engine: Engine, capture_session_id: str
) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(artifacts)
            .where(artifacts.c.capture_session_id == capture_session_id)
            .order_by(artifacts.c.sequence_index)
        ).mappings().all()
        return [dict(r) for r in rows]


def list_quarantined(engine: Engine, limit: int = 100) -> list[dict[str, Any]]:
    """Unreviewed quarantines, oldest first.

    Oldest first because these are a work queue for an engineer, and the one
    that has been sitting longest is the one most likely to be blocking a
    package nobody has noticed is stuck.
    """
    with engine.connect() as connection:
        rows = connection.execute(
            select(quarantined_runs)
            .where(quarantined_runs.c.reviewed_at.is_(None))
            .order_by(quarantined_runs.c.quarantined_at)
            .limit(limit)
        ).mappings().all()
        return [dict(r) for r in rows]


def runs_for_package(
    engine: Engine, package_id: str
) -> Iterable[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(extraction_runs)
            .where(extraction_runs.c.package_id == package_id)
            .order_by(extraction_runs.c.started_at.desc())
        ).mappings().all()
        return [dict(r) for r in rows]
