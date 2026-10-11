"""The checks, one rule at a time, on hand-built documents and the seed vendor master / PO register."""
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import TODAY, set_status, stored_doc

from intake.checks import run_checks

ACME_IBAN = "GB77DEMO40000110000001"
LINES = [{"sku": "AC-100", "description": "Steel bracket, 40 mm", "quantity": "200", "unit_price": "4.50", "line_total": "900.00"},
         {"sku": "AC-220", "description": "Hinge set, zinc", "quantity": "50", "unit_price": "12.00", "line_total": "600.00"}]


def invoice(**fields) -> dict:
    lines = fields.pop("lines", LINES)
    base = {"doc_type": "invoice", "vendor_name": "Acme Demo Components Ltd", "invoice_number": "INV-A-1001",
            "invoice_date": "2026-10-05", "due_date": "2026-11-04", "po_number": "PO-2026-0101", "currency": "GBP",
            "vendor_iban": ACME_IBAN, "subtotal": "1500.00", "tax_rate": "20", "tax_amount": "300.00", "total": "1800.00"}
    base.update(fields)
    return {"id": "doc_aaaaaaaaaaaa", "fields": base, "lines": lines, "reader_notes": [], "corrected": []}


def check(deps, doc, today=TODAY):
    return {(f.code, f.severity) for f in run_checks(doc, deps.store, deps.policy, today)}


def only_codes(deps, doc):
    return sorted(c for c, _ in check(deps, doc))


def test_a_clean_invoice_only_gets_the_po_match_note(deps):
    assert check(deps, invoice()) == {("po_matched", "info")}


def test_required_fields_and_lines_are_errors_when_empty(deps):
    found = check(deps, invoice(invoice_number=None, total=None, lines=[]))
    assert {("missing_field", "error"), ("no_lines", "error")} <= found
    assert len([f for f in run_checks(invoice(invoice_number=None, total=None), deps.store, deps.policy, TODAY)
                if f.code == "missing_field"]) == 2


def test_unknown_vendor_is_an_error_and_misspellings_are_not_matched(deps):
    assert ("vendor_unknown", "error") in check(deps, invoice(vendor_name="Zenith Demo Trading LLC", po_number=None))
    assert ("vendor_unknown", "error") in check(deps, invoice(vendor_name="Acme Demo Componets Ltd"))
    # case, punctuation and a listed alias are fine
    assert ("vendor_unknown", "error") not in check(deps, invoice(vendor_name="ACME demo components, ltd."))
    assert ("vendor_unknown", "error") not in check(deps, invoice(vendor_name="Acme Demo Components Limited"))


def test_changed_bank_details_are_an_error(deps):
    assert ("bank_changed", "error") in check(deps, invoice(vendor_iban="GB40DEMO40009999999999"))


def test_bank_findings_never_spell_out_the_vendor_masters_details(deps):
    """Clerks may see findings: the master's IBAN or account number appears only as its last four."""
    findings = run_checks(invoice(vendor_iban="GB40DEMO40009999999999"), deps.store, deps.policy, TODAY)
    message = next(f.message for f in findings if f.code == "bank_changed")
    assert "ends 0001" in message and "4000011" not in message and "GB77" not in message
    uk = run_checks(invoice(vendor_iban=None, vendor_sort_code="40-00-99", vendor_account="99999999"),
                    deps.store, deps.policy, TODAY)
    message = next(f.message for f in uk if f.code == "bank_changed")
    assert "sort code" in message and "40-00-01" not in message and "10000001" not in message


def test_half_of_the_uk_bank_details_is_not_enough(deps):
    """A sort code that matches the master, with no account number read (or the reverse), must be looked at."""
    only_sort = check(deps, invoice(vendor_iban=None, vendor_sort_code="40-00-01"))
    assert ("bank_missing", "blocking") in only_sort and "bank_changed" not in {c for c, _ in only_sort}
    only_account = check(deps, invoice(vendor_iban=None, vendor_account="10000001"))
    assert ("bank_missing", "blocking") in only_account
    both = check(deps, invoice(vendor_iban=None, vendor_sort_code="40-00-01", vendor_account="10000001"))
    assert not {c for c, _ in both} & {"bank_missing", "bank_changed", "bank_unverified"}


def test_an_iban_with_wrong_check_digits_is_an_error_even_for_an_unknown_vendor(deps):
    assert ("bank_invalid", "error") in check(deps, invoice(vendor_iban="GB78DEMO40000110000001"))
    assert ("bank_invalid", "error") in check(deps, invoice(vendor_name="New Demo Co", vendor_iban="GB00DEMO40000110000001"))


def test_no_readable_iban_must_be_acknowledged(deps):
    # covers bank details printed in another format (sort code, dashes): nothing to compare, so a person looks
    assert ("bank_missing", "blocking") in check(deps, invoice(vendor_iban=None))


