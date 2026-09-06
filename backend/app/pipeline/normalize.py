"""Deterministic normalization of recognised text into typed contract values.

Every function here is pure, total and versioned. Pure and total because §17
benchmarks normalization by exact match and a rule that sometimes throws is not
measurable; versioned because §18 requires knowing which rule transformed a
value, and a rule that changes without changing its identifier makes stored
results incomparable.

The rules are deliberately conservative. A parser that succeeds on ambiguous
input is worse than one that declines: a declined parse becomes UNKNOWN and
someone looks at the photograph, while a wrong parse becomes an OBSERVED fact
with provenance attached and looks entirely trustworthy. So where an input
admits two readings, these return ``None``.

Nothing here decides whether a value is lawful, required, or sufficient. That
is Team 2's. These functions only turn "MRP Rs. 120/-" into
``{"amount": 120.0, "currency": "INR"}``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

#: Bump when any rule below changes behaviour. Stored on every candidate.
NORMALIZER_VERSION: Final[str] = "norm-1.0.0"


# ---------------------------------------------------------------------------
# Text hygiene
# ---------------------------------------------------------------------------

#: Devanagari digits, which appear on bilingual packaging alongside Latin ones.
_DEVANAGARI_DIGITS: Final[dict[int, str]] = {
    ord("०"): "0",
    ord("१"): "1",
    ord("२"): "2",
    ord("३"): "3",
    ord("४"): "4",
    ord("५"): "5",
    ord("६"): "6",
    ord("७"): "7",
    ord("८"): "8",
    ord("९"): "9",
}

#: Characters OCR routinely substitutes inside an otherwise-numeric run.
#: Applied only when the surrounding token is already digit-dominated — see
#: :func:`_repair_numeric`. Applying them globally would corrupt real words.
_NUMERIC_CONFUSIONS: Final[dict[str, str]] = {
    "O": "0",
    "o": "0",
    "D": "0",
    "Q": "0",
    "l": "1",
    "I": "1",
    "|": "1",
    "!": "1",
    "S": "5",
    "s": "5",
    "B": "8",
    "Z": "2",
    "z": "2",
    "G": "6",
    "b": "6",
}


def clean_text(raw: str) -> str:
    """Whitespace and Unicode hygiene, with the characters preserved.

    NFKC folds the compatibility forms OCR emits — full-width digits, ligature
    rupee glyphs — onto their canonical equivalents. It does not change which
    script the text is in, so a Devanagari line stays Devanagari.
    """
    if not raw:
        return ""
    text = unicodedata.normalize("NFKC", raw)
    text = text.translate(_DEVANAGARI_DIGITS)
    # Collapse runs of whitespace, including the newlines a multi-line region
    # produces, into single spaces.
    return re.sub(r"\s+", " ", text).strip()


#: Punctuation allowed inside an otherwise-numeric token.
_NUMERIC_PUNCTUATION: Final[frozenset[str]] = frozenset(".,/- ")


def _repair_numeric(token: str) -> str:
    """Fix letter-for-digit confusions inside a token that is otherwise numeric.

    Two conditions, both necessary. The token must contain at least one real
    digit, and every other character must be either a confusable letter or
    numeric punctuation.

    That is stricter than a digit-majority test and deliberately so. A majority
    test rejects "5OO" (one digit of three) which is plainly 500, while
    accepting tokens like "12AB" that are more likely a batch code than a
    price. Requiring the *whole* token to be digit-shaped is what separates
    "5OO" from "SOAP": the latter has no digit to anchor it, so it is left
    alone rather than turned into "50AP" and then into a number.

    A token with no digit at all is never repaired, even when every character
    is confusable. "SOO" could be 500, but it could equally be a brand, and
    inventing a price out of letters is exactly the class of confident wrong
    answer this module exists to avoid.
    """
    if not any(c.isdigit() for c in token):
        return token
    if not all(
        c.isdigit() or c in _NUMERIC_CONFUSIONS or c in _NUMERIC_PUNCTUATION
        for c in token
    ):
        return token
    return "".join(_NUMERIC_CONFUSIONS.get(c, c) for c in token)


def _parse_decimal(text: str) -> float | None:
    """Parse a number, handling the Indian digit grouping.

    ``1,20,000`` and ``120,000`` both mean the same thing and both appear.
    Rather than guess a grouping convention, separators are stripped and the
    result is validated: a comma that was actually a decimal point would
    produce a wildly different magnitude, so a token with a comma followed by
    exactly two digits at the end is rejected as ambiguous rather than
    silently read as thousands.
    """
    token = text.strip()
    if not token:
        return None

    # "120,50" is ambiguous: 12050 under Indian grouping, 120.50 under the
    # European decimal comma. Both conventions appear on imported packaging.
    if re.fullmatch(r"\d{1,3}(?:,\d{3})*,\d{2}", token) or re.fullmatch(
        r"\d+,\d{2}", token
    ):
        return None

    cleaned = token.replace(",", "").replace(" ", "")
    # A trailing "/-" is the standard Indian price terminator.
    cleaned = re.sub(r"/-?$", "", cleaned)
    if not re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

_CURRENCY_MARKERS: Final[tuple[str, ...]] = (
    "₹",  # ₹
    "rs.",
    "rs",
    "inr",
    "rupees",
)

_MRP_LABELS: Final[tuple[str, ...]] = (
    "maximum retail price",
    "max retail price",
    "mrp",
    "m.r.p",
    "अधिकतम खुदरा मूल्य",  # अधिकतम खुदरा मूल्य
)


@dataclass(frozen=True)
class Money:
    amount: float
    currency: str = "INR"

    def to_json(self) -> dict[str, Any]:
        return {"amount": round(self.amount, 2), "currency": self.currency}


def parse_money(text: str) -> Money | None:
    """Extract a monetary amount from a fragment of package text.

    Requires a currency marker. A bare number in an MRP region is not read as
    a price: batch codes, net weights and phone numbers all live near price
    text, and a rule that accepted any number would attribute whichever one
    the region happened to contain.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return None

    lowered = cleaned.lower()
    if not any(marker in lowered for marker in _CURRENCY_MARKERS):
        return None

    # Take the number that follows the currency marker, not merely the first
    # number in the string: "Net 500g MRP Rs 120" must yield 120, not 500.
    pattern = re.compile(
        r"(?:₹|rs\.?|inr|rupees)\s*([0-9OolIS,. /-]+)", re.IGNORECASE
    )
    match = pattern.search(cleaned)
    if match is None:
        return None

    candidate = _repair_numeric(match.group(1).strip())
    # Trim trailing punctuation and any unit that ran on from the next phrase.
    candidate = re.sub(r"[^\d.,/-].*$", "", candidate).strip()
    amount = _parse_decimal(candidate)
    if amount is None or amount <= 0:
        return None
    return Money(amount=amount, currency="INR")


