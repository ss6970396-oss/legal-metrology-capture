"""Backend image quality diagnostics.

Stage 3, and §11 makes it the authoritative one: "backend quality gates remain
authoritative because the mobile device cannot reproduce server-side model
evaluation exactly". The phone's checks exist to stop obviously unusable frames
being uploaded; these decide whether an uploaded frame is good enough to
extract facts from.

The rule that shapes this module is §14's: **do not deploy a universal
threshold**. There is no "15 pixels is unreadable" constant here and no
Laplacian cutoff presented as a pass mark. What there is:

* Measurements, always computed and always stored.
* A verdict derived from a :class:`QualityPolicy` that is loaded, versioned and
  replaceable — with the shipped default explicitly marked uncalibrated.

That split is what makes §17's calibration possible after the fact: every
extraction stores its raw features, so a policy can be re-fitted against
observed OCR failure without re-photographing anything.

The controller states are §14's four: PASS, RETAKE, CONTINUE, ABSTAIN. ABSTAIN
is the one that matters most and is easiest to leave out — it is the honest
answer when the diagnostics themselves could not run, and collapsing it into
PASS would let an unassessed image through looking assessed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..domain.geometry import Geometry

log = logging.getLogger(__name__)

#: Bump whenever a measurement's definition changes. Stored on every report;
#: scores from different versions are not comparable.
QUALITY_PIPELINE_VERSION = "bq-1.0.0"


class CaptureVerdict(str, Enum):
    """§14's capture-controller states."""

    #: Evidence is likely sufficient.
    PASS = "PASS"

    #: A known defect strongly predicts extraction failure.
    RETAKE = "RETAKE"

    #: Usable, but package coverage is incomplete.
    CONTINUE = "CONTINUE"

    #: The system cannot confidently determine quality. Not a pass.
    ABSTAIN = "ABSTAIN"


@dataclass(frozen=True)
class Measurement:
    """One measured feature, with its verdict kept separate from its value.

    The separation is the point. ``value`` is a fact about the image and stays
    true forever; ``concerning`` is this policy version's opinion about it and
    is expected to change when the policy is re-fitted.
    """

    name: str
    value: float
    unit: str
    concerning: bool
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": round(self.value, 4),
            "unit": self.unit,
            "concerning": self.concerning,
            "note": self.note,
        }


@dataclass
class QualityReport:
    artifact_id: str
    verdict: CaptureVerdict
    measurements: list[Measurement] = field(default_factory=list)
    policy_version: str = ""
    pipeline_version: str = QUALITY_PIPELINE_VERSION
    #: Set when the diagnostics could not run at all.
    error: str | None = None

    @property
    def concerns(self) -> list[Measurement]:
        return [m for m in self.measurements if m.concerning]

    def measurement(self, name: str) -> Measurement | None:
        for m in self.measurements:
            if m.name == name:
                return m
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "verdict": self.verdict.value,
            "measurements": [m.to_json() for m in self.measurements],
            "policy_version": self.policy_version,
            "pipeline_version": self.pipeline_version,
            "error": self.error,
        }


@dataclass(frozen=True)
class QualityPolicy:
    """Thresholds that turn measurements into a verdict.

    Every value here is a **starting point chosen to be plausible**, not a
    calibrated one. §14 requires these to be fitted against a labelled
    benchmark that correlates each feature with actual OCR and field-extraction
    failure, and until that has been done a RETAKE from this policy is a
    suggestion rather than a finding.

    ``version`` travels with every report so that scores gathered under
    different policies stay distinguishable. Change a number, change the
    version.

    ``calibrated`` is False in the shipped default and the runner logs a
    warning while it stays that way. That is deliberate friction: the failure
    mode this guards against is a plausible-looking default quietly becoming
    production policy because nobody remembered it was a guess.
    """

    version: str = "qpolicy-1.0.0-uncalibrated"
    calibrated: bool = False

    #: Variance of the Laplacian, on a 1024px-longest-edge working image.
    #: Scale-dependent: changing the working size invalidates these.
    blur_retake_below: float = 60.0
    blur_concern_below: float = 140.0

    #: Mean luminance, 0..255.
    brightness_retake_below: float = 40.0
    brightness_retake_above: float = 220.0
    brightness_concern_below: float = 70.0
    brightness_concern_above: float = 195.0

    #: Fraction of pixels at the extremes of the range.
    clipped_concern_fraction: float = 0.10
    clipped_retake_fraction: float = 0.25

    #: Fraction of the frame in a single near-saturated specular blob.
    glare_retake_fraction: float = 0.030
    glare_concern_fraction: float = 0.015

    #: RMS contrast below which a panel is likely unreadable.
    contrast_retake_below: float = 18.0
    contrast_concern_below: float = 32.0

    #: Longest edge of the working image. Every scale-dependent threshold above
    #: is expressed against this.
    working_longest_edge: int = 1024

    def to_json(self) -> dict[str, Any]:
        return {"version": self.version, "calibrated": self.calibrated}


