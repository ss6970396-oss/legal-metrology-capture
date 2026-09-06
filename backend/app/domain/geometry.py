"""Evidence geometry.

Team 2's v1.1 ruling accepts BBOX, QUADRILATERAL and POLYGON as provenance
geometry, which closes the old question about axis-aligned-only boxes. The
rule this module exists to enforce is the one from §9 of the architecture:
preserve the richest geometry the detector gave us, and derive a simpler form
only when a consumer actually needs one.

The temptation this guards against is normalising everything to a bounding box
at ingest because it makes the rest of the code simpler. It does, and it throws
away the evidence that a declaration was photographed at an angle, that two
detections on a curved bottle are the same line of text, or that a sticker
overlaps the print beneath it. None of that can be recovered later.

Geometry is evidence, not semantics. Nothing here knows what a region contains
and nothing here may be read as a physical measurement — see
``physical_measurement.py`` for why a pixel box is not a millimetre.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Sequence


class GeometryType(str, Enum):
    """The shapes the contract accepts, ordered loosely by richness."""

    BBOX = "BBOX"
    QUADRILATERAL = "QUADRILATERAL"
    POLYGON = "POLYGON"


class CoordinateSpace(str, Enum):
    """Which pixel grid a set of coordinates is expressed in.

    Every stored region must say this. OCR engines are handed rectified crops,
    not originals, and a coordinate that does not know which image it indexes
    is not provenance — it is a number that happens to look like one.
    """

    #: The decoded original artifact, EXIF orientation already applied.
    ORIGINAL_IMAGE_PX = "original_image_px"

    #: A crop or rectification derived from the original. Only meaningful
    #: alongside the transform that produced it.
    DERIVED_IMAGE_PX = "derived_image_px"


@dataclass(frozen=True)
class Point:
    x: float
    y: float

    def as_list(self) -> list[float]:
        return [self.x, self.y]


@dataclass(frozen=True)
class Geometry:
    """A localized piece of visual evidence.

    Stored as an ordered point ring regardless of type, so that widening a
    BBOX to a QUADRILATERAL later is a change of label rather than a change of
    representation. ``type`` records what the detector actually produced, which
    is the part worth preserving: a quadrilateral that happens to be
    axis-aligned is not the same claim as a box that was only ever a box.
    """

    type: GeometryType
    points: tuple[Point, ...]
    coordinate_space: CoordinateSpace = CoordinateSpace.ORIGINAL_IMAGE_PX

    def __post_init__(self) -> None:
        if len(self.points) < 3:
            raise ValueError(
                f"a {self.type.value} needs at least 3 points, got {len(self.points)}"
            )
        if self.type is GeometryType.QUADRILATERAL and len(self.points) != 4:
            raise ValueError(
                f"a QUADRILATERAL needs exactly 4 points, got {len(self.points)}"
            )

    # -- constructors ----------------------------------------------------

    @classmethod
    def bbox(
        cls,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        space: CoordinateSpace = CoordinateSpace.ORIGINAL_IMAGE_PX,
    ) -> "Geometry":
        """An axis-aligned box, given as two opposite corners in any order."""
        left, right = sorted((float(x0), float(x1)))
        top, bottom = sorted((float(y0), float(y1)))
        return cls(
            type=GeometryType.BBOX,
            points=(
                Point(left, top),
                Point(right, top),
                Point(right, bottom),
                Point(left, bottom),
            ),
            coordinate_space=space,
        )

    @classmethod
    def quad(
        cls,
        corners: Sequence[Sequence[float]],
        space: CoordinateSpace = CoordinateSpace.ORIGINAL_IMAGE_PX,
    ) -> "Geometry":
        """Four corners, in the order the detector reported them."""
        return cls(
            type=GeometryType.QUADRILATERAL,
            points=tuple(Point(float(p[0]), float(p[1])) for p in corners),
            coordinate_space=space,
        )

    @classmethod
    def polygon(
        cls,
        vertices: Sequence[Sequence[float]],
        space: CoordinateSpace = CoordinateSpace.ORIGINAL_IMAGE_PX,
    ) -> "Geometry":
        """Arbitrary outline, for curved or irregular text regions."""
        return cls(
            type=GeometryType.POLYGON,
            points=tuple(Point(float(p[0]), float(p[1])) for p in vertices),
            coordinate_space=space,
        )

    # -- derived views ---------------------------------------------------

    def to_bbox(self) -> "Geometry":
        """The axis-aligned box enclosing this shape.

        The compatibility layer §9 asks for: a simpler consumer gets a box
        without the richer shape being discarded upstream. Deliberately
        *returns* a new geometry rather than mutating, because the caller
        needing a box does not make the polygon wrong for everyone else.
        """
        xs = [p.x for p in self.points]
        ys = [p.y for p in self.points]
        return Geometry.bbox(
            min(xs), min(ys), max(xs), max(ys), space=self.coordinate_space
        )

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """``(left, top, right, bottom)`` of the enclosing box."""
        xs = [p.x for p in self.points]
        ys = [p.y for p in self.points]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def width(self) -> float:
        left, _, right, _ = self.bounds
        return right - left

    @property
    def height(self) -> float:
        _, top, _, bottom = self.bounds
        return bottom - top

    @property
    def area(self) -> float:
        """Shoelace area of the actual shape, not of its bounding box.

        The distinction matters for a rotated quadrilateral, whose bounding
        box can be nearly twice its true area.
        """
        total = 0.0
        n = len(self.points)
        for i in range(n):
            a = self.points[i]
            b = self.points[(i + 1) % n]
            total += a.x * b.y - b.x * a.y
        return abs(total) / 2.0

    @property
    def rotation_degrees(self) -> float:
        """Angle of the longest edge, in degrees, in ``[-90, 90)``.

        Useful for deciding whether a crop needs rectifying before OCR, and
        for spotting a side panel photographed sideways. A BBOX always reports
        0 — not because the text is level, but because an axis-aligned box
        cannot express that it isn't.
        """
        if self.type is GeometryType.BBOX:
            return 0.0
        longest = 0.0
        angle = 0.0
        n = len(self.points)
        for i in range(n):
            a = self.points[i]
            b = self.points[(i + 1) % n]
            length = math.hypot(b.x - a.x, b.y - a.y)
            if length > longest:
                longest = length
                angle = math.degrees(math.atan2(b.y - a.y, b.x - a.x))
        while angle >= 90:
            angle -= 180
        while angle < -90:
            angle += 180
        return angle

    # -- comparison ------------------------------------------------------

    def iou(self, other: "Geometry") -> float:
        """Intersection over union of the two enclosing boxes.

        Box-level rather than polygon-level, and callers should know that.
        It is used for de-duplicating detections and for matching a detection
        against ground truth, where box IoU is the conventional measure and
        the extra precision of true polygon intersection would not change the
        decision. It is *not* good enough to decide that two regions on a
        curved surface are the same text; that is what the resolver's value
        comparison is for.

        Returns 0 when the two are in different coordinate spaces: comparing
        a crop coordinate against an original-image coordinate is a bug, and
        silently returning a plausible overlap would hide it.
        """
        if self.coordinate_space is not other.coordinate_space:
            return 0.0

        al, at, ar, ab = self.bounds
        bl, bt, br, bb = other.bounds

        left = max(al, bl)
        top = max(at, bt)
        right = min(ar, br)
        bottom = min(ab, bb)
        if right <= left or bottom <= top:
            return 0.0

        intersection = (right - left) * (bottom - top)
        union = (
            (ar - al) * (ab - at) + (br - bl) * (bb - bt) - intersection
        )
        return intersection / union if union > 0 else 0.0

    def contains_fraction(self, other: "Geometry") -> float:
        """How much of ``other``'s box lies inside this one's, 0..1."""
        if self.coordinate_space is not other.coordinate_space:
            return 0.0

        al, at, ar, ab = self.bounds
        bl, bt, br, bb = other.bounds

        left = max(al, bl)
        top = max(at, bt)
        right = min(ar, br)
        bottom = min(ab, bb)
        if right <= left or bottom <= top:
            return 0.0

        intersection = (right - left) * (bottom - top)
        other_area = (br - bl) * (bb - bt)
        return intersection / other_area if other_area > 0 else 0.0

    # -- serialisation ---------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """The provenance ``geometry`` block of the v1.1 contract."""
        return {
            "type": self.type.value,
            "coordinates": [p.as_list() for p in self.points],
            "coordinate_space": self.coordinate_space.value,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Geometry":
        coords: Iterable[Sequence[float]] = raw["coordinates"]
        return cls(
            type=GeometryType(raw["type"]),
            points=tuple(Point(float(p[0]), float(p[1])) for p in coords),
            coordinate_space=CoordinateSpace(
                raw.get("coordinate_space", CoordinateSpace.ORIGINAL_IMAGE_PX.value)
            ),
        )


@dataclass(frozen=True)
class TransformStep:
    """One step of the chain from an original artifact to what OCR actually saw.

    Recorded so a region found on a rectified crop can be mapped back to the
    pixels of the original photograph. Without this chain a fact's provenance
    points at coordinates in an image that no longer exists anywhere, which is
    indistinguishable from having no provenance at all.
    """

    kind: str  # "crop" | "rotate" | "rectify" | "scale"
    #: Row-major 3x3 homogeneous matrix mapping source -> destination.
    matrix: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "matrix": [list(r) for r in self.matrix]}


IDENTITY: TransformStep = TransformStep(
    kind="identity",
    matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
)


@dataclass(frozen=True)
class TransformChain:
    """An ordered pipeline of transforms, with the inverse mapping available.

    §18 of the architecture requires the system to be able to answer "which
    pixels caused this fact". That is only answerable if every crop, rotation
    and rectification between the stored artifact and the OCR input is
    recorded — hence a chain rather than a single matrix, so each stage stays
    inspectable instead of being collapsed into an opaque product.
    """

    steps: tuple[TransformStep, ...] = ()

    def then(self, step: TransformStep) -> "TransformChain":
        return TransformChain(steps=self.steps + (step,))

    @property
    def is_identity(self) -> bool:
        return not self.steps

    def _combined(self) -> list[list[float]]:
        result = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        for step in self.steps:
            result = _matmul(step.matrix, result)
        return result

    def to_source(self, geometry: Geometry) -> Geometry:
        """Map a geometry found in the transformed image back to the original.

        Returns the geometry unchanged when the chain is empty, which keeps
        the common case — OCR run directly on the original — free of
        floating-point drift.
        """
        if self.is_identity:
            return geometry

        inverse = _invert(self._combined())
        mapped = [_apply(inverse, (p.x, p.y)) for p in geometry.points]
        return Geometry(
            type=geometry.type,
            points=tuple(Point(x, y) for x, y in mapped),
            coordinate_space=CoordinateSpace.ORIGINAL_IMAGE_PX,
        )

    def to_json(self) -> list[dict[str, Any]]:
        return [s.to_json() for s in self.steps]


def _matmul(
    a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]
) -> list[list[float]]:
    return [
        [sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
        for i in range(3)
    ]


def _apply(m: Sequence[Sequence[float]], p: tuple[float, float]) -> tuple[float, float]:
    x, y = p
    dx = m[0][0] * x + m[0][1] * y + m[0][2]
    dy = m[1][0] * x + m[1][1] * y + m[1][2]
    dw = m[2][0] * x + m[2][1] * y + m[2][2]
    if dw == 0:
        raise ValueError("transform mapped a point to infinity")
    return dx / dw, dy / dw


def _invert(m: Sequence[Sequence[float]]) -> list[list[float]]:
    """Explicit 3x3 inverse.

    Written out rather than pulled from numpy so that this module — which the
    contract's provenance correctness depends on — has no numeric dependency
    and behaves identically wherever it runs.
    """
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]

    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        raise ValueError("transform is not invertible; provenance would be lost")

    return [
        [(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
        [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
        [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det],
    ]
