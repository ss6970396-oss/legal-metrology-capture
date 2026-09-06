"""OpenTelemetry wiring.

§18 asks to be able to trace an image through extraction, and §17 puts a
latency target on the end-to-end run. Both need spans that survive across the
stages, so the correlation identifier is the ``extraction_run_id`` and every
span carries it.

Degrades to no-ops when OpenTelemetry is not installed. Observability is not
allowed to be a hard dependency of extracting evidence: a deployment without a
collector should still process packages, and a laptop running the contract
tests should not need an OTLP endpoint.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger(__name__)

_tracer: Any = None
_enabled = False


def setup(service_name: str, otlp_endpoint: str = "") -> bool:
    """Initialise tracing. Returns whether it actually started."""
    global _tracer, _enabled

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.info("OpenTelemetry not installed; tracing disabled")
        return False

    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name})
    )

    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
            )
        except ImportError:
            log.warning(
                "OTLP endpoint %s configured but the exporter is not "
                "installed; spans stay local",
                otlp_endpoint,
            )

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)
    _enabled = True
    log.info("tracing enabled for %s", service_name)
    return True


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    """Open a span, or do nothing when tracing is off.

    Attributes with a ``None`` value are dropped rather than serialised as the
    string "None", which otherwise turns into a genuinely confusing tag on
    every optional field.
    """
    if not _enabled or _tracer is None:
        yield
        return

    clean = {k: v for k, v in attributes.items() if v is not None}
    with _tracer.start_as_current_span(name) as current:
        for key, value in clean.items():
            current.set_attribute(key, value)
        yield


def record_exception(error: Exception) -> None:
    """Attach an exception to the active span, if there is one."""
    if not _enabled:
        return
    try:
        from opentelemetry import trace

        current = trace.get_current_span()
        current.record_exception(error)
        current.set_status(trace.Status(trace.StatusCode.ERROR, str(error)))
    except Exception:  # noqa: BLE001 - telemetry must never break a run
        pass


def instrument_fastapi(app: Any) -> None:
    """Add HTTP request spans, if the instrumentation is installed."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except ImportError:
        log.debug("FastAPI instrumentation not installed")
