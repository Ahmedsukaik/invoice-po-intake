"""End-to-end demo in one command:  python demo.py   (add --pause to stop after each scene)

Starts the intake server on a free local port with a throwaway database and folders, then
walks through the story: PDFs arrive, are read and checked, reviewers approve or reject
each one, approved documents are exported once, and every step is in the audit log.
No API key is needed: the PDFs are read by strict rules. Options:
  --pause   wait for Enter after each scene (for screen recordings)
  --serve   keep the server running afterwards, with one document left to review
  --keep    keep the temporary folder (database, stored PDFs, export files)
  --claude  read the PDFs with the Claude API instead (needs ANTHROPIC_API_KEY)
"""
from __future__ import annotations

import csv
import logging
import os
import shutil
import ssl
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import httpx

from intake.envfile import load_env_file
from intake.extract import ClaudeExtractor
from intake.localrun import ROOT, free_port, local_deps, serve
from intake.pipeline import scan_inbox
from intake.server import build_app
from intake.values import money_text

SAMPLES = ROOT / "samples" / "pdfs"
CLERK, REVIEWER, REVIEWER2, ADMIN = ("demo-clerk-token-00000001", "demo-reviewer-token-000001",
                                     "demo-reviewer-token-000002", "demo-admin-token-00000001")
TOKENS = {
    CLERK: {"name": "tom", "role": "clerk"},
    REVIEWER: {"name": "sara", "role": "reviewer"},
    REVIEWER2: {"name": "omar", "role": "reviewer"},
    ADMIN: {"name": "ana", "role": "admin"},
}
PAUSE = "--pause" in sys.argv
BOLD = sys.stdout.isatty() and (os.name != "nt" or "WT_SESSION" in os.environ)


def say(title: str) -> None:
    if PAUSE and not title.startswith("1."):
        try:
            input("\n(press Enter for the next scene) ")
        except EOFError:  # no keyboard attached: just carry on
            print()
    print(f"\n\033[1m=== {title} ===\033[0m" if BOLD else f"\n=== {title} ===")


def out(label: str, text: object = "") -> None:
    """Print 'label text', wrapped so long lines stay readable in a recording."""
    print(textwrap.fill(f"{label} {text}".rstrip(), width=100, subsequent_indent="    "))


def findings(doc: dict, indent: str = "    ") -> None:
    if not doc["findings"]:
        print(f"{indent}(no findings)")
    for f in doc["findings"]:
        print(textwrap.fill(f"{f['severity'].upper():<8} {f['code']}: {f['message']}", width=100,
                            initial_indent=indent, subsequent_indent=indent + "         "))


def ack(doc: dict, *codes: str) -> list[str]:
    """The ids of the blocking findings with these codes: what a reviewer ticks."""
    return [f["id"] for f in doc["findings"] if f["severity"] == "blocking" and (not codes or f["code"] in codes)]


