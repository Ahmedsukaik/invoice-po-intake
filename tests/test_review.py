"""Corrections, approval and rejection: the rules a reviewer works under."""
import threading
import time
from dataclasses import replace

import pytest
from conftest import OMAR, SARA, ack, add, codes

from intake import review
from intake.review import ActionRefused
from samples.make_samples import ACME_0101, invoice_pdf, purchase_order_pdf


def test_clean_invoice_is_approved_once(deps):
    doc = add(deps, "01_clean_invoice.pdf")
    approved = review.approve(deps, doc["id"], SARA)
    assert approved["status"] == "approved" and approved["decided_by"] == "sara (reviewer)"
    assert approved["vendor_id"] == "V001"  # fixed at approval, used by the export
    with pytest.raises(ActionRefused) as again:
        review.approve(deps, doc["id"], SARA)
    assert again.value.status_code == 409 and "already approved" in again.value.message


def test_each_blocking_finding_must_be_ticked_by_its_id_and_needs_a_note(deps):
    doc = add(deps, "06_exceeds_po.pdf")
    qty, amount = ack(doc, "po_qty_exceeded")[0], ack(doc, "po_amount_exceeded")[0]
    with pytest.raises(ActionRefused) as none:
        review.approve(deps, doc["id"], SARA)
    assert none.value.status_code == 409 and set(codes(none.value.findings)) == {"po_qty_exceeded", "po_amount_exceeded"}
    with pytest.raises(ActionRefused) as one:
        review.approve(deps, doc["id"], SARA, [amount], "only one of them ticked")
    assert codes(one.value.findings) == ["po_qty_exceeded"]
    with pytest.raises(ActionRefused):  # codes are not ids: a tick names one exact finding
        review.approve(deps, doc["id"], SARA, ["po_qty_exceeded", "po_amount_exceeded"], "codes instead of ids")
    with pytest.raises(ActionRefused, match="needs a note"):
        review.approve(deps, doc["id"], SARA, [qty, amount], "ok")
    done = review.approve(deps, doc["id"], SARA, [qty, amount, "0000000000"], "Buyer agreed the extra units")
    assert done["status"] == "approved" and done["acknowledged"] == ["po_amount_exceeded", "po_qty_exceeded"]
    assert done["decision_note"] == "Buyer agreed the extra units"


def test_an_acknowledgement_does_not_survive_a_changed_finding(deps):
    first = add(deps, "01_clean_invoice.pdf")
    copy = add(deps, "05_duplicate_invoice.pdf")  # the same invoice, resent as another file
    seen_on_copy = ack(copy, "duplicate_invoice")
    review.approve(deps, first["id"], SARA, ack(deps.store.get(first["id"])), "the other copy will be rejected")
    with pytest.raises(ActionRefused) as refused:  # the copy's finding is now an error, and the old tick is void
        review.approve(deps, copy["id"], OMAR, seen_on_copy + ack(copy), "ticked what I saw earlier")
    assert ("duplicate_invoice", "error") in {(f["code"], f["severity"]) for f in refused.value.findings}


def test_a_waiting_document_is_checked_again_when_a_related_one_arrives_or_is_decided(deps):
    first = add(deps, "01_clean_invoice.pdf")
    assert codes(first) == ["po_matched"]
    copy = add(deps, "05_duplicate_invoice.pdf")
    assert "duplicate_invoice" in codes(deps.store.get(first["id"]))  # the queue shows it without opening it
    review.reject(deps, copy["id"], SARA, "duplicate")
    assert codes(deps.store.get(first["id"])) == ["po_matched"]


@pytest.mark.parametrize("name, code", [("04_changed_bank_details.pdf", "bank_changed"),
                                        ("03_unknown_vendor.pdf", "vendor_unknown")])
def test_errors_cannot_be_acknowledged_away(deps, name, code):
    doc = add(deps, name)
    with pytest.raises(ActionRefused) as refused:
        review.approve(deps, doc["id"], SARA, [f["id"] for f in doc["findings"]], "I really want to approve this one")
    assert refused.value.status_code == 409 and code in codes(refused.value.findings)
    assert deps.store.get(doc["id"])["status"] == "needs_review"


