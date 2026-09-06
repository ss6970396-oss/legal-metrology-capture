"""Normalization rules.

§17 benchmarks normalization by exact match, so these are exact-match tests.
The cases that matter most are the ones where a parser should **decline**: a
declined parse becomes UNKNOWN and a person looks at the photograph, while a
wrong parse becomes an OBSERVED fact with provenance and looks trustworthy.
"""

from __future__ import annotations

import pytest

from app.pipeline import normalize as nz


class TestMoney:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("MRP Rs. 120", 120.0),
            ("M.R.P. ₹ 120.50", 120.50),
            ("MRP INR 1,20,000", 120000.0),
            ("Maximum Retail Price Rs 45/-", 45.0),
            ("mrp rs.99", 99.0),
            ("Rs 1,250", 1250.0),
        ],
    )
    def test_parses_indian_price_forms(self, text: str, expected: float) -> None:
        money = nz.parse_money(text)
        assert money is not None
        assert money.amount == pytest.approx(expected)
        assert money.currency == "INR"

    def test_requires_a_currency_marker(self) -> None:
        """A bare number near price text is not a price. Batch codes and net
        weights live in the same region."""
        assert nz.parse_money("MRP 120") is None
        assert nz.parse_money("120") is None

    def test_takes_the_number_after_the_currency_marker(self) -> None:
        """'Net 500g MRP Rs 120' must yield 120, not 500."""
        money = nz.parse_money("Net 500g MRP Rs 120")
        assert money is not None
        assert money.amount == pytest.approx(120.0)

    def test_declines_an_ambiguous_decimal_comma(self) -> None:
        """'120,50' is 12050 under Indian grouping and 120.50 under the
        European decimal comma. Both appear on packaging here."""
        assert nz.parse_money("MRP Rs 120,50") is None

    def test_repairs_letter_for_digit_confusion_in_a_numeric_run(self) -> None:
        money = nz.parse_money("MRP Rs. 5OO")
        assert money is not None
        assert money.amount == pytest.approx(500.0)

    def test_detects_the_inclusive_of_taxes_wording(self) -> None:
        assert nz.mentions_inclusive_of_taxes("MRP Rs 120 (incl. of all taxes)")
        assert nz.mentions_inclusive_of_taxes("Inclusive of all taxes")
        assert not nz.mentions_inclusive_of_taxes("MRP Rs 120")

    def test_recognises_mrp_labels_including_devanagari(self) -> None:
        assert nz.looks_like_mrp_label("M.R.P. Rs 120")
        assert nz.looks_like_mrp_label("Maximum Retail Price")
        assert nz.looks_like_mrp_label("अधिकतम खुदरा मूल्य 120")
        assert not nz.looks_like_mrp_label("Price on request")


class TestQuantity:
    @pytest.mark.parametrize(
        "text,value,unit",
        [
            ("Net Quantity: 500 g", 500.0, "g"),
            ("Net Wt. 1.5 kg", 1.5, "kg"),
            ("Net Qty 250ml", 250.0, "ml"),
            ("Net Contents: 2 L", 2.0, "l"),
            ("Net Quantity 12 N", 12.0, "N"),
            ("शुद्ध मात्रा 500 ग्राम", 500.0, "g"),
        ],
    )
    def test_parses_declarations(self, text: str, value: float, unit: str) -> None:
        quantity = nz.parse_quantity(text)
        assert quantity is not None
        assert quantity.value == pytest.approx(value)
        assert quantity.unit == unit

    def test_keeps_the_unit_as_printed(self) -> None:
        """§5 wants the declaration observed. 500 g and 0.5 kg are different
        declarations of the same mass."""
        assert nz.parse_quantity("Net Wt 0.5 kg").unit == "kg"
        assert nz.parse_quantity("Net Wt 500 g").unit == "g"

    def test_converts_to_a_base_unit_for_comparison(self) -> None:
        half_kg = nz.to_base_unit(nz.Quantity(0.5, "kg"))
        five_hundred_g = nz.to_base_unit(nz.Quantity(500, "g"))
        assert half_kg == five_hundred_g == ("g", 500.0)

    def test_devanagari_digits_are_folded(self) -> None:
        quantity = nz.parse_quantity("Net Quantity ५०० g")
        assert quantity is not None
        assert quantity.value == pytest.approx(500.0)

    def test_ignores_an_unrecognised_unit(self) -> None:
        assert nz.parse_quantity("Batch 500 XYZ") is None