def main(use_claude: bool, keep_serving: bool, keep_files: bool) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    logging.getLogger("intake").setLevel(logging.ERROR)
    load_env_file()
    if use_claude and not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("--claude needs ANTHROPIC_API_KEY (set it in .env or in your shell).")
    workdir = Path(tempfile.mkdtemp(prefix="invoice-intake-demo-"))
    deps = local_deps(workdir, TOKENS, ClaudeExtractor() if use_claude else None)
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    serve(build_app(deps), port)
    local = ssl.create_default_context()  # plain http on 127.0.0.1; one context instead of four bundle loads
    tom, sara, omar, ana = (httpx.Client(base_url=url, headers={"Authorization": f"Bearer {t}"}, timeout=300,
                                         verify=local) for t in (CLERK, REVIEWER, REVIEWER2, ADMIN))
    reader = "the Claude API" if use_claude else "strict rules, no AI model"
    ids: dict[str, str] = {}

    def doc(doc_id: str) -> dict:
        return sara.get(f"/api/documents/{doc_id}").json()

    def drop(*names: str) -> None:
        for name in names:
            shutil.copy(SAMPLES / name, deps.inbox_dir / name)

    say("1. Every request needs a token, and the role decides what it may do")
    out("GET /api/documents without a token ->", f"HTTP {httpx.get(f'{url}/api/documents').status_code}")
    tried = tom.post("/api/documents/doc_000000000000/approve", json={"version": 1})
    out("tom (clerk) tries to approve a document ->", f"HTTP {tried.status_code}, {tried.json()['error']}")
    out("sara (reviewer) tries to export ->", f"HTTP {sara.post('/api/exports').status_code}")

    say("2. Six PDFs land in the inbox folder; the watcher reads and checks them")
    out("(PDFs in this run are read by:", f"{reader})")
    drop("01_clean_invoice.pdf", "02_totals_do_not_add_up.pdf", "03_unknown_vendor.pdf",
         "04_changed_bank_details.pdf", "06_exceeds_po.pdf", "08_scanned_invoice.pdf")
    for item in scan_inbox(deps):
        ids[item.file[:2]] = item.document_id or ""
    for d in sorted(sara.get("/api/documents").json()["documents"], key=lambda d: d["filename"]):
        total = f"{money_text(d['total']):>9} {d['currency']}" if d["total"] else " " * 13
        print(f"  {d['filename']:<30} {d['status']:<19} {(d['vendor_name'] or '-'):<30} {total}  {d['checks']}")

    say("3. The clerk uploads a purchase order; it is checked against the PO register from the ERP")
    with open(SAMPLES / "07_purchase_order.pdf", "rb") as fh:
        up = tom.post("/api/documents", files={"file": ("07_purchase_order.pdf", fh, "application/pdf")})
    po_doc = up.json()["document"]
    ids["07"] = po_doc["id"]
    out(f"tom: upload 07_purchase_order.pdf -> HTTP {up.status_code},",
        f"{po_doc['id']} [{po_doc['status']}] {po_doc['fields']['po_number']} for {po_doc['fields']['vendor_name']}")
    findings(po_doc)

    say("4. A clean invoice: matches its PO, no issues -> approved")
    clean = doc(ids["01"])
    findings(clean)
    done = sara.post(f"/api/documents/{ids['01']}/approve", json={"version": clean["version"]}).json()
    out(f"sara: approve {ids['01']} ->", done["status"])
    drop("01_clean_invoice.pdf")
    item = scan_inbox(deps)[0]
    out("The same file dropped in the inbox again ->", f"same file as {item.document_id}, not added again")

    say("5. Totals that do not add up -> blocking findings; the reviewer rejects")
    totals = doc(ids["02"])
    findings(totals)
    refused = sara.post(f"/api/documents/{ids['02']}/approve", json={"version": totals["version"]})
    out("sara: approve without ticking anything ->", f"HTTP {refused.status_code}, {refused.json()['error']}")
    rej = sara.post(f"/api/documents/{ids['02']}/reject",
                    json={"reason": "Line 2 and the total are wrong. Asked Brightline for a corrected invoice."}).json()
    out(f"sara: reject {ids['02']} ->", f"{rej['status']} ({rej['decision_note']})")

    say("6. Unknown vendor and changed bank details -> errors; they cannot be approved")
    for key in ("03", "04"):
        d = doc(ids[key])
        out(f"{d['filename']}:")
        findings(d)
        tried = sara.post(f"/api/documents/{ids[key]}/approve", json={
            "acknowledge": [f["id"] for f in d["findings"]], "note": "approve anyway?", "version": d["version"]})
        out("    sara: approve, even ticking everything ->", f"HTTP {tried.status_code}, {tried.json()['error']}")
    sara.post(f"/api/documents/{ids['03']}/reject", json={"reason": "Not one of our suppliers."})
    sara.post(f"/api/documents/{ids['04']}/reject",
              json={"reason": "Called Cobalt on the number in the vendor master: their bank has NOT changed. Fraud attempt."})
    out("sara: rejects both, with reasons.")

    say("7. An invoice above its PO -> approved only after each finding is ticked, with a note")
    over = doc(ids["06"])
    findings(over)
    first = sara.post(f"/api/documents/{ids['06']}/approve", json={"version": over["version"]})
    out("sara: approve ->", f"HTTP {first.status_code}, {first.json()['error']}")
    ok = sara.post(f"/api/documents/{ids['06']}/approve", json={
        "acknowledge": ack(over), "version": over["version"],
        "note": "Buyer agreed the extra 40 plates by email on 2026-10-09; PO change to follow."}).json()
    out("sara: approve, ticking both findings, with a note ->", f"{ok['status']}, acknowledged {', '.join(ok['acknowledged'])}")

    say("8. A scanned PDF -> needs manual entry; one reviewer types, another approves")
    scan = doc(ids["08"])
    out(f"{scan['filename']}: [{scan['status']}]", scan["manual_reason"])
    typed = sara.post(f"/api/documents/{ids['08']}/corrections", json={
        "fields": {"doc_type": "invoice", "vendor_name": "Acme Demo Components Ltd", "invoice_number": "INV-A-1003",
                   "invoice_date": "2026-10-09", "due_date": "2026-11-08", "po_number": "PO-2026-0106",
                   "currency": "GBP", "vendor_iban": "GB77 DEMO 4000 0110 0000 01", "subtotal": "450.00",
                   "tax_rate": "20", "tax_amount": "90.00", "total": "540.00"},
        "lines": [{"sku": "AC-100", "description": "Steel bracket, 40 mm", "quantity": "100", "unit_price": "4.50",
                   "line_total": "450.00"}],
        "note": "Typed from the scanned PDF", "version": scan["version"]}).json()
    out("sara: types 12 fields and 1 line ->", f"{len(typed['corrections'])} corrections recorded (old and new value each)")
    findings(typed)
    note = "IBAN typed from the scan; compared with the PDF and the vendor master."
    own = sara.post(f"/api/documents/{ids['08']}/approve",
                    json={"version": typed["version"], "acknowledge": ack(typed), "note": note})
    out("sara: approve her own typing ->", f"HTTP {own.status_code}, {own.json()['error']}")
    done = omar.post(f"/api/documents/{ids['08']}/approve",
                     json={"version": typed["version"], "acknowledge": ack(typed), "note": note}).json()
    out("omar (second reviewer): approve, ticking the typed IBAN ->", done["status"])

    say("9. The purchase order agrees with the PO register -> approved, to be exported for purchasing")
    out("omar: approve the PO ->", omar.post(f"/api/documents/{ids['07']}/approve",
                                             json={"version": po_doc["version"]}).json()["status"])
    out("Approving it only records that a person checked the PDF against the register; the export lists it for "
        "purchasing. The register itself comes from the ERP and does not change.")

    say("10. A week later the supplier sends INV-A-1001 again, as a new file")
    drop("05_duplicate_invoice.pdf")
    dup_id = scan_inbox(deps)[0].document_id or ""
    findings(doc(dup_id))
    sara.post(f"/api/documents/{dup_id}/reject", json={"reason": "Duplicate of INV-A-1001, already approved."})
    out("sara: rejects it as a duplicate (an approved invoice's copy cannot be approved at all).")

    say("11. Export: approved documents become CSV and JSON files, each exported once")
    batch = ana.post("/api/exports").json()["batch"]
    out(f"ana (admin): export -> batch {batch['batch_id']},", f"{batch['count']} documents: {', '.join(batch['files'])}")
    with open(Path(batch["folder"]) / "invoices.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            print(f"    invoices.csv: {row['vendor_id']} {row['invoice_number']:<11} {money_text(row['total']):>9} "
                  f"{row['currency']}  approved by {row['approved_by']}"
                  + (f", despite {row['acknowledged_findings']}" if row["acknowledged_findings"] else ""))
    out("ana: export again ->", ana.post("/api/exports").json()["message"])

    say("12. The audit log: who did what, and when")
    entries = list(reversed(ana.get("/api/audit", params={"limit": 300}).json()["entries"]))
    for e in entries:
        if e["actor"] != "checks":
            print(f"  {e['ts'][11:19]}  {e['actor'][:22]:<22} {e['action']:<12} {(e['document_id'] or ''):<17} {e['outcome']}")
    hidden = sum(1 for e in entries if e["actor"] == "checks")
    print(f"  (+ {hidden} 'check' entries, one after every upload and correction, not shown here)")

    if keep_serving:
        drop("09_price_above_po.pdf")
        scan_inbox(deps)
        print(f"\nThe server is still running: {url}/  One invoice is waiting in 'To review'. Sign in with:")
        for token, info in TOKENS.items():
            print(f"  {info['role']:<9} {info['name']:<5} {token}")
        print("Press Ctrl+C to stop.", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    if keep_files or keep_serving:
        print(f"\nDatabase, stored PDFs and export files for this run are in {workdir}")
    else:
        for client in (tom, sara, omar, ana):
            client.close()
        shutil.rmtree(workdir, ignore_errors=True)
        print("\n(Temporary files removed. Run with --keep to keep the database and the export files.)")


if __name__ == "__main__":
    try:
        main(use_claude="--claude" in sys.argv, keep_serving="--serve" in sys.argv, keep_files="--keep" in sys.argv)
    except Exception as exc:
        if "--claude" not in sys.argv:
            raise
        raise SystemExit(f"The run with --claude failed: {type(exc).__name__}: {str(exc)[:300]}") from None
