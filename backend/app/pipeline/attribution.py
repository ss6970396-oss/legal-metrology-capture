"""Semantic field attribution.

Turns OCR observations into typed :class:`FieldCandidate` proposals. This is
stage 9 of the pipeline, and it is the stage most likely to be wrong in a way
that looks right: a number read perfectly and attributed to the wrong field
produces an OBSERVED fact with full provenance and a high OCR confidence,
which is exactly the shape of a fact nobody questions.

So the rules here are label-driven, not value-driven. A number is only an MRP
if something nearby says so. §5's warning is the design brief: "price strings
elsewhere on package are not necessarily MRP", "numbers alone are ambiguous",
"avoid interpreting unrelated numbers as dates". Every attributor below
requires a label, either in its own text or in a neighbouring region.

Deterministic and rule-based on purpose. §1 permits a constrained model to
assist here later, but it must sit behind typed validation and evidence
requirements — so the rule path has to exist and be measurable first, which is
what §17's per-field precision/recall/F1 measures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from ..domain.geometry import Geometry
from ..domain.observation import (
    FieldCandidate,
    OcrObservation,
    Region,
    SurfaceView,
)
from . import normalize as nz

#: Bump when attribution behaviour changes. Travels with every candidate.
ATTRIBUTION_VERSION = "attr-1.0.0"


@dataclass(frozen=True)
class AttributionContext:
    """One observation plus what surrounds it.

    Neighbouring text matters because packaging separates a label from its
    value constantly — "M.R.P." on one line and "Rs. 120" on the next are two
    regions to the detector and one declaration to a reader. An attributor
    that only saw its own region would miss most real declarations.
    """

    observation: OcrObservation
    region: Region
    surface: SurfaceView
    #: Text of regions near this one, nearest first. Already cleaned.
    neighbours: tuple[str, ...]
    region_quality: float

    @property
    def text(self) -> str:
        return nz.clean_text(self.observation.raw_text)

    @property
    def text_with_neighbours(self) -> str:
        """This region's text followed by its neighbours'.

        Ordering matters: the region's own text comes first so that a parser
        scanning left to right prefers a value printed in the same region over
        one borrowed from a neighbour.
        """
        return " ".join((self.text, *self.neighbours)).strip()


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------

#: Attribution confidence by how the field was identified.
#:
#: These are starting values, not calibrated ones, and they are used only to
#: rank candidates for the same field — never as a probability of correctness.
#: §17 measures attribution with precision/recall/F1 against ground truth and
#: these should be re-fitted from that, not tuned by intuition.
_CONF_LABEL_IN_REGION = 0.94
_CONF_LABEL_IN_NEIGHBOUR = 0.78
_CONF_PDP_BONUS = 0.03


def _confidence(
    *, label_in_region: bool, surface: SurfaceView, cap: float = 0.97
) -> float:
    base = _CONF_LABEL_IN_REGION if label_in_region else _CONF_LABEL_IN_NEIGHBOUR
    if surface.is_principal_display_panel:
        # A declaration on the principal display panel is marginally more
        # likely to be the operative one. A nudge, not a decision — the
        # resolver still compares values rather than trusting the surface.
        base += _CONF_PDP_BONUS
    return min(base, cap)


# ---------------------------------------------------------------------------
# Per-field attributors
# ---------------------------------------------------------------------------

Attributor = Callable[[AttributionContext], "list[_Attribution]"]


@dataclass(frozen=True)
class _Attribution:
    """An attributor's proposal, before it becomes a stored candidate."""

    field: str
    value: object
    confidence: float
    rule: str
    rationale: str


def _attribute_mrp(ctx: AttributionContext) -> list[_Attribution]:
    label_here = nz.looks_like_mrp_label(ctx.text)
    label_near = label_here or any(
        nz.looks_like_mrp_label(n) for n in ctx.neighbours
    )
    if not label_near:
        return []

    money = nz.parse_money(ctx.text) or nz.parse_money(ctx.text_with_neighbours)
    if money is None:
        return []

    rationale = "MRP label in region" if label_here else "MRP label in neighbour"
    if nz.mentions_inclusive_of_taxes(ctx.text_with_neighbours):
        rationale += "; states inclusive of taxes"

    return [
        _Attribution(
            field="package.mrp",
            value=money.to_json(),
            confidence=_confidence(label_in_region=label_here, surface=ctx.surface),
            rule=f"mrp/label+currency@{nz.NORMALIZER_VERSION}",
            rationale=rationale,
        )
    ]


