"""Export: only approved documents, never one twice, in files an accounting import can read."""
import csv
import json
import threading
from pathlib import Path

import pytest

from conftest import ANA, OMAR, SARA, ack, add
from intake import review
from intake.export import CsvJsonExporter, ExportBusy, export_lock, resolve_batch, run_export
from samples.make_samples import invoice_pdf


def approved(deps, *names):
    ids = []
    for name in names:
        doc = add(deps, name)
        review.approve(deps, doc["id"], SARA)
        ids.append(doc["id"])
    return ids


def rows(folder: str, name: str) -> list[dict]:
    with open(Path(folder) / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_only_approved_documents_are_exported_and_only_once(deps):
    ok = approved(deps, "01_clean_invoice.pdf")
    waiting = add(deps, "06_exceeds_po.pdf")
    rejected = add(deps, "03_unknown_vendor.pdf")
    review.reject(deps, rejected["id"], SARA, "not ours")
    batch = run_export(deps, ANA)
    assert sorted(batch["documents"]) == sorted(ok)
    assert {deps.store.get(i)["status"] for i in ok} == {"exported"}
    assert deps.store.get(waiting["id"])["status"] == "needs_review"
    assert run_export(deps, ANA) is None  # nothing new
    assert len(deps.store.exports()) == 1
    [later] = approved(deps, "07_purchase_order.pdf")
    assert run_export(deps, ANA)["documents"] == [later]  # the next batch holds only the new approval


def test_files_have_the_approved_values_and_no_bank_details(deps):
    inv_id, po_id = approved(deps, "01_clean_invoice.pdf", "07_purchase_order.pdf")
    batch = run_export(deps, ANA)
    assert batch["files"] == ["invoices.csv", "invoice_lines.csv", "purchase_orders.csv", "purchase_order_lines.csv",
                              "batch.json"]
    [inv] = rows(batch["folder"], "invoices.csv")
    assert (inv["document_id"], inv["vendor_id"], inv["invoice_number"], inv["total"], inv["approved_by"]) == \
           (inv_id, "V001", "INV-A-1001", "1800.00", "sara (reviewer)")
    assert [(l["line_no"], l["sku"], l["line_total"]) for l in rows(batch["folder"], "invoice_lines.csv")] == \
           [("1", "AC-100", "900.00"), ("2", "AC-220", "600.00")]
    [po] = rows(batch["folder"], "purchase_orders.csv")
    assert po["po_number"] == "PO-2026-0105" and po["vendor_id"] == "V002"
    everything = (Path(batch["folder"]) / "batch.json").read_text(encoding="utf-8")
    assert "DEMO" not in everything and "iban" not in everything.lower()  # payment uses the vendor master
    data = json.loads(everything)
    assert data["count"] == 2 and {d["document_id"] for d in data["documents"]} == {inv_id, po_id}


def test_acknowledged_findings_and_corrections_travel_with_the_export(deps):
    doc = add(deps, "06_exceeds_po.pdf")
    changed = review.correct(deps, doc["id"], SARA, {"due_date": "2026-11-09"})
    review.approve(deps, doc["id"], OMAR, ack(changed), "agreed with the buyer")  # a second reviewer
    batch = run_export(deps, ANA)
    [inv] = rows(batch["folder"], "invoices.csv")
    assert inv["acknowledged_findings"] == "po_amount_exceeded;po_qty_exceeded;value_corrected"
    assert inv["approval_note"] == "agreed with the buyer"
    record = json.loads((Path(batch["folder"]) / "batch.json").read_text(encoding="utf-8"))["documents"][0]
    assert record["corrected_fields"] == ["due_date"] and record["due_date"] == "2026-11-09"


def test_spreadsheet_formulas_from_a_pdf_are_neutralised(deps):
    # vendor names and descriptions come from the PDF, so a hostile value must stay text in a spreadsheet
    hostile = {"name": "=HYPERLINK(\"http://evil.example\",\"Acme\")", "address": "x", "email": None, "iban": ""}
    data = invoice_pdf(hostile, "INV-Z-1", "2026-10-05", None, None,
                       [("AC-100", "+cmd|' /C calc'!A0", "1", "1.00", "1.00")], "1.00", "20", "0.20", "1.20", iban=None)
    doc = add(deps, data, "evil.pdf")
    with deps.store._conn() as conn:  # approve it regardless of findings, to look at the export
        conn.execute("UPDATE documents SET status = 'approved', decided_by = 'test' WHERE id = ?", (doc["id"],))
    batch = run_export(deps, ANA)
    [inv] = rows(batch["folder"], "invoices.csv")
    [line] = rows(batch["folder"], "invoice_lines.csv")
    assert inv["vendor_name"].startswith("'=") and line["description"].startswith("'+")


def test_two_exports_at_the_same_moment_never_share_a_document(deps):
    ids = approved(deps, "01_clean_invoice.pdf", "07_purchase_order.pdf")
    results = []
    threads = [threading.Thread(target=lambda: results.append(run_export(deps, ANA))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    batches = [b for b in results if b]
    exported = [d for b in batches for d in b["documents"]]
    assert sorted(exported) == sorted(ids) and len(exported) == len(set(exported))


class Broken(CsvJsonExporter):
    def write(self, folder, batch, records):
        (folder / "invoices.csv").write_text("half written", encoding="utf-8")
        raise OSError("disk full")


def test_a_failed_write_puts_documents_back_and_leaves_no_batch(deps):
    [doc_id] = approved(deps, "01_clean_invoice.pdf")
    deps.exporter = Broken()
    with pytest.raises(OSError):
        run_export(deps, ANA)
    assert deps.store.get(doc_id)["status"] == "approved" and deps.store.exports() == []
    assert [p.name for p in Path(deps.outbox_dir).iterdir()] == [".export.lock"]  # no batch folder, no temp folder
    deps.exporter = CsvJsonExporter()
    assert run_export(deps, ANA)["documents"] == [doc_id]


def _crash_after_claim(deps, write_files: bool) -> tuple[str, str]:
    [doc_id] = approved(deps, "01_clean_invoice.pdf")
    batch_id = "exp_crashed_000001"
    claimed = deps.store.claim_for_export(batch_id, "ana (admin)")
    assert [d["id"] for d in claimed] == [doc_id]
    if write_files:
        folder = Path(deps.outbox_dir) / batch_id
        folder.mkdir()
        CsvJsonExporter().write(folder, {"batch_id": batch_id}, [])
    return doc_id, batch_id


def test_crash_after_files_were_written_finishes_that_batch(deps):
    doc_id, batch_id = _crash_after_claim(deps, write_files=True)
    assert run_export(deps, ANA) is None  # recovered, not exported a second time
    doc = deps.store.get(doc_id)
    assert doc["status"] == "exported" and doc["export_batch"] == batch_id


def test_crash_before_files_were_written_releases_the_documents(deps):
    doc_id, batch_id = _crash_after_claim(deps, write_files=False)
    batch = run_export(deps, ANA)
    assert batch["documents"] == [doc_id] and batch["batch_id"] != batch_id


def test_an_export_in_progress_is_never_disturbed(deps):
    [doc_id] = approved(deps, "01_clean_invoice.pdf")
    with export_lock(Path(deps.outbox_dir)):  # another export is running, however long it takes
        with pytest.raises(ExportBusy):
            run_export(deps, ANA, lock_timeout=0.3)
    assert deps.store.get(doc_id)["status"] == "approved" and deps.store.exports() == []


class PostsToErp:
    """A replacement exporter that cannot tell afterwards whether a batch reached the target."""
    name = "fake-erp"

    def __init__(self, fail: bool = False):
        self.posted: list[str] = []
        self.fail = fail

    def write(self, folder, batch, records):
        self.posted.extend(r["document_id"] for r in records)
        if self.fail:
            raise ConnectionError("the ERP did not answer")
        return []


def test_an_exporter_that_cannot_tell_leaves_the_batch_for_a_person(deps):
    [doc_id] = approved(deps, "01_clean_invoice.pdf")
    erp = PostsToErp(fail=True)
    deps.exporter = erp
    with pytest.raises(ConnectionError):
        run_export(deps, ANA)
    assert deps.store.get(doc_id)["status"] == "exporting"  # in doubt: maybe posted, maybe not
    erp.fail = False
    assert run_export(deps, ANA) is None and erp.posted == [doc_id]  # not posted a second time
    [batch] = deps.store.exports()
    assert resolve_batch(deps, ANA, batch["batch_id"], written=False) == "not_written"  # checked in the ERP
    run_export(deps, ANA)
    assert erp.posted == [doc_id, doc_id] and deps.store.get(doc_id)["status"] == "exported"
    with pytest.raises(KeyError):
        resolve_batch(deps, ANA, batch["batch_id"], written=True)  # already settled


def test_the_database_refuses_a_second_export_of_the_same_document(deps):
    import sqlite3

    [doc_id] = approved(deps, "01_clean_invoice.pdf")
    run_export(deps, ANA)
    with deps.store._conn() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO export_items (document_id, batch_id) VALUES (?, 'exp_other')", (doc_id,))


def test_the_exporter_is_replaceable(deps):
    seen = []

    class Accounting:
        name = "fake-erp"

        def write(self, folder, batch, records):
            seen.extend(records)
            (folder / "posted.txt").write_text(f"{len(records)} posted", encoding="utf-8")
            return ["posted.txt"]

    deps.exporter = Accounting()
    [doc_id] = approved(deps, "01_clean_invoice.pdf")
    batch = run_export(deps, ANA)
    assert batch["exporter"] == "fake-erp" and batch["files"] == ["posted.txt"]
    assert seen[0]["document_id"] == doc_id and seen[0]["lines"][0]["sku"] == "AC-100"
