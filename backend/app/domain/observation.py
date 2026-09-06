"""The internal observation model.

§6 and §11 of the architecture ask for a richer internal model than the
contract exposes, and for one specific reason: the snapshot carries one
provenance block per fact, so anything the pipeline saw but did not promote
would be lost if the snapshot were the only record. It is not. This is.

The lineage the architecture requires is::

    ImageArtifact -> Region -> OCRObservation -> Candidate -> ResolvedFact
                                                           -> facts[] -> projection

Each arrow is a stage that can be wrong on its own, and each object here keeps
enough to answer §18's question — "which pixels caused this fact, which OCR
run produced the observation, which normalization rule transformed it, which
resolver decision made it authoritative" — without re-running anything.

Nothing in this module decides anything. Observations preserve what a model
saw; resolved facts (``facts.py``) represent the deterministic reconciliation
of those observations. Keeping the two apart is what makes a conflict
inspectable instead of hidden behind whichever OCR result happened to run last.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Sequence

from .geometry import Geometry, TransformChain


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


class SurfaceView(str, Enum):
    """Which face of the package an artifact shows.

    Supplied by the capture client, which asked the inspector to photograph a
    named surface, rather than inferred here. A guided capture already knows
    the answer and a classifier guessing at it would be a worse source.
    """

    FRONT = "front"
    BACK = "back"
    SIDE_1 = "side_1"
    SIDE_2 = "side_2"
    EXTRA = "extra"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: str | None) -> "SurfaceView":
        if not raw:
            return cls.UNKNOWN
        normalised = raw.strip().lower()
        for member in cls:
            if member.value == normalised:
                return member
        if normalised.startswith("extra"):
            return cls.EXTRA
        return cls.UNKNOWN

    @property
    def is_principal_display_panel(self) -> bool:
        """Whether this face is the PDP candidate.

        Relevant because several declarations are required on the principal
        display panel specifically, so an MRP found on the front is weak
        evidence *for* being the real one where an MRP on a side panel is not.
        Used only as a resolver prior, never as a legal determination.
        """
        return self is SurfaceView.FRONT


@dataclass(frozen=True)
class ImageArtifact:
    """One immutable photograph, as the backend received it.

    The hash is checked on receipt against the one the device computed. A
    mismatch is a hard failure rather than a warning: evidence whose bytes
    changed in transit is not evidence, and the device still holds the
    original, so rejecting costs nothing but a re-upload.
    """

    artifact_id: str
    capture_session_id: str
    package_id: str
    sha256: str
    uri: str
    width_px: int
    height_px: int
    sequence_index: int
    captured_at: datetime
    surface: SurfaceView = SurfaceView.UNKNOWN
    surface_label: str = ""
    orientation: int = 1
    focal_length_mm: float | None = None
    #: The capture client's own quality verdicts, carried for diagnosis. Not
    #: authoritative — §11 makes the backend's assessment the deciding one.
    client_quality: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "capture_session_id": self.capture_session_id,
            "package_id": self.package_id,
            "sha256": self.sha256,
            "uri": self.uri,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "sequence_index": self.sequence_index,
            "captured_at": self.captured_at.isoformat(),
            "surface": self.surface.value,
            "surface_label": self.surface_label,
            "orientation": self.orientation,
            "focal_length_mm": self.focal_length_mm,
        }


@dataclass(frozen=True)
class Region:
    """A localized piece of visual evidence within one artifact.

    ``geometry`` is always in the original artifact's coordinate space, and
    ``transform`` records how to get from there to whatever image the
    recognizer actually saw. Storing the crop-space coordinates as the primary
    would make provenance depend on a derived image that nothing keeps.
    """

    region_id: str
    artifact_id: str
    geometry: Geometry
    #: Detector score for "there is text here", distinct from how well it was
    #: then read.
    detection_confidence: float
    transform: TransformChain = TransformChain()
    #: Local image quality measured on this region specifically. §7 prefers
    #: region-level quality over a global score, because a sharp package with
    #: one blurred declaration panel passes every global check.
    quality: dict[str, float] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        artifact_id: str,
        geometry: Geometry,
        detection_confidence: float,
        transform: TransformChain | None = None,
        quality: dict[str, float] | None = None,
    ) -> "Region":
        return cls(
            region_id=_new_id("REG"),
            artifact_id=artifact_id,
            geometry=geometry,
            detection_confidence=detection_confidence,
            transform=transform or TransformChain(),
            quality=quality or {},
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "artifact_id": self.artifact_id,
            "geometry": self.geometry.to_json(),
            "detection_confidence": round(self.detection_confidence, 4),
            "transform": self.transform.to_json(),
            "quality": {k: round(v, 4) for k, v in self.quality.items()},
        }


@dataclass(frozen=True)
class OcrRun:
    """Identifies one execution of one recognizer over one input.

    §18 requires a run to be reproducible, which means recording not just
    which model but which configuration: a PP-OCRv5 run with a different
    language set is a different run and may legitimately produce a different
    transcript.
    """

    ocr_run_id: str
    engine: str
    model_id: str
    model_version: str
    languages: tuple[str, ...]
    preprocessing_version: str
    started_at: datetime

    @classmethod
    def create(
        cls,
        engine: str,
        model_id: str,
        model_version: str,
        languages: Sequence[str],
        preprocessing_version: str,
    ) -> "OcrRun":
        return cls(
            ocr_run_id=_new_id("OCR"),
            engine=engine,
            model_id=model_id,
            model_version=model_version,
            languages=tuple(languages),
            preprocessing_version=preprocessing_version,
            started_at=datetime.now(timezone.utc),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "ocr_run_id": self.ocr_run_id,
            "engine": self.engine,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "languages": list(self.languages),
            "preprocessing_version": self.preprocessing_version,
            "started_at": self.started_at.isoformat(),
        }


@dataclass(frozen=True)
class OcrObservation:
    """What one recognizer read in one region.

    Both ``raw_text`` and ``normalized_text`` are kept. The raw string is the
    evidence — it is what the model actually emitted, and a normalization bug
    found six months from now can only be diagnosed against it. The normalized
    form is a convenience for matching, and it is derived, so it is allowed to
    change when the normalizer does.
    """

    observation_id: str
    region_id: str
    artifact_id: str
    ocr_run_id: str
    raw_text: str
    normalized_text: str
    recognition_confidence: float
    script: str = ""
    language: str = ""

    @classmethod
    def create(
        cls,
        region: Region,
        run: OcrRun,
        raw_text: str,
        normalized_text: str,
        recognition_confidence: float,
        script: str = "",
        language: str = "",
    ) -> "OcrObservation":
        return cls(
            observation_id=_new_id("OBS"),
            region_id=region.region_id,
            artifact_id=region.artifact_id,
            ocr_run_id=run.ocr_run_id,
            raw_text=raw_text,
            normalized_text=normalized_text,
            recognition_confidence=recognition_confidence,
            script=script,
            language=language,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "region_id": self.region_id,
            "artifact_id": self.artifact_id,
            "ocr_run_id": self.ocr_run_id,
            "raw_text": self.raw_text,
            "normalized_text": self.normalized_text,
            "recognition_confidence": round(self.recognition_confidence, 4),
            "script": self.script,
            "language": self.language,
        }


@dataclass(frozen=True)
class FieldCandidate:
    """A proposal that one observation means one contract field.

    The stage between reading text and asserting a fact, and the one most
    worth keeping separately. "MRP Rs. 120" being read correctly and that
    string meaning ``package.mrp`` are two different claims that fail in
    different ways — a price printed elsewhere on the package is read
    perfectly and attributed wrongly — so they carry separate confidences and
    are evaluated separately in the benchmark.

    ``normalized_value`` is the typed value the contract expects: a
    ``{"amount": ..., "currency": ...}`` mapping for money, a
    ``{"value": ..., "unit": ...}`` mapping for quantity, an ISO string for a
    date. ``normalization_rule`` names the rule that produced it so §18's
    "which normalization rule transformed it" is answerable.
    """

    candidate_id: str
    field: str
    observation_id: str
    region_id: str
    artifact_id: str
    ocr_run_id: str
    geometry: Geometry
    raw_text: str
    normalized_value: Any
    attribution_confidence: float
    recognition_confidence: float
    normalization_rule: str
    region_quality: float = 1.0
    surface: SurfaceView = SurfaceView.UNKNOWN
    #: Why the attributor thought this was the field. Engineering diagnostics.
    rationale: str = ""

    @classmethod
    def create(
        cls,
        field_name: str,
        observation: OcrObservation,
        region: Region,
        normalized_value: Any,
        attribution_confidence: float,
        normalization_rule: str,
        surface: SurfaceView = SurfaceView.UNKNOWN,
        region_quality: float = 1.0,
        rationale: str = "",
    ) -> "FieldCandidate":
        return cls(
            candidate_id=_new_id("CAND"),
            field=field_name,
            observation_id=observation.observation_id,
            region_id=region.region_id,
            artifact_id=region.artifact_id,
            ocr_run_id=observation.ocr_run_id,
            geometry=region.geometry,
            raw_text=observation.raw_text,
            normalized_value=normalized_value,
            attribution_confidence=attribution_confidence,
            recognition_confidence=observation.recognition_confidence,
            normalization_rule=normalization_rule,
            region_quality=region_quality,
            surface=surface,
            rationale=rationale,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "field": self.field,
            "observation_id": self.observation_id,
            "region_id": self.region_id,
            "artifact_id": self.artifact_id,
            "ocr_run_id": self.ocr_run_id,
            "geometry": self.geometry.to_json(),
            "raw_text": self.raw_text,
            "normalized_value": self.normalized_value,
            "attribution_confidence": round(self.attribution_confidence, 4),
            "recognition_confidence": round(self.recognition_confidence, 4),
            "normalization_rule": self.normalization_rule,
            "region_quality": round(self.region_quality, 4),
            "surface": self.surface.value,
            "rationale": self.rationale,
        }


@dataclass
class ObservationStore:
    """Everything one extraction run saw, before anything was decided.

    Held in memory for the duration of a run and persisted alongside the
    snapshot. It is the audit record: the snapshot says what was concluded,
    this says what it was concluded from — including the readings that were
    rejected, which is the half that makes a disputed fact reviewable.
    """

    extraction_run_id: str
    package_id: str
    artifacts: dict[str, ImageArtifact] = field(default_factory=dict)
    regions: dict[str, Region] = field(default_factory=dict)
    runs: dict[str, OcrRun] = field(default_factory=dict)
    observations: dict[str, OcrObservation] = field(default_factory=dict)
    candidates: list[FieldCandidate] = field(default_factory=list)

    def add_artifact(self, artifact: ImageArtifact) -> None:
        self.artifacts[artifact.artifact_id] = artifact

    def add_region(self, region: Region) -> None:
        self.regions[region.region_id] = region

    def add_run(self, run: OcrRun) -> None:
        self.runs[run.ocr_run_id] = run

    def add_observation(self, observation: OcrObservation) -> None:
        self.observations[observation.observation_id] = observation

    def add_candidate(self, candidate: FieldCandidate) -> None:
        self.candidates.append(candidate)

    def candidates_for(self, field_name: str) -> list[FieldCandidate]:
        return [c for c in self.candidates if c.field == field_name]

    @property
    def fields_seen(self) -> set[str]:
        return {c.field for c in self.candidates}

    def to_json(self) -> dict[str, Any]:
        return {
            "extraction_run_id": self.extraction_run_id,
            "package_id": self.package_id,
            "artifacts": [a.to_json() for a in self.artifacts.values()],
            "regions": [r.to_json() for r in self.regions.values()],
            "ocr_runs": [r.to_json() for r in self.runs.values()],
            "observations": [o.to_json() for o in self.observations.values()],
            "candidates": [c.to_json() for c in self.candidates],
        }