def test_an_iban_cannot_be_trusted_when_the_master_has_none(deps):
    deps.store.set_vendor_iban("V001", None)
    found = check(deps, invoice(vendor_iban="GB40DEMO40009999999999"))
    assert ("bank_unverified", "blocking") in found and "bank_changed" not in {c for c, _ in found}


def test_line_arithmetic(deps):
    bad = [dict(LINES[0], line_total="905.00"), LINES[1]]
    found = check(deps, invoice(lines=bad, subtotal="1505.00", tax_amount="301.00", total="1806.00"))
    assert ("line_math", "blocking") in found
    # rounding to the cent is not a finding: 3 x 3.3333 = 9.9999 -> 10.00
    rounding = [{"sku": "AC-100", "description": "x", "quantity": "3", "unit_price": "3.3333", "line_total": "10.00"}]
    assert "line_math" not in only_codes(deps, invoice(lines=rounding, po_number=None, subtotal="10.00",
                                                       tax_amount="2.00", total="12.00"))


def test_a_line_without_amount_is_an_error(deps):
    assert ("line_incomplete", "error") in check(deps, invoice(lines=[dict(LINES[0], line_total=None), LINES[1]]))


@pytest.mark.parametrize("fields, code", [
    ({"subtotal": "1510.00", "tax_amount": "302.00", "total": "1812.00"}, "subtotal_mismatch"),
    ({"tax_amount": "310.00", "total": "1810.00"}, "tax_mismatch"),
    ({"total": "1810.00"}, "total_mismatch"),
    ({"tax_rate": "17", "tax_amount": "255.00", "total": "1755.00"}, "tax_rate_unusual"),
])
def test_totals_and_tax(deps, fields, code):
    assert (code, "blocking") in check(deps, invoice(**fields))


def test_amount_tolerance_is_two_pence(deps):
    assert "total_mismatch" not in only_codes(deps, invoice(total="1800.02"))
    assert "total_mismatch" in only_codes(deps, invoice(total="1800.03"))


def test_currency_rules(deps):
    assert ("currency_not_allowed", "error") in check(deps, invoice(currency="JPY"))
    found = check(deps, invoice(currency="EUR"))
    assert ("currency_unusual", "warning") in found and ("po_currency_mismatch", "blocking") in found


def test_dates(deps):
    week = (TODAY + timedelta(days=7)).isoformat()
    later = (TODAY + timedelta(days=8)).isoformat()
    assert "date_in_future" not in only_codes(deps, invoice(invoice_date=week, due_date=None))
    assert ("date_in_future", "blocking") in check(deps, invoice(invoice_date=later, due_date=None))
    assert ("due_before_invoice", "blocking") in check(deps, invoice(due_date="2026-10-01"))
    assert ("due_far_out", "warning") in check(deps, invoice(due_date="2027-06-01"))
    assert ("due_missing", "warning") in check(deps, invoice(due_date=None))


def test_duplicate_invoice_same_vendor_and_number(deps):
    first = stored_doc(deps, invoice(invoice_number="inv-a 1001")["fields"])  # spelled differently
    found = check(deps, invoice())
    assert ("duplicate_invoice", "blocking") in found
    set_status(deps, first["id"], "rejected")  # a rejected document does not count
    assert "duplicate_invoice" not in only_codes(deps, invoice())


def test_same_number_from_another_vendor_is_not_a_duplicate(deps):
    stored_doc(deps, invoice(vendor_name="Brightline Demo Packaging Ltd")["fields"])
    assert "duplicate_invoice" not in only_codes(deps, invoice())


def test_same_vendor_and_total_within_days_under_another_number_must_be_acknowledged(deps):
    stored_doc(deps, invoice(invoice_number="INV-A-0999", invoice_date="2026-10-02")["fields"])
    assert ("possible_duplicate", "blocking") in check(deps, invoice())
    # 28 days later: most likely a monthly invoice, so only a warning naming the other one
    far = check(deps, invoice(invoice_date="2026-10-30", due_date="2026-11-29"))
    assert ("possible_duplicate", "warning") in far and ("possible_duplicate", "blocking") not in far


def test_duplicates_are_found_through_aliases_and_look_alike_numbers(deps):
    stored_doc(deps, invoice(vendor_name="Acme Demo Components Limited", invoice_number="INV-A-1OO1")["fields"])
    found = run_checks(invoice(), deps.store, deps.policy, TODAY)
    look_alike = [f for f in found if f.code == "possible_duplicate"]
    assert [f.severity for f in look_alike] == ["blocking"] and "INV-A-1OO1" in look_alike[0].message
    assert "duplicate_invoice" not in {f.code for f in found}