class TestDates:
    @pytest.mark.parametrize(
        "text,iso",
        [
            ("MFG 07/2026", "2026-07"),
            ("Mfg. Date: Jul 2026", "2026-07"),
            ("Best Before: 01/2027", "2027-01"),
            ("Packed on 14 Jul 2026", "2026-07-14"),
            ("MFG 07/26", "2026-07"),
        ],
    )
    def test_parses_unambiguous_dates(self, text: str, iso: str) -> None:
        parsed = nz.parse_date(text)
        assert parsed is not None
        assert parsed.to_iso() == iso

    def test_declines_an_ambiguous_all_numeric_date(self) -> None:
        """03/04/2026 is 3 April here and 4 March in the US, and imported
        packaging carries its origin's convention."""
        assert nz.parse_date("03/04/2026") is None

    def test_accepts_an_all_numeric_date_when_the_day_is_unambiguous(self) -> None:
        parsed = nz.parse_date("25/04/2026")
        assert parsed is not None
        assert parsed.to_iso() == "2026-04-25"

    def test_month_precision_does_not_invent_a_day(self) -> None:
        parsed = nz.parse_date("MFG 07/2026")
        assert parsed.precision == "month"
        assert parsed.to_iso() == "2026-07"  # not 2026-07-01

    def test_rejects_an_impossible_date(self) -> None:
        assert nz.parse_date("31/02/2026") is None

    def test_rejects_an_implausible_year(self) -> None:
        assert nz.parse_date("07/2199") is None

    def test_parses_a_best_before_duration_without_resolving_it(self) -> None:
        """Resolving 'best before 9 months' needs the manufacture date, which
        makes it a derivation with its own trace, not a text parse."""
        assert nz.parse_best_before_duration("Best before 9 months from packaging") == 9
        assert nz.parse_best_before_duration("Best before 01/2027") is None

    def test_distinguishes_the_two_date_labels(self) -> None:
        assert nz.looks_like_manufacture_label("Mfg Date 07/2026")
        assert nz.looks_like_best_before_label("Best Before 01/2027")
        assert not nz.looks_like_best_before_label("Mfg Date 07/2026")


class TestUnitSalePrice:
    @pytest.mark.parametrize(
        "text,amount,per_value,per_unit",
        [
            ("Unit Sale Price: Rs 0.24 per g", 0.24, 1.0, "g"),
            ("Unit price Rs 12 / 100 g", 12.0, 100.0, "g"),
            ("Price per ml: Rs 0.5", 0.5, 1.0, "ml"),
        ],
    )
    def test_parses_printed_declarations(
        self, text: str, amount: float, per_value: float, per_unit: str
    ) -> None:
        price = nz.parse_unit_sale_price(text)
        assert price is not None
        assert price.amount == pytest.approx(amount)
        assert price.per_value == pytest.approx(per_value)
        assert price.per_unit == per_unit

    def test_derives_from_mrp_and_quantity(self) -> None:
        derived = nz.derive_unit_sale_price(
            nz.Money(120.0), nz.Quantity(500, "g")
        )
        assert derived is not None
        assert derived.amount == pytest.approx(0.24)
        assert derived.per_unit == "g"

    def test_declines_to_derive_a_per_item_price(self) -> None:
        """A price 'per 1 N' of a single-item pack restates the MRP."""
        assert nz.derive_unit_sale_price(nz.Money(120.0), nz.Quantity(1, "N")) is None


