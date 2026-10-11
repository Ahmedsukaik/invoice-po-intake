"""Parsing of single values: strict, and never a half-valid result."""
import pytest

from intake.values import (
    amount_in_text,
    clean_text,
    exact_number_key,
    iban_is_valid,
    name_key,
    normalize,
    normalize_line,
    number_key,
    parse_printed_amount,
    parse_printed_date,
    uk_details_from_iban,
)


@pytest.mark.parametrize("raw, expected", [
    ("1,500.00", "1500.00"), ("1500", "1500.00"), ("0.5", "0.50"), (" 12.30 ", "12.30"), ("1,234,567.89", "1234567.89"),
])
def test_amounts_are_normalised(raw, expected):
    assert normalize("amount", raw) == expected


@pytest.mark.parametrize("raw", [
    "1.234,50",      # European decimal comma: ambiguous, refused rather than guessed
    "12,34",         # not a thousands group
    "-10.00",        # credit notes are out of scope
    "10.001",        # three decimals on an amount
    "GBP 10.00", "£10.00", "10 EUR", "1e5", "NaN", "Infinity", "0x10", "١٢٣",
    "99999999999.00",
])
def test_bad_amounts_are_refused(raw):
    with pytest.raises(ValueError):
        normalize("amount", raw, "Total")


def test_prices_keep_up_to_four_decimals_and_quantities_must_be_above_zero():
    assert normalize("price", "0.8525") == "0.8525"
    assert normalize("price", "4.5") == "4.50"
    assert normalize("quantity", "200") == "200" and normalize("quantity", "2.500") == "2.5"
    with pytest.raises(ValueError, match="above zero"):
        normalize("quantity", "0")
    with pytest.raises(ValueError):
        normalize("price", "1.12345")


@pytest.mark.parametrize("raw", ["2026-02-30", "05/10/2026", "2026-1-5", "20261005", "1999-12-31", "2026-10-05T00:00"])
def test_dates_must_be_real_iso_dates(raw):
    with pytest.raises(ValueError):
        normalize("date", raw, "Invoice date")


def test_iban_check_digits():
    assert iban_is_valid(normalize("iban", "GB77 DEMO 4000 0110 0000 01"))
    assert not iban_is_valid(normalize("iban", "GB78 DEMO 4000 0110 0000 01"))  # one digit changed
    assert not iban_is_valid("GB77DEMO")
    with pytest.raises(ValueError):
        normalize("iban", "not an iban at all")


def test_text_is_one_clean_line_without_control_or_bidi_characters():
    hostile = "Acme‮ Demo\x00\r\nLtd​\t"
    assert clean_text(hostile) == "Acme Demo Ltd"
    assert normalize("text", "  ") is None and normalize("text", None) is None


def test_references_and_enums_are_strict():
    assert normalize("ref", "PO-2026-0101") == "PO-2026-0101"
    for bad in ("PO 2026", "../etc", "<script>", "A" * 41):
        with pytest.raises(ValueError):
            normalize("ref", bad)
    assert normalize("doc_type", "Purchase Order") == "purchase_order"
    with pytest.raises(ValueError):
        normalize("doc_type", "credit_note")
    with pytest.raises(ValueError):
        normalize("amount", 12.5)  # floats are refused: money arrives as text


def test_invoice_numbers_that_look_alike_count_as_the_same_number():
    same = ["INV-A-1001", "inv a 1001", "INV-A-01001", "INV-A-1OO1", "lNV-A-1001", "INV/A/0001001"]
    assert len({number_key(n) for n in same}) == 1
    assert number_key("INV-A-1001") != number_key("INV-A-1002")
    # zeros are dropped inside each part of the number, not across its separators
    assert number_key("INV-2026-0001") == number_key("INV-2026-001") == number_key("inv 2026/1")
    assert number_key("2026/0042") == number_key("2026/42")
    assert number_key("INV-10-01") != number_key("INV-1001")
    assert name_key("ACME  Demo Components, Ltd.") == name_key("acme demo components ltd")
    assert name_key("Smith & Co") == name_key("Smith and Co")