def test_bank_details_read_from_the_pdf_cannot_be_edited_to_pass_the_check(deps):
    doc = add(deps, "04_changed_bank_details.pdf")
    for value in ("GB81 DEMO 4000 0330 0000 03", ""):
        with pytest.raises(ActionRefused, match="cannot be changed"):
            review.correct(deps, doc["id"], SARA, {"vendor_iban": value})
    assert deps.store.corrections(doc["id"]) == []


def test_a_real_bank_change_goes_through_the_vendor_master_then_a_recheck(deps):
    doc = add(deps, "04_changed_bank_details.pdf")
    deps.store.set_vendor_bank("V003", "GB40 DEMO 4000 9999 9999 99")  # the ERP's own controlled process
    assert "bank_changed" not in codes(review.recheck(deps, doc["id"], SARA))
    assert review.approve(deps, doc["id"], SARA)["status"] == "approved"


def test_approval_rechecks_current_data_not_what_the_reviewer_saw(deps):
    doc = add(deps, "01_clean_invoice.pdf")
    assert codes(doc) == ["po_matched"]
    deps.store.set_vendor_iban("V001", "GB81 DEMO 4000 0330 0000 03")  # master changed after the upload
    with pytest.raises(ActionRefused) as refused:
        review.approve(deps, doc["id"], SARA)
    assert "bank_changed" in codes(refused.value.findings)


def test_a_correction_records_old_and_new_values_and_reruns_the_checks(deps):
    doc = add(deps, "02_totals_do_not_add_up.pdf")
    assert "line_math" in codes(doc) and "total_mismatch" in codes(doc)
    lines = [dict(line) for line in doc["lines"]]
    lines[1]["line_total"] = "390.00"
    fixed = review.correct(deps, doc["id"], SARA, {"subtotal": "815.00", "tax_amount": "163.00", "total": "978.00"},
                           lines, "Supplier confirmed by phone", seen_version=doc["version"])
    assert fixed["version"] == doc["version"] + 1 and set(fixed["corrected"]) == {"subtotal", "tax_amount", "total", "lines"}
    # the arithmetic findings are gone, but each change to a value read from the PDF must now be confirmed
    assert set(codes(fixed)) == {"value_corrected", "po_matched"} and codes(fixed).count("value_corrected") == 4
    log = {c["field"]: (c["old_value"], c["new_value"], c["corrected_by"]) for c in deps.store.corrections(doc["id"])}
    assert log["total"] == ("974.00", "978.00", "sara (reviewer)")
    assert '"395.00"' in log["lines"][0] and '"390.00"' in log["lines"][1]
    assert fixed["original_fields"]["total"] == "974.00"  # what the PDF said is kept


def test_invalid_or_unknown_corrections_change_nothing(deps):
    doc = add(deps, "01_clean_invoice.pdf")
    for fields, message in [({"total": "1.800,00"}, "number"), ({"invoice_date": "31/12/2026"}, "YYYY-MM-DD"),
                            ({"approved": "yes"}, "Unknown field"), ({"currency": "pounds"}, "three-letter")]:
        with pytest.raises(ActionRefused, match=message):
            review.correct(deps, doc["id"], SARA, fields)
    with pytest.raises(ActionRefused, match="Line 1"):
        review.correct(deps, doc["id"], SARA, lines=[{"sku": "AC-100", "quantity": "-5"}])
    after = deps.store.get(doc["id"])
    assert after["version"] == doc["version"] and after["fields"] == doc["fields"]


def test_a_stale_page_cannot_overwrite_or_approve(deps):
    doc = add(deps, "01_clean_invoice.pdf")
    review.correct(deps, doc["id"], OMAR, {"due_date": "2026-11-05"}, seen_version=doc["version"])
    with pytest.raises(ActionRefused, match="changed after you opened it"):
        review.correct(deps, doc["id"], SARA, {"due_date": "2026-11-30"}, seen_version=doc["version"])
    with pytest.raises(ActionRefused, match="changed after you opened it"):
        review.approve(deps, doc["id"], SARA, seen_version=doc["version"])


def test_empty_rows_remove_lines_and_no_change_is_not_a_correction(deps):
    doc = add(deps, "01_clean_invoice.pdf")
    same = review.correct(deps, doc["id"], SARA, {"total": "1,800.00"})  # same value, written differently
    assert same["version"] == doc["version"] and deps.store.corrections(doc["id"]) == []
    blank = {"sku": "", "description": "", "quantity": "", "unit_price": "", "line_total": ""}
    fewer = review.correct(deps, doc["id"], SARA, lines=[doc["lines"][0], blank])
    assert len(fewer["lines"]) == 1 and "subtotal_mismatch" in codes(fewer)