class TestParties:
    def test_captures_the_role_label(self) -> None:
        """§5 wants the role, not just the name: 'Marketed by' and
        'Manufactured by' carry different obligations."""
        party = nz.parse_party("Manufactured by: Acme Foods Ltd, Pune 411001")
        assert party is not None
        assert party.role == "manufacturer"
        assert party.name == "Acme Foods Ltd"
        assert "Pune" in party.address

    @pytest.mark.parametrize(
        "text,role",
        [
            ("Packed by Beta Packers, Nashik", "packer"),
            ("Imported by Gamma Imports Pvt Ltd, Mumbai", "importer"),
            ("Marketed by Delta Marketing, Delhi", "marketer"),
        ],
    )
    def test_recognises_each_role(self, text: str, role: str) -> None:
        party = nz.parse_party(text)
        assert party is not None
        assert party.role == role

    def test_handles_a_name_with_no_address(self) -> None:
        party = nz.parse_party("Manufactured by Acme Foods Ltd")
        assert party is not None
        assert party.name == "Acme Foods Ltd"
        assert party.address == ""


class TestCountryOfOrigin:
    @pytest.mark.parametrize(
        "text,country",
        [
            ("Country of Origin: India", "India"),
            ("Made in Germany", "Germany"),
            ("Product of Sri Lanka", "Sri Lanka"),
        ],
    )
    def test_extracts_the_country(self, text: str, country: str) -> None:
        assert nz.parse_country_of_origin(text) == country

    def test_returns_the_printed_phrase_not_an_iso_code(self) -> None:
        """§5 asks for the exact country name. Mapping 'P.R.C.' onto 'CN' is
        an interpretation and the compliance engine is better placed to make
        it with the full phrase in view."""
        assert nz.parse_country_of_origin("Made in P.R.C.") == "P.R.C"

    def test_stops_before_the_next_declaration(self) -> None:
        assert (
            nz.parse_country_of_origin("Country of Origin: India MRP Rs 120")
            == "India"
        )

    def test_returns_none_without_a_label(self) -> None:
        assert nz.parse_country_of_origin("India Gate Basmati Rice") is None


class TestConsumerCare:
    def test_requires_a_contact_detail(self) -> None:
        """A heading with nothing under it is a cropped region, not a
        declaration."""
        assert nz.parse_consumer_care("Consumer Care:") is None

    def test_extracts_email_and_phone(self) -> None:
        care = nz.parse_consumer_care(
            "Consumer Care: care@acme.example, 1800 123 4567"
        )
        assert care is not None
        assert care.email == "care@acme.example"
        assert care.phone is not None


class TestGenericName:
    def test_requires_an_explicit_label(self) -> None:
        """The largest text on the front panel is the brand far more often
        than it is the generic name."""
        assert nz.parse_generic_name("ACME GOLD") is None
        assert nz.parse_generic_name("Generic Name: Refined Sunflower Oil") == (
            "Refined Sunflower Oil"
        )


class TestNotForRetailSale:
    def test_is_observed_as_a_statement(self) -> None:
        """§5.1: recorded as an observation, never converted into a set of
        DECLARED_ABSENCE values."""
        assert nz.mentions_not_for_retail_sale("NOT FOR RETAIL SALE")
        assert nz.mentions_not_for_retail_sale("Not for retail sale")
        assert not nz.mentions_not_for_retail_sale("For retail sale")


class TestTextHygiene:
    def test_collapses_whitespace_and_folds_compatibility_forms(self) -> None:
        assert nz.clean_text("  MRP\n\nRs.  120  ") == "MRP Rs. 120"

    def test_preserves_devanagari_script(self) -> None:
        assert "शुद्ध" in nz.clean_text("शुद्ध मात्रा")

    def test_does_not_corrupt_words_with_digit_substitutions(self) -> None:
        """'SOAP' must not become '5OAP' and then a number."""
        assert nz._repair_numeric("SOAP") == "SOAP"
        assert nz._repair_numeric("5OO") == "500"