def test_only_numbers_printed_the_same_way_count_as_exactly_the_same():
    assert exact_number_key("INV-A-1001") == exact_number_key("inv a/1001")
    for a, b in [("INV-11", "INV-L1"), ("SO-1001", "S0-1001"), ("INV-0042", "INV-42"), ("INV-10-01", "INV-1001")]:
        assert exact_number_key(a) != exact_number_key(b)


@pytest.mark.parametrize("printed, expected", [
    ("£1,500.00", "1500.00"), ("1.500,00 EUR", "1500.00"), ("GBP 1 500.00", "1500.00"), ("12,50", "12.50"),
    ("1,500", "1500.00"), ("1.500.000,25", "1500000.25"),
])
def test_printed_amounts_are_read_in_code(printed, expected):
    assert parse_printed_amount(printed, "amount", "Total") == expected


def test_a_comma_and_three_digits_is_read_uk_style_unless_it_cannot_be_thousands():
    assert parse_printed_amount("1,500", "quantity", "Qty") == "1500"       # UK style: thousands
    assert parse_printed_amount("0,125", "price", "Unit price") == "0.125"  # a thousands group never starts with 0
    assert parse_printed_amount("0.125", "price", "Unit price") == "0.125"
    assert parse_printed_amount("1234,567", "quantity", "Qty") == "1234.567"  # 1234 is not a thousands group
    with pytest.raises(ValueError, match="two ways"):
        parse_printed_amount("1.234", "price", "Unit price")             # 1.234 or 1234: refused


def test_an_amount_must_appear_as_a_number_in_its_source_line():
    assert amount_in_text("1800.00", "Total due 1,800.00", "amount")
    assert amount_in_text("1500.00", "Summe 1.500,00 EUR", "amount")
    assert not amount_in_text("1800.00", "Total 18", "amount")          # the digits 18 are not 1800.00
    assert not amount_in_text("1800.00", "Total 180.00", "amount")
    assert not amount_in_text("1800.00", "Ref 18000 paid", "amount")


@pytest.mark.parametrize("printed", ["1.500", "-12.00", "(12.00)", "12,5,0", "about 12"])
def test_printed_amounts_that_could_mean_two_things_are_refused(printed):
    with pytest.raises(ValueError):
        parse_printed_amount(printed, "amount", "Total")


def test_printed_dates_and_ambiguous_ones():
    assert parse_printed_date("25/10/2026") == ("2026-10-25", False)
    assert parse_printed_date("10/25/2026") == ("2026-10-25", False)
    assert parse_printed_date("5 October 2026") == ("2026-10-05", False)
    assert parse_printed_date("Oct 5, 2026") == ("2026-10-05", False)
    assert parse_printed_date("05.10.2026") == ("2026-10-05", False)  # dotted dates are day first
    assert parse_printed_date("05/10/2026") == ("2026-10-05", True)
    assert parse_printed_date("05/10/2026", order="mdy") == ("2026-05-10", True)
    for bad in ("31/02/2026", "next Tuesday", "2026-13-01"):
        with pytest.raises(ValueError):
            parse_printed_date(bad)


def test_uk_bank_details():
    assert normalize("sortcode", "40 00 01") == "40-00-01" and normalize("account", "1000 0001") == "10000001"
    with pytest.raises(ValueError):
        normalize("sortcode", "40-00")
    assert uk_details_from_iban("GB77DEMO40000110000001") == ("40-00-01", "10000001")
    assert uk_details_from_iban("DE89370400440532013000") is None


def test_line_items_reject_unknown_cells():
    assert normalize_line({"sku": "AC-100", "quantity": "2", "unit_price": "1.50", "line_total": "3.00"})["sku"] == "AC-100"
    with pytest.raises(ValueError, match="unknown line field"):
        normalize_line({"sku": "AC-100", "discount": "5"})