def looks_like_mrp_label(text: str) -> bool:
    """Whether the text carries an MRP label.

    §5 is explicit that "price strings elsewhere on package are not
    necessarily MRP", so attribution requires the label and not merely a
    currency symbol.
    """
    lowered = clean_text(text).lower()
    return any(label in lowered for label in _MRP_LABELS)


def mentions_inclusive_of_taxes(text: str) -> bool:
    """Whether the text states the price is inclusive of taxes.

    Captured as evidence because Rule 6 requires MRP to be inclusive of all
    taxes and the wording's presence is an observation Team 2 may want. Whether
    its absence matters is their call, not this module's.
    """
    lowered = clean_text(text).lower()
    # The period after the abbreviation is the common printed form —
    # "(incl. of all taxes)" — so it has to be allowed between the word and
    # the "of", not merely tolerated at the end.
    return bool(re.search(r"incl\w*\.?\s*of\s+all\s+tax", lowered))


# ---------------------------------------------------------------------------
# Quantity
# ---------------------------------------------------------------------------

#: Canonical unit -> the spellings that map onto it.
#:
#: Only SI mass/volume/length and 'N' (number of units) appear, because those
#: are what the Packaged Commodities Rules use for net quantity declarations.
_UNIT_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "g": ("g", "gm", "gms", "gram", "grams", "ग्राम"),
    "kg": ("kg", "kgs", "kilogram", "kilograms", "किग्रा"),
    "mg": ("mg", "milligram", "milligrams"),
    "ml": ("ml", "mls", "millilitre", "milliliter", "मिली"),
    "l": ("l", "ltr", "ltrs", "litre", "liter", "litres", "liters"),
    "cm": ("cm", "centimetre", "centimeter"),
    "mm": ("mm", "millimetre", "millimeter"),
    "m": ("m", "metre", "meter"),
    "N": ("n", "no", "nos", "pcs", "pieces", "units", "u"),
}

_UNIT_LOOKUP: Final[dict[str, str]] = {
    alias: canonical
    for canonical, aliases in _UNIT_ALIASES.items()
    for alias in aliases
}