SCAN_FIELDS = {"doc_type": "invoice", "vendor_name": "Acme Demo Components Ltd", "invoice_number": "INV-A-1003",
               "invoice_date": "2026-10-09", "due_date": "2026-11-08", "po_number": "PO-2026-0106", "currency": "GBP",
               "vendor_iban": "GB77 DEMO 4000 0110 0000 01", "subtotal": "450.00", "tax_rate": "20",
               "tax_amount": "90.00", "total": "540.00"}
SCAN_LINES = [{"sku": "AC-100", "description": "Steel bracket, 40 mm", "quantity": "100", "unit_price": "4.50",
               "line_total": "450.00"}]


def test_a_scan_is_typed_in_by_one_reviewer_and_approved_by_another(deps):
    doc = add(deps, "08_scanned_invoice.pdf")
    assert codes(doc) == ["doc_type_unknown", "manual_entry"]
    typed = review.correct(deps, doc["id"], SARA, SCAN_FIELDS, SCAN_LINES)
    assert typed["status"] == "needs_manual_entry" and codes(typed) == ["bank_typed", "po_matched"]
    review.correct(deps, doc["id"], SARA, {"vendor_iban": "GB77DEMO40000110000001 "})  # typed, so still fixable
    note = "IBAN checked against the PDF and the master"
    with pytest.raises(ActionRefused, match="four-eyes"):
        review.approve(deps, doc["id"], SARA, ack(typed), note)
    with pytest.raises(ActionRefused, match="ticked"):
        review.approve(deps, doc["id"], OMAR)
    done = review.approve(deps, doc["id"], OMAR, ack(typed), note)
    assert done["status"] == "approved" and done["acknowledged"] == ["bank_typed"]


def test_whoever_changed_a_read_value_cannot_approve_it_unless_the_rule_is_off(deps):
    doc = add(deps, "01_clean_invoice.pdf")
    changed = review.correct(deps, doc["id"], SARA, {"due_date": "2026-11-05"})
    with pytest.raises(ActionRefused, match="another reviewer"):
        review.approve(deps, doc["id"], SARA, ack(changed), "the date was misread")
    deps.policy = replace(deps.policy, four_eyes=False)
    assert review.approve(deps, doc["id"], SARA, ack(changed), "the date was misread")["status"] == "approved"


def test_typing_into_a_field_the_reader_left_empty_also_needs_a_second_reviewer(deps):
    """Typing a PO number onto an invoice that has none makes po_missing go away without any
    tick; whoever typed it still cannot approve the document alone."""
    data = invoice_pdf("acme", "INV-A-1011", "2026-10-05", "2026-11-04", None, ACME_0101,
                       "1,500.00", "20", "300.00", "1,800.00")
    doc = add(deps, data, "no_po.pdf")
    assert "po_missing" in codes(doc)
    typed = review.correct(deps, doc["id"], SARA, {"po_number": "PO-2026-0101"})
    assert not ack(typed)  # nothing left to tick: the PO matches
    with pytest.raises(ActionRefused, match="another reviewer"):
        review.approve(deps, doc["id"], SARA)
    assert review.approve(deps, doc["id"], OMAR)["status"] == "approved"


def test_the_findings_a_document_was_decided_on_are_never_overwritten(deps):
    first = add(deps, "01_clean_invoice.pdf")
    decided = review.approve(deps, first["id"], SARA)
    assert decided["status"] == "approved"
    # a stale re-check (computed before the approval, saved after it) does not land
    assert deps.store.save_findings(first["id"], [{"code": "stale", "severity": "error"}], decided["version"]) is False
    assert deps.store.require(first["id"])["findings"] == decided["findings"]


def test_typing_a_different_iban_on_a_scan_is_still_caught(deps):
    doc = add(deps, "08_scanned_invoice.pdf")
    typed = review.correct(deps, doc["id"], SARA, {"doc_type": "invoice", "vendor_name": "Acme Demo Components Ltd",
                                                   "vendor_iban": "GB40 DEMO 4000 9999 9999 99"})
    assert "bank_changed" in codes(typed)


