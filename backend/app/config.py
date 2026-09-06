"""Runtime configuration.

Everything that varies between a developer laptop, CI and a deployment lives
here and is read from the environment. Two of these settings decide whether the
service behaves correctly rather than merely conveniently, and both default to
the safe answer:

* ``quality_policy_calibrated`` defaults to False. §14 forbids treating
  unvalidated thresholds as acceptance criteria, so the default says out loud
  that they have not been validated.
* ``submit_to_compliance_engine`` defaults to False. A snapshot reaching Team 2
  is a real consequence, and it should take a deliberate act to enable it
  rather than a forgotten default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    # -- service ---------------------------------------------------------
    service_name: str = field(
        default_factory=lambda: os.environ.get(
            "LM_SERVICE_NAME", "lm-extraction"
        )
    )
    environment: str = field(
        default_factory=lambda: os.environ.get("LM_ENVIRONMENT", "development")
    )

    # -- storage ---------------------------------------------------------
    #: Local scratch for artifact bytes while a run processes them.
    work_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("LM_WORK_DIR", "./.lm-work")
        )
    )
    #: S3-compatible endpoint. Empty means "use local filesystem storage",
    #: which is what tests and a laptop want.
    s3_endpoint: str = field(
        default_factory=lambda: os.environ.get("LM_S3_ENDPOINT", "")
    )
    s3_bucket: str = field(
        default_factory=lambda: os.environ.get("LM_S3_BUCKET", "lm-artifacts")
    )
    s3_region: str = field(
        default_factory=lambda: os.environ.get("LM_S3_REGION", "ap-south-1")
    )

    # -- database --------------------------------------------------------
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "LM_DATABASE_URL", "sqlite+pysqlite:///./lm-extraction.db"
        )
    )

    # -- OCR -------------------------------------------------------------
    ocr_engine: str = field(
        default_factory=lambda: os.environ.get("LM_OCR_ENGINE", "paddleocr")
    )
    ocr_languages: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            lang.strip()
            for lang in os.environ.get(
                "LM_OCR_LANGUAGES", "en,devanagari"
            ).split(",")
            if lang.strip()
        )
    )

    # -- quality ---------------------------------------------------------
    #: Whether the configured quality policy has passed §17's calibration.
    #: Leave False until a benchmark says otherwise; the runner warns while
    #: it is False and the warning is the point.
    quality_policy_calibrated: bool = field(
        default_factory=lambda: _env_bool("LM_QUALITY_CALIBRATED", False)
    )
    #: Drop artifacts the quality stage flags for retake. Off until the policy
    #: is calibrated — an unvalidated threshold must not discard evidence.
    skip_ocr_on_retake: bool = field(
        default_factory=lambda: _env_bool("LM_SKIP_OCR_ON_RETAKE", False)
    )

    # -- downstream ------------------------------------------------------
    compliance_engine_url: str = field(
        default_factory=lambda: os.environ.get("LM_COMPLIANCE_ENGINE_URL", "")
    )
    #: Whether validated snapshots are forwarded to the compliance engine.
    #: Off by default: sending is an outward-facing act and should be
    #: switched on deliberately.
    submit_to_compliance_engine: bool = field(
        default_factory=lambda: _env_bool("LM_SUBMIT_TO_COMPLIANCE", False)
    )

    # -- observability ---------------------------------------------------
    otlp_endpoint: str = field(
        default_factory=lambda: os.environ.get("LM_OTLP_ENDPOINT", "")
    )

    # -- limits ----------------------------------------------------------
    max_artifact_bytes: int = field(
        default_factory=lambda: _env_int(
            "LM_MAX_ARTIFACT_BYTES", 40 * 1024 * 1024
        )
    )
    max_artifacts_per_job: int = field(
        default_factory=lambda: _env_int("LM_MAX_ARTIFACTS_PER_JOB", 24)
    )

    def __post_init__(self) -> None:
        self.work_dir = Path(self.work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    @property
    def uses_object_store(self) -> bool:
        return bool(self.s3_endpoint)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Drop the cached settings. For tests that manipulate the environment."""
    global _settings
    _settings = None