DEFAULT_POLICY = QualityPolicy()


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def _load_working_image(image_path: Path, longest_edge: int) -> Any:
    """Decode and downscale to the policy's working size.

    Downscaling before measuring is what makes the thresholds meaningful
    across devices: Laplacian variance scales with resolution, so a 48MP phone
    and a 12MP one would otherwise land on opposite sides of any blur cutoff
    while producing equally readable photographs.
    """
    import cv2

    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"could not decode image at {image_path}")

    height, width = image.shape[:2]
    longest = max(height, width)
    if longest > longest_edge:
        scale = longest_edge / longest
        image = cv2.resize(
            image,
            (max(1, int(width * scale)), max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    return image


def analyse(
    image_path: Path,
    artifact_id: str,
    policy: QualityPolicy = DEFAULT_POLICY,
) -> QualityReport:
    """Measure one artifact and derive a verdict.

    Never raises for an unreadable or odd image. A decode failure produces an
    ABSTAIN report carrying the error, because the pipeline needs to record
    that quality was not assessed — which is a different claim from the image
    being bad, and the two must not be conflated.
    """
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        return QualityReport(
            artifact_id=artifact_id,
            verdict=CaptureVerdict.ABSTAIN,
            policy_version=policy.version,
            error=f"image diagnostics unavailable: {exc}",
        )

    try:
        image = _load_working_image(image_path, policy.working_longest_edge)
    except Exception as exc:  # noqa: BLE001
        return QualityReport(
            artifact_id=artifact_id,
            verdict=CaptureVerdict.ABSTAIN,
            policy_version=policy.version,
            error=str(exc),
        )

    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    measurements: list[Measurement] = []

    # --- Blur -----------------------------------------------------------
    blur = float(cv2.Laplacian(grey, cv2.CV_64F).var())
    measurements.append(
        Measurement(
            name="blur_laplacian_variance",
            value=blur,
            unit="variance",
            concerning=blur < policy.blur_concern_below,
            note="higher is sharper; scale-dependent on the working image size",
        )
    )

    # --- Brightness -----------------------------------------------------
    brightness = float(grey.mean())
    measurements.append(
        Measurement(
            name="mean_luminance",
            value=brightness,
            unit="0-255",
            concerning=(
                brightness < policy.brightness_concern_below
                or brightness > policy.brightness_concern_above
            ),
        )
    )

    # --- Clipping -------------------------------------------------------
    total = grey.size
    shadow = float((grey <= 16).sum()) / total
    highlight = float((grey >= 239).sum()) / total
    clipped = shadow + highlight
    measurements.append(
        Measurement(
            name="clipped_fraction",
            value=clipped,
            unit="fraction",
            concerning=clipped > policy.clipped_concern_fraction,
            note=f"shadow {shadow:.3f}, highlight {highlight:.3f}",
        )
    )

    # --- Contrast -------------------------------------------------------
    contrast = float(grey.std())
    measurements.append(
        Measurement(
            name="rms_contrast",
            value=contrast,
            unit="0-255",
            concerning=contrast < policy.contrast_concern_below,
        )
    )

    # --- Glare ----------------------------------------------------------
    #
    # Largest connected near-saturated region, not the total bright area.
    # A white label is uniformly bright and legible; a specular hotspot is a
    # concentrated blob that destroys the text under it. The total fraction
    # cannot tell those apart, which is the known weak point the mobile
    # implementation documents — measuring the largest blob separates them.
    saturated = (grey >= 250).astype("uint8")
    glare_fraction = 0.0
    if saturated.any():
        count, labels, stats, _ = cv2.connectedComponentsWithStats(
            saturated, connectivity=8
        )
        if count > 1:
            # Row 0 is the background component.
            largest = int(stats[1:, cv2.CC_STAT_AREA].max())
            glare_fraction = largest / total
    measurements.append(
        Measurement(
            name="largest_specular_blob_fraction",
            value=glare_fraction,
            unit="fraction",
            concerning=glare_fraction > policy.glare_concern_fraction,
            note="largest connected saturated region, not total bright area",
        )
    )

    # --- Perspective ----------------------------------------------------
    #
    # Reported, never gated on. §7's own research note says projective
    # distortion affects recognition reliability, but the rectification stage
    # exists precisely to handle it, so a skewed photograph is a job for the
    # pipeline rather than a reason to send the inspector back.
    skew = _dominant_text_skew(grey)
    if skew is not None:
        measurements.append(
            Measurement(
                name="dominant_edge_skew_degrees",
                value=skew,
                unit="degrees",
                concerning=False,
                note="diagnostic only; rectification handles skew",
            )
        )

    verdict = _verdict_for(measurements, policy)
    return QualityReport(
        artifact_id=artifact_id,
        verdict=verdict,
        measurements=measurements,
        policy_version=policy.version,
    )


def _dominant_text_skew(grey: Any) -> float | None:
    """Angle of the dominant straight edges, if any are found."""
    try:
        import cv2
        import numpy as np

        edges = cv2.Canny(grey, 60, 180)
        lines = cv2.HoughLines(edges, 1, np.pi / 180, threshold=160)
        if lines is None or len(lines) == 0:
            return None

        angles = []
        for line in lines[:60]:
            theta = float(line[0][1])
            degrees = (theta * 180.0 / np.pi) - 90.0
            while degrees >= 45:
                degrees -= 90
            while degrees < -45:
                degrees += 90
            angles.append(degrees)
        return float(np.median(angles)) if angles else None
    except Exception:  # noqa: BLE001 - a diagnostic must never break the run
        return None


def _verdict_for(
    measurements: list[Measurement], policy: QualityPolicy
) -> CaptureVerdict:
    """Reduce measurements to a controller state.

    RETAKE requires a measurement past the *retake* threshold, not merely a
    concerning one. Concerning features are recorded and let through: §14 wants
    RETAKE reserved for "a known quality defect strongly predictive of
    failure", and an over-eager gate costs the inspector a second trip to a
    premises they have already left.
    """
    by_name = {m.name: m for m in measurements}

    def value(name: str) -> float | None:
        m = by_name.get(name)
        return m.value if m is not None else None

    blur = value("blur_laplacian_variance")
    if blur is not None and blur < policy.blur_retake_below:
        return CaptureVerdict.RETAKE

    brightness = value("mean_luminance")
    if brightness is not None and (
        brightness < policy.brightness_retake_below
        or brightness > policy.brightness_retake_above
    ):
        return CaptureVerdict.RETAKE

    clipped = value("clipped_fraction")
    if clipped is not None and clipped > policy.clipped_retake_fraction:
        return CaptureVerdict.RETAKE

    glare = value("largest_specular_blob_fraction")
    if glare is not None and glare > policy.glare_retake_fraction:
        return CaptureVerdict.RETAKE

    contrast = value("rms_contrast")
    if contrast is not None and contrast < policy.contrast_retake_below:
        return CaptureVerdict.RETAKE

    return CaptureVerdict.PASS


# ---------------------------------------------------------------------------
# Region-level quality
# ---------------------------------------------------------------------------

def region_quality(
    image_path: Path,
    geometry: Geometry,
    policy: QualityPolicy = DEFAULT_POLICY,
) -> dict[str, float]:
    """Measure quality inside one detected text region.

    §7 prefers this over global gating, for a concrete reason: a photograph of
    a package can be sharp, well exposed and perfectly framed while the one
    declaration panel that matters sits in shadow behind a reflection. A global
    score says the image is fine. The region score says the MRP is unreadable,
    and that is the one that should influence the fact's confidence.

    Returns an empty mapping when the region cannot be measured, and callers
    treat that as "no region signal" rather than as a low score.
    """
    try:
        import cv2
    except ImportError:
        return {}

    try:
        image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            return {}

        height, width = image.shape[:2]
        left, top, right, bottom = geometry.bounds
        x0 = max(0, int(left))
        y0 = max(0, int(top))
        x1 = min(width, int(right))
        y1 = min(height, int(bottom))
        if x1 - x0 < 4 or y1 - y0 < 4:
            return {}

        crop = image[y0:y1, x0:x1]
        sharpness = float(cv2.Laplacian(crop, cv2.CV_64F).var())
        contrast = float(crop.std())
        saturated = float((crop >= 250).sum()) / crop.size

        # Normalised to 0..1 for use as a confidence component. The divisors
        # are the same uncalibrated starting points as the global policy and
        # carry the same caveat.
        overall = min(
            1.0,
            (min(sharpness / 150.0, 1.0) * 0.6)
            + (min(contrast / 50.0, 1.0) * 0.4),
        ) * (1.0 - min(saturated * 2.0, 0.8))

        return {
            "sharpness": sharpness,
            "contrast": contrast,
            "saturated_fraction": saturated,
            "text_pixel_height": float(geometry.height),
            "overall": max(0.05, overall),
        }
    except Exception as exc:  # noqa: BLE001
        log.debug("region quality failed: %s", exc)
        return {}
