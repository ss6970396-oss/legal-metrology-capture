"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from . import telemetry
from .api.routes import router
from .config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    telemetry.setup(settings.service_name, settings.otlp_endpoint)
    telemetry.instrument_fastapi(app)

    if not settings.quality_policy_calibrated:
        # Repeated at startup as well as per-run. §14's rule is easy to forget
        # once the service is running and quietly producing verdicts.
        log.warning(
            "quality thresholds are UNCALIBRATED. RETAKE verdicts are "
            "advisory and must not be used as acceptance criteria until they "
            "are fitted against a project benchmark (contract v1.1 §14)."
        )
    if settings.submit_to_compliance_engine:
        log.warning(
            "snapshot forwarding to the compliance engine is ENABLED (%s)",
            settings.compliance_engine_url or "no URL configured",
        )

    log.info(
        "extraction service ready: env=%s ocr=%s languages=%s",
        settings.environment,
        settings.ocr_engine,
        ",".join(settings.ocr_languages),
    )
    yield


app = FastAPI(
    title="Legal Metrology Extraction Service",
    version="1.0.0",
    summary=(
        "Turns package photographs into a provenance-bearing "
        "PackageFactSnapshot v1.1."
    ),
    description=(
        "Team 1's upstream evidence pipeline. It owns capture ingestion, image "
        "processing, OCR, observation construction, normalization, fact "
        "reconciliation and snapshot generation.\n\n"
        "It does **not** own legal applicability, compliance rules, verdicts or "
        "decision traces — those belong to the compliance engine. No endpoint "
        "here returns a PASS/FAIL result, and the statuses it does return "
        "(EXTRACTION_READY, NEEDS_RECAPTURE, INCOMPLETE_EVIDENCE, QUARANTINED, "
        "EXTRACTION_ERROR) describe this pipeline's state, not a package's "
        "compliance."
    ),
    lifespan=lifespan,
)

app.include_router(router, prefix="/v1")