_NET_QUANTITY_LABELS: Final[tuple[str, ...]] = (
    "net quantity",
    "net qty",
    "net wt",
    "net weight",
    "net content",
    "net contents",
    "शुद्ध मात्रा",  # शुद्ध मात्रा
)


@dataclass(frozen=True)
class Quantity:
    value: float
    unit: str

    def to_json(self) -> dict[str, Any]:
        return {"value": self.value, "unit": self.unit}


def parse_quantity(text: str) -> Quantity | None:
    """Extract a net-quantity declaration.

    Returns the unit as printed rather than converting to a base unit. §5
    wants the declaration observed; ``500 g`` and ``0.5 kg`` are different
    declarations even though they are the same mass, and the compliance engine
    may care which one the package actually carries. Conversion is available
    separately through :func:`to_base_unit` for the resolver's value
    comparison.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return None

    # Number immediately followed by a unit word, optionally separated.
    pattern = re.compile(
        r"(?<![\d.])([0-9][0-9OolIS,.]*)\s*"
        r"([a-zA-Zऀ-ॿ]{1,12})\.?(?![a-zA-Zऀ-ॿ])"
    )

    for match in pattern.finditer(cleaned):
        raw_number, raw_unit = match.group(1), match.group(2)
        unit = _UNIT_LOOKUP.get(raw_unit.lower().strip("."))
        if unit is None:
            continue
        value = _parse_decimal(_repair_numeric(raw_number))
        if value is None or value <= 0:
            continue
        return Quantity(value=value, unit=unit)

    return None


def looks_like_net_quantity_label(text: str) -> bool:
    lowered = clean_text(text).lower()
    return any(label in lowered for label in _NET_QUANTITY_LABELS)


#: Multipliers onto a base unit, per dimension. Used only for comparison.
_BASE_FACTORS: Final[dict[str, tuple[str, float]]] = {
    "mg": ("g", 0.001),
    "g": ("g", 1.0),
    "kg": ("g", 1000.0),
    "ml": ("ml", 1.0),
    "l": ("ml", 1000.0),
    "mm": ("mm", 1.0),
    "cm": ("mm", 10.0),
    "m": ("mm", 1000.0),
    "N": ("N", 1.0),
}


def to_base_unit(quantity: Quantity) -> tuple[str, float] | None:
    """``(dimension, magnitude)`` for comparing two quantities.

    Lets the resolver see that ``0.5 kg`` on the front and ``500 g`` on the
    back are the same declaration rather than a conflict — while the facts
    still record what each panel actually printed.
    """
    entry = _BASE_FACTORS.get(quantity.unit)
    if entry is None:
        return None
    dimension, factor = entry
    return dimension, quantity.value * factor


# ---------------------------------------------------------------------------
# Unit sale price
# ---------------------------------------------------------------------------

_UNIT_PRICE_LABELS: Final[tuple[str, ...]] = (
    "unit sale price",
    "unit price",
    "price per",
)


@dataclass(frozen=True)
class UnitSalePrice:
    amount: float
    currency: str
    per_value: float
    per_unit: str

    def to_json(self) -> dict[str, Any]:
        return {
            "amount": round(self.amount, 4),
            "currency": self.currency,
            "per": {"value": self.per_value, "unit": self.per_unit},
        }


def parse_unit_sale_price(text: str) -> UnitSalePrice | None:
    """Extract a printed unit-sale-price declaration.

    §8 closed this question: unit sale price is in the canonical model, and an
    explicitly printed declaration must be captured as OBSERVED. A computed
    figure is a different thing — see :func:`derive_unit_sale_price` — and
    must never replace what the package prints.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return None

    money = parse_money(cleaned)
    if money is None:
        return None

    # The "per <qty> <unit>" tail. "Rs 0.24 per g" and "Rs 12 / 100 g" are
    # both ordinary; the quantity defaults to 1 when only a unit is given.
    tail = re.search(
        r"(?:per|/|\bpr\b)\s*([0-9][0-9.,]*)?\s*"
        r"([a-zA-Zऀ-ॿ]{1,12})\.?(?![a-zA-Zऀ-ॿ])",
        cleaned,
        re.IGNORECASE,
    )
    if tail is None:
        return None

    unit = _UNIT_LOOKUP.get(tail.group(2).lower().strip("."))
    if unit is None:
        return None

    per_value = 1.0
    if tail.group(1):
        parsed = _parse_decimal(tail.group(1))
        if parsed is None or parsed <= 0:
            return None
        per_value = parsed

    return UnitSalePrice(
        amount=money.amount,
        currency=money.currency,
        per_value=per_value,
        per_unit=unit,
    )