def _attribute_net_quantity(ctx: AttributionContext) -> list[_Attribution]:
    label_here = nz.looks_like_net_quantity_label(ctx.text)
    label_near = label_here or any(
        nz.looks_like_net_quantity_label(n) for n in ctx.neighbours
    )
    if not label_near:
        return []

    quantity = nz.parse_quantity(ctx.text) or nz.parse_quantity(
        ctx.text_with_neighbours
    )
    if quantity is None:
        return []

    return [
        _Attribution(
            field="package.net_quantity",
            value=quantity.to_json(),
            confidence=_confidence(label_in_region=label_here, surface=ctx.surface),
            rule=f"net_quantity/label+unit@{nz.NORMALIZER_VERSION}",
            rationale=(
                "net quantity label in region"
                if label_here
                else "net quantity label in neighbour"
            ),
        )
    ]


def _attribute_unit_sale_price(ctx: AttributionContext) -> list[_Attribution]:
    """Only ever attributes a *printed* declaration.

    The derived form is produced by the resolver, from resolved facts, with a
    derivation trace. Producing it here would give it a region and an OCR run
    it did not come from — provenance pointing at pixels that do not show it.
    """
    label_here = nz.looks_like_unit_price_label(ctx.text)
    label_near = label_here or any(
        nz.looks_like_unit_price_label(n) for n in ctx.neighbours
    )
    if not label_near:
        return []

    price = nz.parse_unit_sale_price(ctx.text) or nz.parse_unit_sale_price(
        ctx.text_with_neighbours
    )
    if price is None:
        return []

    return [
        _Attribution(
            field="package.unit_sale_price",
            value=price.to_json(),
            confidence=_confidence(label_in_region=label_here, surface=ctx.surface),
            rule=f"unit_sale_price/printed@{nz.NORMALIZER_VERSION}",
            rationale="printed unit sale price declaration",
        )
    ]


def _attribute_dates(ctx: AttributionContext) -> list[_Attribution]:
    """Manufacture/pack and best-before dates.

    Both labels are checked against the region's own text before the
    neighbours', because a panel routinely prints both dates within millimetres
    of each other. Borrowing a label from a neighbour there would attribute the
    manufacture date to best-before as often as not, so a region carrying its
    own label is not allowed to also match the other one from a neighbour.
    """
    out: list[_Attribution] = []

    mfg_here = nz.looks_like_manufacture_label(ctx.text)
    bb_here = nz.looks_like_best_before_label(ctx.text)

    if mfg_here or bb_here:
        parsed = nz.parse_date(ctx.text)
        if parsed is not None:
            # A region labelled with both is ambiguous; decline rather than
            # attribute the one date to whichever label matched first.
            if mfg_here and not bb_here:
                out.append(
                    _Attribution(
                        field="declarations.manufacture_date",
                        value=parsed.to_json(),
                        confidence=_confidence(
                            label_in_region=True, surface=ctx.surface
                        ),
                        rule=f"date/mfg-label@{nz.NORMALIZER_VERSION}",
                        rationale=f"manufacture label, {parsed.precision} precision",
                    )
                )
            elif bb_here and not mfg_here:
                out.append(
                    _Attribution(
                        field="declarations.best_before",
                        value=parsed.to_json(),
                        confidence=_confidence(
                            label_in_region=True, surface=ctx.surface
                        ),
                        rule=f"date/best-before-label@{nz.NORMALIZER_VERSION}",
                        rationale=f"best-before label, {parsed.precision} precision",
                    )
                )
        return out

    # No label in this region. Try the neighbours, but only when exactly one
    # kind of date label is nearby — otherwise the same ambiguity applies.
    mfg_near = any(nz.looks_like_manufacture_label(n) for n in ctx.neighbours)
    bb_near = any(nz.looks_like_best_before_label(n) for n in ctx.neighbours)
    if mfg_near == bb_near:
        return out

    parsed = nz.parse_date(ctx.text)
    if parsed is None:
        return out

    field_name = (
        "declarations.manufacture_date" if mfg_near else "declarations.best_before"
    )
    out.append(
        _Attribution(
            field=field_name,
            value=parsed.to_json(),
            confidence=_confidence(label_in_region=False, surface=ctx.surface),
            rule=f"date/neighbour-label@{nz.NORMALIZER_VERSION}",
            rationale="date label in neighbouring region",
        )
    )
    return out