def test_a_copy_of_an_approved_invoice_is_an_error_not_a_tick_box(deps):
    stored_doc(deps, invoice(invoice_number="inv a/1001")["fields"], status="exported")  # printed the same way
    assert ("duplicate_invoice", "error") in check(deps, invoice())


@pytest.mark.parametrize("earlier, later", [
    ("INV-2026-0001", "INV-2026-001"),   # zeros dropped inside one part of the number
    ("2026/0042", "2026/42"),
    ("INV-11", "INV-L1"),                # look-alike letters
    ("SO-1001", "S0-1001"),
])
def test_numbers_written_differently_are_caught_but_can_be_ticked(deps, earlier, later):
    """A re-sent invoice with its number written a little differently is found even a month later
    and without a PO; because the printed numbers and the totals differ, it is blocking, not an error."""
    stored_doc(deps, invoice(invoice_number=earlier, po_number=None, invoice_date="2026-09-05",
                             due_date="2026-10-05", total="999.00")["fields"], status="approved")
    found = check(deps, invoice(invoice_number=later, po_number=None))
    assert ("possible_duplicate", "blocking") in found
    assert ("duplicate_invoice", "error") not in found


@pytest.mark.parametrize("status", ["approved", "exported"])
def test_a_look_alike_copy_of_an_approved_invoice_with_the_same_total_is_an_error(deps, status):
    """INV-A-01001 after INV-A-1001 was approved, same total: one tick must not be enough to pay twice."""
    stored_doc(deps, invoice(invoice_number="INV-A-1001")["fields"], status=status)
    found = check(deps, invoice(invoice_number="INV-A-01001"))
    assert ("duplicate_invoice", "error") in found
    assert "possible_duplicate" not in {code for code, _ in found}


def test_numbers_that_only_share_digits_are_not_matched(deps):
    stored_doc(deps, invoice(invoice_number="INV-10-01", total="999.00")["fields"], status="approved")
    found = only_codes(deps, invoice(invoice_number="INV-1001"))
    assert "duplicate_invoice" not in found and "possible_duplicate" not in found


def test_po_reference_problems(deps):
    assert ("po_missing", "blocking") in check(deps, invoice(po_number=None))
    from dataclasses import replace
    relaxed = replace(deps.policy, require_po=False)
    assert ("po_missing", "warning") in {(f.code, f.severity) for f in run_checks(invoice(po_number=None), deps.store,
                                                                                   relaxed, TODAY)}
    assert ("po_not_found", "blocking") in check(deps, invoice(po_number="PO-2026-9999"))
    other_vendor = check(deps, invoice(po_number="PO-2026-0102"))
    assert ("po_vendor_mismatch", "blocking") in other_vendor and ("po_line_not_found", "blocking") in other_vendor


def test_closed_po(deps):
    with deps.store._conn() as conn:
        conn.execute("UPDATE purchase_orders SET status = 'closed' WHERE number = 'PO-2026-0101'")
    assert ("po_closed", "blocking") in check(deps, invoice())


def test_unit_price_against_po_with_two_percent_tolerance(deps):
    def priced(price: str) -> dict:
        total = (Decimal(price) * 200).quantize(Decimal("0.01"))
        lines = [dict(LINES[0], unit_price=price, line_total=str(total)), LINES[1]]
        sub = total + Decimal("600.00")
        return invoice(lines=lines, subtotal=str(sub), tax_amount=str((sub * Decimal("0.2")).quantize(Decimal("0.01"))),
                       total=str(sub + (sub * Decimal("0.2")).quantize(Decimal("0.01"))))
    assert "po_price_mismatch" not in only_codes(deps, priced("4.59"))  # +2.0%
    assert ("po_price_mismatch", "blocking") in check(deps, priced("4.60"))  # +2.2%


def test_quantity_and_amount_count_earlier_invoices_against_the_same_po(deps):
    half = [dict(LINES[0], quantity="120", line_total="540.00")]
    earlier = stored_doc(deps, invoice(invoice_number="INV-A-0900", invoice_date="2026-10-01", subtotal="540.00",
                                       tax_amount="108.00", total="648.00")["fields"], lines=half, status="approved")
    found = check(deps, invoice())
    assert ("po_qty_exceeded", "blocking") in found and ("po_amount_exceeded", "blocking") in found
    set_status(deps, earlier["id"], "needs_review")  # not yet approved: it does not use up the PO
    assert check(deps, invoice()) == {("po_matched", "info")}


def test_amount_already_invoiced_outside_this_system_counts(deps):
    with deps.store._conn() as conn:
        conn.execute("UPDATE purchase_orders SET already_invoiced = '100.00' WHERE number = 'PO-2026-0101'")
    assert ("po_amount_exceeded", "blocking") in check(deps, invoice())