def looks_like_unit_price_label(text: str) -> bool:
    lowered = clean_text(text).lower()
    return any(label in lowered for label in _UNIT_PRICE_LABELS)


def derive_unit_sale_price(
    mrp: Money, net_quantity: Quantity
) -> UnitSalePrice | None:
    """Compute a unit price from an observed MRP and net quantity.

    The result is DERIVED, never OBSERVED, and the caller must not emit it
    when a printed declaration exists: §8 forbids replacing an observed
    declaration with an independently calculated value. It exists because the
    contract includes the field and a package that prints MRP and quantity but
    not unit price still supports a traceable derivation.

    Returns None for units where a per-unit price is not meaningful — a price
    "per 1 N" of a single-item pack restates the MRP and adds nothing.
    """
    base = to_base_unit(net_quantity)
    if base is None:
        return None
    dimension, magnitude = base
    if dimension == "N" or magnitude <= 0:
        return None

    return UnitSalePrice(
        amount=mrp.amount / magnitude,
        currency=mrp.currency,
        per_value=1.0,
        per_unit=dimension,
    )


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_MONTHS: Final[dict[str, int]] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_MANUFACTURE_LABELS: Final[tuple[str, ...]] = (
    "date of manufacture", "mfg date", "mfg", "manufactured on",
    "date of packing", "packed on", "pkd", "date of import",
)

_BEST_BEFORE_LABELS: Final[tuple[str, ...]] = (
    "best before", "best before use", "use by", "expiry", "exp date", "exp",
    "consume before",
)


@dataclass(frozen=True)
class PackageDate:
    """A date declaration, kept at the precision the package printed it.

    ``YYYY-MM`` is the common case: Rule 6 asks for the month and year of
    manufacture, and most packages print exactly that. Rendering it as
    ``YYYY-MM-01`` would invent a day that is not on the package, so the
    precision is carried explicitly and the ISO string is truncated to match.
    """

    year: int
    month: int
    day: int | None = None

    @property
    def precision(self) -> str:
        return "day" if self.day is not None else "month"

    def to_iso(self) -> str:
        if self.day is not None:
            return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
        return f"{self.year:04d}-{self.month:02d}"

    def to_json(self) -> str:
        return self.to_iso()


def _normalise_year(raw: int) -> int | None:
    """Expand a two-digit year, refusing anything implausible.

    A two-digit year on packaging is this century. The upper bound rejects
    OCR noise — a "year" of 2199 is a misread batch code, not a date — and the
    lower bound predates any package still on a shelf.
    """
    if 0 <= raw <= 99:
        return 2000 + raw
    if 1990 <= raw <= 2100:
        return raw
    return None


