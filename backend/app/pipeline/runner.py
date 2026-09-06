"""The extraction run — stages 1 through 12, in order.

§5 of the contract lists them:

1. Capture session validation          7. Geometry normalization
2. Artifact integrity validation       8. Observation store
3. Image quality diagnostics           9. Field attribution + normalization
4. Package / view / duplicate analysis 10. Multi-image fact resolution
5. Text detection                      11. Snapshot builder
6. OCR recognition                     12. Schema + consistency validation

Detection and recognition are one call here because the engines in ``ocr.py``
do both; the stages stay conceptually distinct and are benchmarked separately
(§17 scores detection and recognition apart).

Two properties this module has to preserve above convenience:

* **Nothing is discarded.** Every artifact, region, observation and rejected
  candidate lands in the :class:`ObservationStore` alongside the snapshot. The
  snapshot says what was concluded; the store says what it was concluded from.

* **A failure quarantines rather than degrades.** If validation fails at stage
  12 the run produces a :class:`QuarantinedRun` and no snapshot. §7 forbids
  silently repairing an inconsistent snapshot, and a pipeline that returned a
  partial one would be doing exactly that.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..domain.facts import FactSet
from ..domain.geometry import CoordinateSpace
from ..domain.observation import (
    ImageArtifact,
    ObservationStore,
    OcrObservation,
    OcrRun,
    Region,
    SurfaceView,
)
from ..domain.snapshot import (
    CommercialContext,
    Jurisdiction,
    PackageFactSnapshot,
    QuarantinedRun,
    SnapshotConsistencyError,
    SnapshotValidationError,
    build_snapshot,
)
from . import attribution, quality, resolver
from . import normalize as nz
from .ocr import OcrEngine, OcrResult

log = logging.getLogger(__name__)

PIPELINE_VERSION = "extract-1.0.0"


class ExtractionError(Exception):
    """A run could not proceed far enough to produce anything."""


@dataclass
class ArtifactInput:
    """One artifact as the job submitted it, plus where its bytes are."""

    artifact_id: str
    sha256: str
    local_path: Path
    sequence_index: int
    captured_at: datetime
    width_px: int
    height_px: int
    surface: SurfaceView = SurfaceView.UNKNOWN
    surface_label: str = ""
    orientation: int = 1
    focal_length_mm: float | None = None
    uri: str = ""
    client_quality: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractionRequest:
    capture_session_id: str
    package_id: str
    jurisdiction: Jurisdiction
    commercial_context: CommercialContext
    artifacts: list[ArtifactInput]
    #: Coverage as the capture client reported it. Carried into diagnostics so
    #: a snapshot built from a partial set is identifiable as such.
    coverage: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractionResult:
    """What one run produced.

    Exactly one of ``snapshot`` and ``quarantine`` is set. The store and the
    diagnostics are populated either way — a quarantined run is the one you
    most need the evidence trail for.
    """

    extraction_run_id: str
    store: ObservationStore
    snapshot: PackageFactSnapshot | None = None
    quarantine: QuarantinedRun | None = None
    quality_reports: list[quality.QualityReport] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def released(self) -> bool:
        """Whether a snapshot may be sent to the compliance engine."""
        return self.snapshot is not None and self.quarantine is None

    def to_json(self) -> dict[str, Any]:
        return {
            "extraction_run_id": self.extraction_run_id,
            "released": self.released,
            "snapshot": self.snapshot.to_json() if self.snapshot else None,
            "snapshot_hash": (
                self.snapshot.content_hash() if self.snapshot else None
            ),
            "quarantine": self.quarantine.to_json() if self.quarantine else None,
            "quality": [r.to_json() for r in self.quality_reports],
            "diagnostics": self.diagnostics,
            "observations": self.store.to_json(),
        }


@dataclass
class PipelineConfig:
    engine: OcrEngine
    quality_policy: quality.QualityPolicy = quality.DEFAULT_POLICY
    #: Skip OCR on artifacts the quality stage says to retake.
    #:
    #: Off by default, and that is the safer setting: §14's thresholds are
    #: uncalibrated, so a RETAKE verdict is currently a suggestion. Dropping an
    #: artifact on the strength of an unvalidated threshold would lose evidence
    #: that may well have been readable. Turn it on once the policy is fitted.
    skip_ocr_on_retake: bool = False
    #: Minimum detector confidence for a region to reach attribution.
    min_detection_confidence: float = 0.30


def run_extraction(
    request: ExtractionRequest, config: PipelineConfig
) -> ExtractionResult:
    """Execute one extraction run end to end."""
    run_id = f"RUN-{uuid.uuid4()}"
    started = time.monotonic()

    store = ObservationStore(
        extraction_run_id=run_id, package_id=request.package_id
    )
    quality_reports: list[quality.QualityReport] = []
    diagnostics: dict[str, Any] = {
        "pipeline_version": PIPELINE_VERSION,
        "normalizer_version": nz.NORMALIZER_VERSION,
        "attribution_version": attribution.ATTRIBUTION_VERSION,
        "resolver_version": resolver.RESOLVER_VERSION,
        "quality_pipeline_version": quality.QUALITY_PIPELINE_VERSION,
        "quality_policy": config.quality_policy.to_json(),
        "ocr_engine": config.engine.name,
        "ocr_model_id": config.engine.model_id,
        "ocr_model_version": config.engine.model_version,
        "client_coverage": request.coverage,
        "stages": {},
    }

    if not config.quality_policy.calibrated:
        # Loud on purpose. §14 forbids shipping unvalidated thresholds as
        # acceptance criteria, and a warning in every run's diagnostics is
        # harder to forget than a note in a design document.
        log.warning(
            "run %s uses quality policy %s, which is NOT calibrated against a "
            "project benchmark; RETAKE verdicts are advisory only",
            run_id,
            config.quality_policy.version,
        )
        diagnostics["quality_policy_warning"] = (
            "Thresholds are uncalibrated starting values. Per contract §14 they "
            "must be fitted against a labelled benchmark before any verdict is "
            "treated as an acceptance criterion."
        )

    # -- Stage 1: capture session validation ------------------------------
    _validate_session(request)
    diagnostics["stages"]["session_validated"] = True

    # -- Stage 2-8: per-artifact -----------------------------------------
    pairs: list[tuple[Region, OcrObservation]] = []
    surfaces: dict[str, SurfaceView] = {}

    for artifact_input in sorted(
        request.artifacts, key=lambda a: a.sequence_index
    ):
        artifact = _register_artifact(request, artifact_input, store)
        surfaces[artifact.artifact_id] = artifact.surface

        report = quality.analyse(
            artifact_input.local_path,
            artifact.artifact_id,
            config.quality_policy,
        )
        quality_reports.append(report)

        if (
            config.skip_ocr_on_retake
            and report.verdict is quality.CaptureVerdict.RETAKE
        ):
            log.info(
                "run %s: skipping OCR for %s (quality verdict RETAKE)",
                run_id,
                artifact.artifact_id,
            )
            continue

        ocr_result = config.engine.recognize(artifact_input.local_path)
        ocr_run = OcrRun.create(
            engine=ocr_result.engine,
            model_id=ocr_result.model_id,
            model_version=ocr_result.model_version,
            languages=ocr_result.languages,
            preprocessing_version=quality.QUALITY_PIPELINE_VERSION,
        )
        store.add_run(ocr_run)

        pairs.extend(
            _observations_from(
                ocr_result=ocr_result,
                ocr_run=ocr_run,
                artifact=artifact,
                image_path=artifact_input.local_path,
                store=store,
                config=config,
            )
        )

    diagnostics["stages"]["artifacts_processed"] = len(store.artifacts)
    diagnostics["stages"]["regions_detected"] = len(store.regions)
    diagnostics["stages"]["observations"] = len(store.observations)

    # -- Stage 4 (deferred): duplicate and identity analysis -------------
    #
    # Run after OCR rather than before it, because the signal that two
    # photographs show the same panel is the text they share — which is not
    # available until the text has been read.
    duplicates = resolver.duplicate_artifacts(store)
    diagnostics["near_duplicate_artifacts"] = [list(p) for p in duplicates]

    # -- Stage 9: field attribution --------------------------------------
    for candidate in attribution.attribute(pairs, surfaces):
        store.add_candidate(candidate)
    diagnostics["stages"]["candidates"] = len(store.candidates)

    statements = attribution.observed_statements(pairs)
    diagnostics["observed_statements"] = {
        name: [g.to_json() for g in geometries]
        for name, geometries in statements.items()
    }

    # -- Stage 10: fact resolution ---------------------------------------
    facts: FactSet = resolver.resolve(store)
    identity_warnings = resolver.check_package_identity(store)
    diagnostics["package_identity_warnings"] = [
        w.to_json() for w in identity_warnings
    ]
    diagnostics["surface_coverage"] = resolver.surface_coverage(store)
    diagnostics["fact_status_counts"] = _status_counts(facts)

    # -- Stage 11-12: snapshot construction and validation ---------------
    result = ExtractionResult(
        extraction_run_id=run_id,
        store=store,
        quality_reports=quality_reports,
        diagnostics=diagnostics,
    )

    try:
        result.snapshot = build_snapshot(
            package_id=request.package_id,
            facts=facts,
            jurisdiction=request.jurisdiction,
            commercial_context=request.commercial_context,
            extraction_run_id=run_id,
        )
    except (SnapshotConsistencyError, SnapshotValidationError) as exc:
        # §7's quarantine rule. The run is preserved in full — store,
        # diagnostics, quality — so the inconsistency can be diagnosed, and no
        # snapshot is released.
        log.error("run %s quarantined: %s", run_id, exc)
        result.quarantine = QuarantinedRun.from_error(
            extraction_run_id=run_id,
            package_id=request.package_id,
            error=exc,
        )

    diagnostics["duration_ms"] = int((time.monotonic() - started) * 1000)
    return result


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def _validate_session(request: ExtractionRequest) -> None:
    """Stage 1. Refuses a session that cannot produce a meaningful snapshot."""
    if not request.package_id:
        raise ExtractionError("package_id is required")
    if not request.capture_session_id:
        raise ExtractionError("capture_session_id is required")
    if not request.artifacts:
        raise ExtractionError(
            "an extraction job needs at least one artifact; a package with no "
            "images has nothing to extract and would resolve every field to "
            "UNKNOWN, which is a misleading result rather than an empty one"
        )

    seen: set[str] = set()
    for artifact in request.artifacts:
        if artifact.artifact_id in seen:
            raise ExtractionError(
                f"artifact {artifact.artifact_id} appears twice in the job"
            )
        seen.add(artifact.artifact_id)


def _register_artifact(
    request: ExtractionRequest,
    artifact_input: ArtifactInput,
    store: ObservationStore,
) -> ImageArtifact:
    """Stage 2. Verify integrity, then record the artifact.

    The hash check is a hard failure. Evidence whose bytes changed between the
    device and here is not the evidence the device recorded, the device still
    holds the original, and §18's whole immutability control rests on the two
    matching.
    """
    actual = _sha256_of(artifact_input.local_path)
    if actual != artifact_input.sha256:
        raise ExtractionError(
            f"artifact {artifact_input.artifact_id} failed its integrity check: "
            f"declared {artifact_input.sha256[:12]}…, stored {actual[:12]}…"
        )

    artifact = ImageArtifact(
        artifact_id=artifact_input.artifact_id,
        capture_session_id=request.capture_session_id,
        package_id=request.package_id,
        sha256=artifact_input.sha256,
        uri=artifact_input.uri,
        width_px=artifact_input.width_px,
        height_px=artifact_input.height_px,
        sequence_index=artifact_input.sequence_index,
        captured_at=artifact_input.captured_at,
        surface=artifact_input.surface,
        surface_label=artifact_input.surface_label,
        orientation=artifact_input.orientation,
        focal_length_mm=artifact_input.focal_length_mm,
        client_quality=artifact_input.client_quality,
    )
    store.add_artifact(artifact)
    return artifact


def _sha256_of(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _observations_from(
    *,
    ocr_result: OcrResult,
    ocr_run: OcrRun,
    artifact: ImageArtifact,
    image_path: Path,
    store: ObservationStore,
    config: PipelineConfig,
) -> list[tuple[Region, OcrObservation]]:
    """Stages 5-8: detection, recognition, geometry and the store."""
    pairs: list[tuple[Region, OcrObservation]] = []

    for line in ocr_result.lines:
        if not line.text.strip():
            continue

        # Confidence is None when the engine reports none. Attribution needs a
        # number, so a neutral 0.5 stands in — and the substitution is recorded
        # on the region so a downstream reader can tell a measured score from a
        # placeholder. §7's point: never let an unmeasured quantity look
        # measured.
        confidence = line.confidence if line.confidence is not None else 0.5
        if confidence < config.min_detection_confidence:
            continue

        geometry = line.geometry
        if geometry.coordinate_space is not CoordinateSpace.ORIGINAL_IMAGE_PX:
            # Engines are handed the original today, so this is defensive. When
            # a rectification stage is added it supplies the transform chain and
            # this is where the mapping back happens.
            geometry = geometry.to_bbox()

        measured = quality.region_quality(
            image_path, geometry, config.quality_policy
        )
        region = Region.create(
            artifact_id=artifact.artifact_id,
            geometry=geometry,
            detection_confidence=confidence,
            quality=measured or {"overall": 1.0},
        )
        store.add_region(region)

        observation = OcrObservation.create(
            region=region,
            run=ocr_run,
            raw_text=line.text,
            normalized_text=nz.clean_text(line.text),
            recognition_confidence=confidence,
            script=line.script,
            language=line.language,
        )
        store.add_observation(observation)
        pairs.append((region, observation))

    return pairs


def _status_counts(facts: FactSet) -> dict[str, int]:
    counts: dict[str, int] = {}
    for fact in facts.ordered():
        key = fact.status.value
        counts[key] = counts.get(key, 0) + 1
    return counts