def _attribute_country_of_origin(ctx: AttributionContext) -> list[_Attribution]:
    country = nz.parse_country_of_origin(ctx.text)
    if country is None:
        return []
    return [
        _Attribution(
            field="declarations.country_of_origin",
            value=country,
            confidence=_confidence(label_in_region=True, surface=ctx.surface),
            rule=f"origin/label@{nz.NORMALIZER_VERSION}",
            rationale="country-of-origin label in region",
        )
    ]


def _attribute_parties(ctx: AttributionContext) -> list[_Attribution]:
    """Manufacturer, packer, importer.

    "Marketed by" is parsed and then dropped: the contract's ``declarations``
    block has no marketer field, and §4 forbids the extraction layer adding
    fields to the contract. The observation survives in the store, so adding
    the field later is a projection change rather than a re-extraction.
    """
    party = nz.parse_party(ctx.text)
    if party is None:
        return []

    field_map = {
        "manufacturer": "declarations.manufacturer",
        "packer": "declarations.packer",
        "importer": "declarations.importer",
    }
    field_name = field_map.get(party.role)
    if field_name is None:
        return []

    return [
        _Attribution(
            field=field_name,
            value=party.to_json(),
            confidence=_confidence(label_in_region=True, surface=ctx.surface),
            rule=f"party/{party.role}@{nz.NORMALIZER_VERSION}",
            rationale=f"'{party.role}' role label in region",
        )
    ]


def _attribute_consumer_care(ctx: AttributionContext) -> list[_Attribution]:
    """Consumer care name/address/phone/email.

    The label must be in the region's own text. That is a stricter rule than
    the numeric attributors use, and it is necessary because this field's value
    is free text rather than a parsed number.

    For MRP, borrowing a label from a neighbour is safe: the value is still the
    number that follows the currency marker, so every region near the label
    parses out the same 120.0 and they merge. For consumer care the value *is*
    the text, so a region that borrowed the label would take its own text plus
    its neighbours' as the value — and every nearby region would produce a
    differently-concatenated string. Those look like disagreeing readings to
    the resolver, and one printed consumer-care line comes out CONFLICTING.

    So the label anchors the region, and only a neighbour that supplies a
    missing contact detail is appended.
    """
    if not nz.looks_like_consumer_care_label(ctx.text):
        return []

    # The whole declaration is in this region: the ordinary case.
    care = nz.parse_consumer_care(ctx.text)
    if care is not None:
        return [
            _Attribution(
                field="declarations.consumer_care",
                value=care.to_json(),
                confidence=_confidence(label_in_region=True, surface=ctx.surface),
                rule=f"consumer_care/label+contact@{nz.NORMALIZER_VERSION}",
                rationale="consumer-care label and contact detail in region",
            )
        ]

    # Labelled here, contact detail on the next line. Join this region to the
    # first neighbour that completes it — and only that one, so the value stays
    # something the package actually prints.
    for neighbour in ctx.neighbours:
        joined = f"{ctx.text} {neighbour}".strip()
        completed = nz.parse_consumer_care(joined)
        if completed is not None:
            return [
                _Attribution(
                    field="declarations.consumer_care",
                    value=completed.to_json(),
                    confidence=_confidence(
                        label_in_region=False, surface=ctx.surface
                    ),
                    rule=f"consumer_care/label+adjacent-contact@{nz.NORMALIZER_VERSION}",
                    rationale="consumer-care label with contact in the next region",
                )
            ]

    return []


def _attribute_generic_name(ctx: AttributionContext) -> list[_Attribution]:
    name = nz.parse_generic_name(ctx.text)
    if name is None:
        return []
    return [
        _Attribution(
            field="declarations.generic_name",
            value=name,
            confidence=_confidence(label_in_region=True, surface=ctx.surface),
            rule=f"generic_name/label@{nz.NORMALIZER_VERSION}",
            rationale="generic-name label in region",
        )
    ]