def parse_date(text: str) -> PackageDate | None:
    """Extract a date declaration.

    Ambiguous all-numeric dates are refused. ``03/04/2026`` is 3 April under
    the Indian convention and 4 March under the American one, and packaging in
    an Indian shop can be either — imported goods carry their origin's
    convention. §5 warns against "interpreting unrelated numbers as dates";
    reading one wrong by a month is the same class of error. Where the day is
    above 12 the reading is unambiguous and is accepted.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return None

    # 1. "MM/YYYY" or "MM-YY" — month and year only, the common case.
    match = re.search(r"(?<!\d)(\d{1,2})\s*[/\-.]\s*(\d{2,4})(?!\d)", cleaned)
    alpha = re.search(
        r"(?<![a-zA-Z])([a-zA-Z]{3,9})\s*[,/\-. ]\s*(\d{2,4})(?!\d)", cleaned
    )
    numeric_full = re.search(
        r"(?<!\d)(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{2,4})(?!\d)",
        cleaned,
    )

    # 2. "DD MMM YYYY" / "MMM YYYY" — unambiguous because the month is named.
    day_alpha = re.search(
        r"(?<!\d)(\d{1,2})\s*[/\-. ]\s*([a-zA-Z]{3,9})\s*[,/\-. ]\s*(\d{2,4})(?!\d)",
        cleaned,
    )
    if day_alpha:
        month = _MONTHS.get(day_alpha.group(2).lower())
        year = _normalise_year(int(day_alpha.group(3)))
        day = int(day_alpha.group(1))
        if month and year and 1 <= day <= 31:
            if _is_real_date(year, month, day):
                return PackageDate(year=year, month=month, day=day)
        return None

    if numeric_full:
        a, b = int(numeric_full.group(1)), int(numeric_full.group(2))
        year = _normalise_year(int(numeric_full.group(3)))
        if year is None:
            return None
        # Only accept when one of the two can only be the day.
        if a > 12 and b <= 12 and _is_real_date(year, b, a):
            return PackageDate(year=year, month=b, day=a)
        if b > 12 and a <= 12 and _is_real_date(year, a, b):
            return PackageDate(year=year, month=a, day=b)
        # Both <= 12: genuinely ambiguous. Decline rather than guess.
        return None

    if alpha:
        month = _MONTHS.get(alpha.group(1).lower())
        year = _normalise_year(int(alpha.group(2)))
        if month and year:
            return PackageDate(year=year, month=month)
        # The alpha token was not a month name. That is the ordinary case for
        # "MFG 07/2026", where the label itself matches the alpha pattern and
        # "07" looks like a year. Fall through to the numeric form rather than
        # concluding there is no date here — returning None at this point made
        # every labelled month/year declaration unparseable.

    if match:
        month = int(match.group(1))
        year = _normalise_year(int(match.group(2)))
        if year and 1 <= month <= 12:
            return PackageDate(year=year, month=month)
        return None

    return None


def _is_real_date(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
        return True
    except ValueError:
        return False


def looks_like_manufacture_label(text: str) -> bool:
    lowered = clean_text(text).lower()
    return any(label in lowered for label in _MANUFACTURE_LABELS)


def looks_like_best_before_label(text: str) -> bool:
    lowered = clean_text(text).lower()
    return any(label in lowered for label in _BEST_BEFORE_LABELS)


def parse_best_before_duration(text: str) -> int | None:
    """Months, for the "Best before N months from packaging" form.

    Returned as a duration rather than resolved into a date. Resolving it
    would require the manufacture date, making the result depend on another
    extracted fact — a derivation, with its own trace, which belongs in the
    resolver rather than hidden inside a text parser.
    """
    cleaned = clean_text(text).lower()
    match = re.search(r"(\d{1,3})\s*months?\b", cleaned)
    if match is None:
        return None
    months = int(match.group(1))
    return months if 1 <= months <= 120 else None


# ---------------------------------------------------------------------------
# Country of origin, names and addresses
# ---------------------------------------------------------------------------

_ORIGIN_LABELS: Final[tuple[str, ...]] = (
    "country of origin",
    "made in",
    "product of",
    "origin",
    "उत्पत्ति का देश",
)


def parse_country_of_origin(text: str) -> str | None:
    """The country named after an origin label.

    Returns the country as printed rather than an ISO code. §5 asks for "the
    exact phrase and country name"; mapping "Made in P.R.C." onto "CN" is an
    interpretation, and one the compliance engine is better placed to make
    with the full phrase in front of it.
    """
    cleaned = clean_text(text)
    lowered = cleaned.lower()

    for label in _ORIGIN_LABELS:
        index = lowered.find(label)
        if index < 0:
            continue
        tail = cleaned[index + len(label):].lstrip(" :–-—,")
        # Split on separators only. A period is deliberately NOT one: country
        # names are routinely printed as abbreviations — "P.R.C.", "U.S.A." —
        # and splitting on the first period truncates them to a single letter,
        # which then fails the length check and loses the declaration entirely.
        country = re.split(r"[,;|\n]|\s{2,}", tail, maxsplit=1)[0].strip()
        # Trim a run-on into the next declaration on the same line.
        country = re.sub(
            r"\s+(?:mrp|net|mfg|best|packed|marketed|manufactured|use|exp).*$",
            "",
            country,
            flags=re.IGNORECASE,
        ).strip()
        # A trailing sentence period is punctuation, not part of the name;
        # an internal one (P.R.C) is.
        country = country.rstrip(" .")
        if 2 <= len(country) <= 56 and re.search(r"[a-zA-Zऀ-ॿ]", country):
            return country
    return None


_ROLE_PATTERNS: Final[dict[str, tuple[str, ...]]] = {
    "manufacturer": ("manufactured by", "mfd by", "mfg by", "manufacturer"),
    "packer": ("packed by", "packer", "repacked by"),
    "importer": ("imported by", "importer"),
    "marketer": ("marketed by", "marketer"),
}


@dataclass(frozen=True)
class PartyDeclaration:
    """A named party and its address, with the role the package assigned it.

    §5 wants the role label captured, not just the name: "Marketed by" and
    "Manufactured by" carry different obligations and a pipeline that flattened
    both into "the company on the pack" would destroy the distinction before
    Team 2 ever saw it.
    """

    role: str
    name: str
    address: str

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "address": self.address}


def parse_party(text: str) -> PartyDeclaration | None:
    """Split a "<role> by <name>, <address>" declaration.

    The name/address split is heuristic and deliberately crude: the first
    comma-delimited segment is taken as the name and the remainder as the
    address. Indian package addresses have no reliable structure, and a
    cleverer parser would fail in ways that are harder to notice than this
    one's. The raw text is preserved on the observation regardless, so a
    reviewer always has the printed form.
    """
    cleaned = clean_text(text)
    lowered = cleaned.lower()

    for role, patterns in _ROLE_PATTERNS.items():
        for pattern in patterns:
            index = lowered.find(pattern)
            if index < 0:
                continue
            tail = cleaned[index + len(pattern):].lstrip(" :–-—.,")
            if not tail:
                continue
            parts = [p.strip() for p in tail.split(",", 1)]
            name = parts[0]
            address = parts[1].strip() if len(parts) > 1 else ""
            if len(name) < 2:
                continue
            return PartyDeclaration(role=role, name=name, address=address)
    return None


_CONSUMER_CARE_LABELS: Final[tuple[str, ...]] = (
    "consumer care",
    "customer care",
    "customer service",
    "for complaints",
    "consumer complaints",
    "grievance",
)

_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)

#: Indian phone numbers as printed: optional +91, optional 0 trunk prefix,
#: 10 digits for mobiles, and the 1800 toll-free block.
_PHONE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:\+91[\-\s]?)?(?:1800[\-\s]?\d{3}[\-\s]?\d{3,4}|0?\d{10}|\d{3,5}[\-\s]\d{6,8})"
)


@dataclass(frozen=True)
class ConsumerCare:
    text: str
    email: str | None = None
    phone: str | None = None

    def to_json(self) -> str:
        # The contract types consumer_care as a string, so the structured
        # parts are kept on the observation and the printed text is what
        # projects. Widening the contract is Team 2's call, not ours.
        return self.text


def looks_like_consumer_care_label(text: str) -> bool:
    lowered = clean_text(text).lower()
    return any(label in lowered for label in _CONSUMER_CARE_LABELS)


def parse_consumer_care(text: str) -> ConsumerCare | None:
    cleaned = clean_text(text)
    if not looks_like_consumer_care_label(cleaned):
        return None

    email_match = _EMAIL_RE.search(cleaned)
    phone_match = _PHONE_RE.search(cleaned)
    if email_match is None and phone_match is None:
        # A "Consumer care" heading with no contact details underneath it is a
        # region that was cropped, not a declaration.
        return None

    return ConsumerCare(
        text=cleaned,
        email=email_match.group(0) if email_match else None,
        phone=phone_match.group(0).strip() if phone_match else None,
    )


_GENERIC_NAME_LABELS: Final[tuple[str, ...]] = (
    "generic name",
    "common name",
    "name of commodity",
    "commodity",
)


def parse_generic_name(text: str) -> str | None:
    """The commodity's generic name, when the package labels one.

    Only taken from an explicit label. §5 requires distinguishing commodity
    identity from branding, and the largest text on the front panel is the
    brand far more often than it is the generic name — so inferring one from
    prominence would be wrong most of the time.
    """
    cleaned = clean_text(text)
    lowered = cleaned.lower()

    for label in _GENERIC_NAME_LABELS:
        index = lowered.find(label)
        if index < 0:
            continue
        tail = cleaned[index + len(label):].lstrip(" :–-—.,")
        name = re.split(r"[.,;|\n]|\s{2,}", tail, maxsplit=1)[0].strip()
        if 2 <= len(name) <= 80:
            return name
    return None


_NOT_FOR_RETAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"not\s+for\s+retail\s+sale", re.IGNORECASE
)


def mentions_not_for_retail_sale(text: str) -> bool:
    """Whether the package carries a "not for retail sale" statement.

    Recorded as an observation and nothing more. §5.1 is explicit that this
    statement must not be turned into a blanket set of DECLARED_ABSENCE values
    — whether it changes what is required is an applicability question and
    therefore Team 2's.
    """
    return bool(_NOT_FOR_RETAIL_RE.search(clean_text(text)))
