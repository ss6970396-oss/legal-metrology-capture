"""Request and response models for the extraction API.

Pydantic does the shape validation so the route handlers deal in typed objects
rather than dictionaries. Two things are validated harder than pydantic's
defaults would:

* **Applicability flags have no defaults.** ``ContextIn``'s four fields are
  required. A missing ``is_imported`` is a 422, not a False. §2's rule that an
  applicability flag must be declared rather than defaulted only holds if the
  boundary refuses to invent one.

* **Identifiers are checked for shape.** They become object-store keys and
  database keys, and a path separator in an artifact id would let an upload
  write outside its prefix.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Identifiers the device mints: a prefix and a UUID, or a bare UUID.
#: Deliberately strict — these end up in filesystem paths and object keys.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")


def _validate_id(value: str, field_name: str) -> str:
    if not _ID_PATTERN.match(value):
        raise ValueError(
            f"{field_name} must be 1-128 chars of letters, digits, dot, dash "
            f"or underscore; got {value!r}"
        )
    return value


class JurisdictionIn(BaseModel):
    country: str = Field(default="IN", max_length=2, min_length=2)
    state: str | None = Field(default=None, max_length=64)


class ContextIn(BaseModel):
    """The declared commercial context.

    Every flag is required. There is no sensible default for any of them: each
    switches a body of rules on or off downstream, and a default would be this
    service asserting something about a package it has never seen.
    """

    model_config = ConfigDict(extra="forbid")

    jurisdiction: JurisdictionIn = Field(default_factory=JurisdictionIn)
    sale_channel: str = Field(min_length=1, max_length=32)
    is_imported: bool
    is_for_retail: bool
    is_ecommerce_listing: bool


class ClientIn(BaseModel):
    platform: Literal["android", "ios", "other"] = "other"
    app_version: str = Field(default="unknown", max_length=64)
    device_model: str = Field(default="unknown", max_length=128)


class CaptureSessionIn(BaseModel):
    """``POST /capture-sessions``."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = Field(default="lm-capture-input/1.1", max_length=64)
    capture_session_id: str
    package_id: str
    client: ClientIn = Field(default_factory=ClientIn)
    context: ContextIn
    started_at: datetime | None = None

    @field_validator("capture_session_id")
    @classmethod
    def _check_session_id(cls, v: str) -> str:
        return _validate_id(v, "capture_session_id")

    @field_validator("package_id")
    @classmethod
    def _check_package_id(cls, v: str) -> str:
        return _validate_id(v, "package_id")


class CaptureSessionOut(BaseModel):
    capture_session_id: str
    package_id: str
    created: bool
    #: True when this call matched an existing session. The device retries, so
    #: this is expected rather than exceptional.
    already_existed: bool = False


class ArtifactOut(BaseModel):
    artifact_id: str
    sha256: str
    uri: str
    size_bytes: int
    already_existed: bool = False


class CaptureMetadataIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    width_px: int = Field(default=0, ge=0)
    height_px: int = Field(default=0, ge=0)
    focal_length_mm: float | None = None
    orientation: int = Field(default=1, ge=1, le=8)


class ImageEntryIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    artifact_id: str
    sha256: str = Field(min_length=64, max_length=64)
    uri: str | None = None
    sequence_index: int = Field(default=1, ge=1)
    captured_at: datetime | None = None
    capture_metadata: CaptureMetadataIn = Field(
        default_factory=CaptureMetadataIn
    )
    surface: str | None = None
    surface_label: str | None = None

    @field_validator("artifact_id")
    @classmethod
    def _check_artifact_id(cls, v: str) -> str:
        return _validate_id(v, "artifact_id")

    @field_validator("sha256")
    @classmethod
    def _check_sha(cls, v: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", v):
            raise ValueError("sha256 must be 64 hex characters")
        return v.lower()


class CoverageIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    required_surfaces: int = 0
    resolved_surfaces: int = 0
    captured_surfaces: int = 0
    complete: bool = False
    unresolved_surfaces: list[str] = Field(default_factory=list)
    skipped_surfaces: list[dict[str, Any]] = Field(default_factory=list)
    surfaces_needing_manual_review: list[str] = Field(default_factory=list)


class ExtractionJobIn(BaseModel):
    """``POST /extraction-jobs``."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = Field(default="lm-capture-input/1.1", max_length=64)
    capture_session_id: str
    package_id: str
    client: ClientIn = Field(default_factory=ClientIn)
    context: ContextIn
    images: list[ImageEntryIn] = Field(min_length=1)
    coverage: CoverageIn = Field(default_factory=CoverageIn)

    @field_validator("capture_session_id")
    @classmethod
    def _check_session_id(cls, v: str) -> str:
        return _validate_id(v, "capture_session_id")

    @field_validator("package_id")
    @classmethod
    def _check_package_id(cls, v: str) -> str:
        return _validate_id(v, "package_id")


class ExtractionJobOut(BaseModel):
    extraction_run_id: str
    capture_session_id: str
    package_id: str
    #: Pipeline state, never a compliance verdict. §20 is explicit that the
    #: extraction service must not expose a legal PASS/FAIL as its own result.
    status: Literal[
        "EXTRACTION_READY",
        "NEEDS_RECAPTURE",
        "INCOMPLETE_EVIDENCE",
        "QUARANTINED",
        "EXTRACTION_ERROR",
    ]
    snapshot_available: bool
    message: str = ""


class SnapshotOut(BaseModel):
    """``GET /extraction-jobs/{id}/snapshot``.

    Returns the snapshot only when the run was released. A quarantined run
    yields a 409 with its problems, not a partial snapshot — §7 forbids
    releasing something the validator refused.
    """

    extraction_run_id: str
    package_id: str
    snapshot_hash: str
    snapshot: dict[str, Any]


class QuarantineOut(BaseModel):
    extraction_run_id: str
    package_id: str
    reason: str
    problems: list[str]
    quarantined_at: datetime
    snapshot_released: Literal[False] = False


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    version: str
    ocr_engines_available: list[str]
    quality_policy_calibrated: bool
    #: Set when something is configured in a way that would mislead. The
    #: uncalibrated quality policy is the one that matters today.
    warnings: list[str] = Field(default_factory=list)