def test_typing_the_master_iban_over_an_unreadable_one_is_not_silent(deps):
    data = invoice_pdf("acme", "INV-A-1010", "2026-10-05", "2026-11-04", "PO-2026-0101", ACME_0101,
                       "1,500.00", "20", "300.00", "1,800.00", iban="GB40-DEMO-4000-9999-9999-99")
    doc = add(deps, data, "dashes.pdf")
    typed = review.correct(deps, doc["id"], SARA, {"vendor_iban": "GB77 DEMO 4000 0110 0000 01"})  # the master's
    assert "bank_typed" in ack_codes(typed)
    with pytest.raises(ActionRefused, match="ticked"):
        review.approve(deps, doc["id"], OMAR)


def ack_codes(doc):
    return [f["code"] for f in doc["findings"] if f["severity"] == "blocking"]


def test_bank_details_in_another_format_are_not_waved_through(deps):
    data = invoice_pdf("acme", "INV-A-1009", "2026-10-05", "2026-11-04", "PO-2026-0101", ACME_0101,
                       "1,500.00", "20", "300.00", "1,800.00", iban="GB40-DEMO-4000-9999-9999-99")
    doc = add(deps, data, "dashes.pdf")
    assert doc["fields"]["vendor_iban"] is None and "bank_missing" in ack_codes(doc)
    with pytest.raises(ActionRefused, match="No bank details"):
        review.approve(deps, doc["id"], SARA)


def test_reject_needs_a_reason_and_only_works_before_a_decision(deps):
    doc = add(deps, "03_unknown_vendor.pdf")
    with pytest.raises(ActionRefused, match="reason"):
        review.reject(deps, doc["id"], SARA, "  ")
    rejected = review.reject(deps, doc["id"], SARA, "Not our supplier")
    assert rejected["status"] == "rejected" and rejected["decision_note"] == "Not our supplier"
    for action in (lambda: review.reject(deps, doc["id"], SARA, "again"), lambda: review.approve(deps, doc["id"], SARA),
                   lambda: review.correct(deps, doc["id"], SARA, {"total": "1.00"})):
        with pytest.raises(ActionRefused) as refused:
            action()
        assert refused.value.status_code == 409
    with pytest.raises(ActionRefused) as missing:
        review.reject(deps, "doc_000000000000", SARA, "no such thing")
    assert missing.value.status_code == 404


def test_approving_a_purchase_order_never_writes_the_po_register(deps):
    before = deps.store.purchase_orders()
    real = add(deps, "07_purchase_order.pdf")
    assert codes(real) == ["po_in_register"]
    review.approve(deps, real["id"], SARA)
    fake = add(deps, purchase_order_pdf("PO-2026-7777", "2026-10-03", "acme",
                                        [("AC-100", "Steel bracket, 40 mm", "12000", "4.50", "54,000.00")],
                                        "54,000.00", "20", "10,800.00", "64,800.00"), "fake-po.pdf")
    assert "po_not_in_register" in ack_codes(fake)
    review.approve(deps, fake["id"], OMAR, ack(fake), "purchasing will enter it in the ERP")
    assert deps.store.purchase_orders() == before  # nothing approved here created a PO
    invoice = add(deps, invoice_pdf("acme", "INV-A-7777", "2026-10-05", "2026-11-04", "PO-2026-7777",
                                    [("AC-100", "Steel bracket, 40 mm", "12000", "4.50", "54,000.00")],
                                    "54,000.00", "20", "10,800.00", "64,800.00"), "big.pdf")
    assert "po_not_found" in codes(invoice)


def test_read_again_is_only_for_documents_nobody_has_corrected(deps):
    doc = add(deps, "01_clean_invoice.pdf")
    again = review.read_again(deps, doc["id"], SARA)
    assert again["version"] == doc["version"] + 1 and again["fields"] == doc["fields"]
    review.correct(deps, doc["id"], SARA, {"due_date": "2026-11-05"})
    with pytest.raises(ActionRefused, match="already corrected"):
        review.read_again(deps, doc["id"], SARA)


