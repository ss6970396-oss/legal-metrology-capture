# Legal Metrology Capture & Extraction

Team 1's upstream evidence pipeline for Indian Legal Metrology package
inspection. Photographs of a retail package go in; a provenance-bearing
`PackageFactSnapshot v1.1` comes out.

Two halves:

| | |
|---|---|
| **`lib/`** | Flutter capture client. Guided multi-surface capture, on-device quality assistance, declared commercial context, durable offline upload queue. |
| **`backend/`** | Python extraction service. Image diagnostics, OCR, observation store, field attribution, fact resolution, snapshot construction. See [`backend/README.md`](backend/README.md). |

## The boundary

Team 1 extracts and reconciles evidence. Team 2 owns legal applicability,
compliance rules, verdicts and decision traces.

Nothing in this repository produces a PASS/FAIL. The extraction service reports
pipeline states — `EXTRACTION_READY`, `NEEDS_RECAPTURE`, `INCOMPLETE_EVIDENCE`,
`QUARANTINED`, `EXTRACTION_ERROR` — which describe this system, not a package's
compliance. `INCOMPLETE_EVIDENCE` means we did not get enough photographs, not
that a declaration is missing.

## Flow

```
inspector → guided capture → quality assist → declared context
    │
    │  POST /v1/capture-sessions      (once the context is declared)
    │  POST /v1/artifacts             (per accepted image)
    │  POST /v1/extraction-jobs       (once the package is finished)
    ▼
quality → OCR → observations → candidates → resolved facts
    ▼
PackageFactSnapshot v1.1 → compliance engine
```

The upload queue enforces that ordering across restarts and network outages: a
session before its artifacts, every artifact before extraction.

## Running it

```bash
flutter test && flutter analyze          # 21 tests
cd backend && pip install -e ".[dev]" && python -m pytest   # 160 tests
```

## Two design rules worth knowing before reading the code

**Applicability flags are declared, never defaulted.** `is_imported`,
`is_for_retail`, `is_ecommerce_listing` and `sale_channel` each switch a body
of rules on or off downstream. The app models them as nullable, refuses to
serialise a half-answered context, and will not let a package be finished until
a person has answered all four. A default of `false` does not read downstream
as "nobody said" — it reads as an inspector asserting something about a package
they may never have checked.

**Absence of an observation is `UNKNOWN`, never `DECLARED_ABSENCE`.** "OCR
found no MRP" and "this package carries no price declaration" are different
claims, and only the second is a finding. The fact constructor demands a
written evidence basis for `DECLARED_ABSENCE` and refuses without one.

## Status

The capture client and the extraction pipeline are complete and tested. Three
things are explicitly **not** production-ready, each documented in
[`backend/README.md`](backend/README.md):

1. Quality thresholds are uncalibrated starting values, not fitted ones.
2. No OCR engine has been selected — PaddleOCR is a candidate, not a
   measurement.
3. **The ground-truth dataset does not exist.** The benchmark harness is
   built and has nothing to measure. This is field work, and it gates the
   other two.
