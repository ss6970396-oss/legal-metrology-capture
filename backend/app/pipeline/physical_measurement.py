"""Physical text-size measurement — the v1 "no" and what it would take.

§10 decides this explicitly: **do not claim automated font-size compliance
from ordinary uncontrolled smartphone photographs in v1.**

This module exists so that decision is expressed in code rather than only in a
document. It is deliberately a module that declines to do something, because
the failure mode it prevents is subtle: nothing in the pipeline stops anyone
dividing a region's pixel height by an assumed DPI and emitting a millimetre
figure. The result would look like every other fact — typed, provenance-bearing,
confident — and would be fabricated.

Rule 7 sets minimum letter and numeral heights against the principal display
panel's area. Checking that requires millimetres. A bounding box gives pixels.
The conversion needs a scale, and a scale needs one of:

* a known physical dimension visible in the frame (a reference object, or the
  package's own declared dimensions),
* camera intrinsics *plus* subject distance — focal length alone is not enough,
* a depth measurement from ARCore or ARKit on a device that supports it,
* a controlled capture protocol that fixes the distance.

None of those is available in the general v1 capture path, so
:func:`assess_measurability` returns :data:`NOT_TESTABLE` and the resolver
emits a fact saying so. When a calibrated capture mode is built, this is where
its scale model plugs in — and §19 requires it to clear an accuracy acceptance
gate before anything it produces becomes compliance-relevant evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ScaleSource(str, Enum):
    """How a pixel-to-millimetre scale might be established."""

    NONE = "none"
    REFERENCE_OBJECT = "reference_object"
    DECLARED_PACKAGE_DIMENSIONS = "declared_package_dimensions"
    CAMERA_INTRINSICS_AND_DISTANCE = "camera_intrinsics_and_distance"
    ARCORE_DEPTH = "arcore_depth"
    ARKIT_LIDAR = "arkit_lidar"
    CONTROLLED_CAPTURE_DISTANCE = "controlled_capture_distance"


@dataclass(frozen=True)
class ScaleEstimate:
    """A pixel-to-millimetre scale, if one could be established.

    ``error_bound_mm`` is required and must not be None for a usable estimate.
    §10's list of what robust inference needs ends with "an empirically
    demonstrated error bound across devices, angles, curved surfaces, glare and
    small text" — a scale without a measured error bound cannot support a
    statement about a legal minimum, because there is no way to say whether a
    letter measured at 1.6mm clears a 1.5mm floor.
    """

    mm_per_pixel: float
    source: ScaleSource
    error_bound_mm: float
    #: Set when the estimate came from a protocol that has passed §19's gate.
    validated: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "mm_per_pixel": round(self.mm_per_pixel, 6),
            "source": self.source.value,
            "error_bound_mm": round(self.error_bound_mm, 3),
            "validated": self.validated,
        }


@dataclass(frozen=True)
class MeasurabilityAssessment:
    """Whether physical measurement is possible for one artifact."""

    testable: bool
    reason: str
    scale: ScaleEstimate | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "testable": self.testable,
            "reason": self.reason,
            "scale": self.scale.to_json() if self.scale else None,
        }


NOT_TESTABLE = MeasurabilityAssessment(
    testable=False,
    reason=(
        "No validated pixel-to-millimetre scale is available for an "
        "uncontrolled handheld capture. Physical letter-height measurement is "
        "out of scope for v1 (contract v1.1 §10). The text evidence is "
        "preserved; the measurement is reported as not testable rather than "
        "estimated from pixels."
    ),
)


def assess_measurability(
    capture_metadata: dict[str, Any],
    scale: ScaleEstimate | None = None,
) -> MeasurabilityAssessment:
    """Decide whether a physical measurement may be attempted.

    The default answer is no, and the only way to get a yes is to hand in a
    scale that is both validated and carries an error bound. Note what is
    *not* sufficient: a focal length in the EXIF. That is the most tempting
    input in ``capture_metadata`` and it is not a scale — without subject
    distance it says nothing about how many millimetres a pixel covers, and a
    pipeline that treated its presence as enabling measurement would produce
    confident nonsense on every package photographed at an unusual distance.
    """
    if scale is None:
        return NOT_TESTABLE

    if not scale.validated:
        return MeasurabilityAssessment(
            testable=False,
            reason=(
                f"A scale from {scale.source.value} was supplied but has not "
                "passed the accuracy validation gate required by §19. It may "
                "not be used for compliance-relevant measurement."
            ),
            scale=scale,
        )

    if scale.error_bound_mm <= 0:
        return MeasurabilityAssessment(
            testable=False,
            reason=(
                "The supplied scale carries no positive error bound, so no "
                "statement about a legal minimum height can be supported."
            ),
            scale=scale,
        )

    return MeasurabilityAssessment(
        testable=True,
        reason=f"Validated scale from {scale.source.value}.",
        scale=scale,
    )


def measure_letter_height_mm(
    pixel_height: float,
    assessment: MeasurabilityAssessment,
) -> tuple[float, float] | None:
    """Convert a region's pixel height to millimetres, with its error bound.

    Returns ``(height_mm, error_bound_mm)`` or ``None``. The bound is returned
    alongside the value rather than as an optional extra so that no caller can
    take the measurement without also taking its uncertainty — the number on
    its own is the thing §10 forbids producing.
    """
    if not assessment.testable or assessment.scale is None:
        return None
    scale = assessment.scale
    return pixel_height * scale.mm_per_pixel, scale.error_bound_mm