def test_two_reviewers_approving_at_the_same_moment_produce_one_decision(deps):
    doc = add(deps, "01_clean_invoice.pdf")
    results: list[str] = []

    def go(who):
        try:
            results.append(review.approve(deps, doc["id"], who)["decided_by"])
        except ActionRefused as exc:
            results.append(f"refused {exc.status_code}")

    threads = [threading.Thread(target=go, args=(who,)) for who in (SARA, OMAR, SARA, OMAR)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count("refused 409") == 3
    approvals = [e for e in deps.store.audit_entries(100, doc["id"]) if e["action"] == "approve" and e["outcome"] == "ok"]
    assert len(approvals) == 1


def test_two_invoices_approved_at_once_cannot_overrun_a_po_together(deps, monkeypatch):
    slow_checks = review.run_checks

    def slow(*args, **kwargs):  # widen the gap between checking and approving, as on a busy server
        found = slow_checks(*args, **kwargs)
        time.sleep(0.3)
        return found

    def eighty(number, day):
        return add(deps, invoice_pdf("acme", number, day, "2026-11-08", "PO-2026-0106",
                                     [("AC-100", "Steel bracket, 40 mm", "80", "4.50", "360.00")],
                                     "360.00", "20", "72.00", "432.00"), f"{number}.pdf")

    # PO-2026-0106 is for 100 units; the two invoices are a month apart: at most a warning about the same amount
    first, second = eighty("INV-A-2001", "2026-09-08"), eighty("INV-A-2002", "2026-10-09")
    for doc in (first, second):
        assert [f["code"] for f in doc["findings"] if f["severity"] in ("error", "blocking")] == []
    monkeypatch.setattr(review, "run_checks", slow)
    results: dict[str, str] = {}

    def go(doc, who):
        try:
            results[doc["id"]] = review.approve(deps, doc["id"], who)["status"]
        except ActionRefused as exc:
            results[doc["id"]] = "refused: " + ",".join(sorted(codes(exc.findings)))

    threads = [threading.Thread(target=go, args=(first, SARA)), threading.Thread(target=go, args=(second, OMAR))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results.values()) == ["approved", "refused: po_amount_exceeded,po_qty_exceeded"]


def test_a_value_read_from_the_pdf_cannot_be_removed(deps):
    doc = add(deps, "06_exceeds_po.pdf")
    for name in ("po_number", "tax_rate", "due_date"):
        with pytest.raises(ActionRefused, match="not removed"):
            review.correct(deps, doc["id"], SARA, {name: ""})
    refusals = [e for e in deps.store.audit_entries(50, doc["id"]) if e["action"] == "correct"]
    assert [e["outcome"] for e in refusals] == ["refused"] * 3


def test_correcting_a_value_away_still_needs_an_acknowledgement(deps):
    doc = add(deps, "06_exceeds_po.pdf")  # over its PO: two blocking findings
    moved = review.correct(deps, doc["id"], SARA, {"po_number": "PO-2026-0106"})
    assert "value_corrected" in codes(moved)
    with pytest.raises(ActionRefused, match="A value read from the PDF was changed"):
        review.approve(deps, doc["id"], OMAR, ack(doc), "only the original findings ticked")


def test_refused_corrections_are_in_the_audit_log(deps):
    doc = add(deps, "04_changed_bank_details.pdf")
    with pytest.raises(ActionRefused):
        review.correct(deps, doc["id"], SARA, {"vendor_iban": "GB81 DEMO 4000 0330 0000 03"})
    entry = deps.store.audit_entries(1, doc["id"])[0]
    assert (entry["action"], entry["outcome"]) == ("correct", "refused") and "cannot be changed" in entry["detail"]


def test_every_step_is_in_the_audit_log(deps):
    doc = add(deps, "06_exceeds_po.pdf")
    with pytest.raises(ActionRefused):
        review.approve(deps, doc["id"], SARA)
    changed = review.correct(deps, doc["id"], SARA, {"due_date": "2026-11-09"}, note="typo")
    review.approve(deps, doc["id"], OMAR, ack(changed), "agreed with the buyer")
    trail = [(e["actor"], e["action"], e["outcome"]) for e in reversed(deps.store.audit_entries(50, doc["id"]))]
    assert trail == [("tom", "upload", "ok"), ("reader:rules", "extract", "ok"), ("checks", "check", "ok"),
                     ("sara", "approve", "refused"), ("sara", "correct", "ok"), ("checks", "check", "ok"),
                     ("omar", "approve", "ok")]
