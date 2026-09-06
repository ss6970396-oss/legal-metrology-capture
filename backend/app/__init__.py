"""Legal Metrology extraction backend.

Team 1's authoritative pipeline: image artifacts in, PackageFactSnapshot v1.1
out. It owns capture, image processing, OCR, observation construction,
normalization, fact reconciliation and snapshot generation.

It does not own legal applicability, compliance rules, verdicts or decision
traces. Those are Team 2's, and nothing in this package may produce a
PASS/FAIL/NOT_APPLICABLE result.
"""

__version__ = "1.0.0"