def test_purchase_order_documents_are_compared_with_the_register_never_added(deps):
    po = {"id": "doc_bbbbbbbbbbbb", "reader_notes": [], "corrected": [],
          "fields": {"doc_type": "purchase_order", "vendor_name": "Brightline Demo Packaging Ltd", "po_number": "PO-2026-0105",
                     "order_date": "2026-10-02", "currency": "GBP", "subtotal": "335.00", "tax_rate": "20",
                     "tax_amount": "67.00", "total": "402.00"},
          "lines": [{"sku": "BP-310", "description": "Carton", "quantity": "300", "unit_price": "0.85", "line_total": "255.00"},
                    {"sku": "BP-505", "description": "Edge protector", "quantity": "200", "unit_price": "0.40", "line_total": "80.00"}]}
    assert check(deps, po) == {("po_in_register", "info")}
    po["fields"].update(subtotal="80.00", tax_amount="16.00", total="96.00")
    po["lines"] = po["lines"][1:]
    assert ("po_differs", "blocking") in check(deps, po)
    po["fields"]["po_number"] = "PO-2026-7777"  # a PO the ERP never issued
    assert ("po_not_in_register", "blocking") in check(deps, po)
    stored_doc(deps, po["fields"])
    assert ("duplicate_po", "blocking") in check(deps, po)
    assert deps.store.po("PO-2026-7777") is None


def test_unknown_document_type_stops_further_checks(deps):
    doc = invoice()
    doc["fields"]["doc_type"] = None
    assert check(deps, doc) == {("doc_type_unknown", "error")}


def test_reader_notes_disappear_once_the_field_is_corrected(deps):
    doc = invoice()
    doc["reader_notes"] = [{"code": "reader_conflict", "severity": "warning", "message": "two totals", "field": "total"}]
    assert ("reader_conflict", "warning") in check(deps, doc)
    doc["corrected"] = ["total"]
    assert "reader_conflict" not in only_codes(deps, doc)


def test_changing_a_value_read_from_the_pdf_must_be_acknowledged(deps):
    doc = invoice(po_number="PO-2026-0106")  # a PO with room, to hide that the real one is used up
    doc["original_fields"] = invoice()["fields"]
    doc["corrected"] = ["po_number"]
    found = check(deps, doc)
    assert ("value_corrected", "blocking") in found
    doc["fields"]["po_number"] = "PO-2026-0101"  # corrected back to what the PDF says: nothing to confirm
    assert "value_corrected" not in only_codes(deps, doc)


def test_typing_in_a_value_the_reader_left_empty_is_not_a_correction_finding(deps):
    doc = invoice()
    doc["original_fields"] = dict(doc["fields"], due_date=None)
    doc["corrected"] = ["due_date"]
    assert "value_corrected" not in only_codes(deps, doc)


def test_changed_lines_must_be_acknowledged(deps):
    doc = invoice(lines=[LINES[0]], subtotal="900.00", tax_amount="180.00", total="1080.00")
    doc["original_fields"] = dict(doc["fields"])
    doc["original_lines"] = LINES
    doc["corrected"] = ["lines"]
    assert ("value_corrected", "blocking") in check(deps, doc)


def test_a_price_on_a_free_po_line_is_flagged(deps):
    with deps.store._conn() as conn:
        conn.execute("""UPDATE purchase_orders SET lines = '[{"sku": "AC-100", "description": "x", "quantity": "200",
                        "unit_price": "0.00"}, {"sku": "AC-220", "description": "y", "quantity": "50",
                        "unit_price": "12.00"}]' WHERE number = 'PO-2026-0101'""")
    assert ("po_price_mismatch", "blocking") in check(deps, invoice())


def test_tax_without_a_printed_rate_must_still_fit_a_rate(deps):
    assert check(deps, invoice(tax_rate=None)) == {("po_matched", "info")}  # 300 on 1,500 is 20%
    inflated = check(deps, invoice(tax_rate=None, tax_amount="450.00", total="1950.00"))
    assert ("tax_rate_unusual", "blocking") in inflated


def test_pennies_that_add_up_over_many_lines_are_caught(deps):
    lines = [{"sku": "AC-100", "description": "x", "quantity": "1", "unit_price": "4.505", "line_total": "4.50"}] * 150
    doc = invoice(lines=lines, po_number=None, subtotal="675.00", tax_amount="135.00", total="810.00")
    found = check(deps, doc)
    assert ("subtotal_mismatch", "blocking") in found and "line_math" not in {c for c, _ in found}


def test_uk_sort_code_and_account_are_compared_with_the_master(deps):
    same = invoice(vendor_iban=None, vendor_sort_code="40-00-01", vendor_account="10000001")
    assert check(deps, same) == {("po_matched", "info")}
    changed = invoice(vendor_iban=None, vendor_sort_code="40-00-01", vendor_account="99999999")
    assert ("bank_changed", "error") in check(deps, changed)