#: Every attributor, run over every observation.
#:
#: Order is irrelevant — each returns independently and the resolver reconciles
#: afterwards — so this stays a plain list rather than a priority chain.
ATTRIBUTORS: tuple[Attributor, ...] = (
    _attribute_mrp,
    _attribute_net_quantity,
    _attribute_unit_sale_price,
    _attribute_dates,
    _attribute_country_of_origin,
    _attribute_parties,
    _attribute_consumer_care,
    _attribute_generic_name,
)


# ---------------------------------------------------------------------------
# Neighbour search
# ---------------------------------------------------------------------------

#: How far from a region to look for its label, as a multiple of the region's
#: own height. Labels sit on the line above or beside their value, so a couple
#: of line-heights is the right scale; much more and a dense panel makes every
#: region a neighbour of every other.
NEIGHBOUR_RADIUS_MULTIPLE = 2.5

#: Most neighbours to consider, nearest first. Bounds the cost on a dense
#: label and stops a crowded region borrowing a label from across the panel.
MAX_NEIGHBOURS = 6


def find_neighbours(
    region: Region,
    others: Sequence[tuple[Region, OcrObservation]],
) -> tuple[str, ...]:
    """Text of the regions nearest ``region``, nearest first.

    Distance is measured between bounding-box centres and scaled by the
    region's own height, so the radius adapts to the text size: a small-print
    region looks for its label within a small-print distance, and a large
    front-panel region within a large one.
    """
    left, top, right, bottom = region.geometry.bounds
    cx, cy = (left + right) / 2, (top + bottom) / 2
    height = max(bottom - top, 1.0)
    radius = height * NEIGHBOUR_RADIUS_MULTIPLE

    scored: list[tuple[float, str]] = []
    for other, observation in others:
        if other.region_id == region.region_id:
            continue
        if other.artifact_id != region.artifact_id:
            continue
        ol, ot, orr, ob = other.geometry.bounds
        ox, oy = (ol + orr) / 2, (ot + ob) / 2
        distance = ((ox - cx) ** 2 + (oy - cy) ** 2) ** 0.5
        if distance <= radius:
            scored.append((distance, nz.clean_text(observation.raw_text)))

    scored.sort(key=lambda pair: pair[0])
    return tuple(text for _, text in scored[:MAX_NEIGHBOURS] if text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def attribute(
    pairs: Sequence[tuple[Region, OcrObservation]],
    surfaces: dict[str, SurfaceView],
) -> list[FieldCandidate]:
    """Produce field candidates for every observation in one package.

    ``surfaces`` maps artifact_id to which face it shows, so a candidate can
    record the surface it was found on without this module needing the
    artifact records themselves.

    Returns candidates, not facts. Two regions proposing different MRPs both
    appear here; deciding between them is the resolver's job and doing it here
    would hide the disagreement §15 requires to stay visible.
    """
    candidates: list[FieldCandidate] = []

    for region, observation in pairs:
        ctx = AttributionContext(
            observation=observation,
            region=region,
            surface=surfaces.get(region.artifact_id, SurfaceView.UNKNOWN),
            neighbours=find_neighbours(region, pairs),
            region_quality=float(region.quality.get("overall", 1.0)),
        )

        for attributor in ATTRIBUTORS:
            for proposal in attributor(ctx):
                candidates.append(
                    FieldCandidate.create(
                        field_name=proposal.field,
                        observation=observation,
                        region=region,
                        normalized_value=proposal.value,
                        attribution_confidence=proposal.confidence,
                        normalization_rule=proposal.rule,
                        surface=ctx.surface,
                        region_quality=ctx.region_quality,
                        rationale=proposal.rationale,
                    )
                )

    return candidates


def observed_statements(
    pairs: Iterable[tuple[Region, OcrObservation]],
) -> dict[str, list[Geometry]]:
    """Package statements worth recording that are not contract fields.

    "Not for retail sale" is the motivating case. §5.1 forbids turning it into
    DECLARED_ABSENCE values, but it is still an observation Team 2 may want to
    reason about — so it is captured here, with its geometry, and passed
    through in the extraction diagnostics rather than into ``facts[]``.
    """
    found: dict[str, list[Geometry]] = {}
    for region, observation in pairs:
        if nz.mentions_not_for_retail_sale(observation.raw_text):
            found.setdefault("not_for_retail_sale", []).append(region.geometry)
        if nz.mentions_inclusive_of_taxes(observation.raw_text):
            found.setdefault("mrp_inclusive_of_taxes", []).append(region.geometry)
    return found
