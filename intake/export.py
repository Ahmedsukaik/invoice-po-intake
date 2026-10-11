"""Export of approved documents for an accounting import. Only approved documents are
exported, and each one at most once.

The file format lives in one replaceable class. CsvJsonExporter writes a folder per batch:

    outbox/<batch id>/invoices.csv            one row per invoice
    outbox/<batch id>/invoice_lines.csv       one row per invoice line
    outbox/<batch id>/purchase_orders.csv     one row per purchase order
    outbox/<batch id>/purchase_order_lines.csv
    outbox/<batch id>/batch.json              everything above, plus who approved what and why

To post into QuickBooks, Xero or an ERP instead, write another class with the same `write`
method (it receives the same records) and pass it as Deps.exporter. Bank details are not
exported: payment uses the vendor master in the accounting system.

How "at most once" is kept:
- Exports run one at a time: a lock (a lock file, plus a thread lock) is held for the whole
  export, so two export requests never overlap, even from two processes on one machine.
- Approved documents are claimed into a batch in one database transaction (status
  `exporting`). Only after the exporter succeeds are they marked `exported`.
- If the exporter fails, or the process dies, the batch is settled by asking the exporter
  whether it was written (`was_written`). CsvJsonExporter can tell: it writes to a temporary
  folder and renames it into place, so the folder exists only when complete. An exporter that
  cannot tell leaves the batch "in doubt": its documents stay `exporting`, are never
  re-exported automatically, and an admin settles it with resolve_batch after checking the
  target system. A replacement exporter should also pass the document id to the target as
  its own reference, so the target can refuse a duplicate.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Protocol

from .checks import match_vendor
from .config import Identity

if TYPE_CHECKING:
    from .deps import Deps

BATCH_ID = re.compile(r"exp_\d{8}_\d{6}_[0-9a-f]{6}", re.ASCII)
INVOICE_COLUMNS = ["document_id", "vendor_id", "vendor_name", "invoice_number", "invoice_date", "due_date", "po_number",
                   "currency", "subtotal", "tax_rate", "tax_amount", "total", "approved_by", "approved_at",
                   "acknowledged_findings", "approval_note", "source_file", "source_sha256"]
PO_COLUMNS = ["document_id", "vendor_id", "vendor_name", "po_number", "order_date", "currency", "subtotal", "tax_rate",
              "tax_amount", "total", "approved_by", "approved_at", "source_file", "source_sha256"]
LINE_COLUMNS = ["document_id", "number", "line_no", "sku", "description", "quantity", "unit_price", "line_total"]
TEXT_COLUMNS = {"vendor_name", "invoice_number", "po_number", "number", "sku", "description", "approval_note",
                "approved_by", "source_file", "acknowledged_findings"}


class Exporter(Protocol):
    """name, and write(). Optional, and recommended: was_written(folder) -> True / False / None,
    which says whether a batch reached the target (None: cannot tell)."""

    name: str

    def write(self, folder: Path, batch: dict[str, Any], records: list[dict[str, Any]]) -> list[str]:
        """Write one batch into `folder` (it exists and is empty). Return the file names written."""
        ...


class ExportBusy(Exception):
    pass


def safe_cell(column: str, value: Any) -> Any:
    """Stop spreadsheet formula injection: text that starts with = + - @ (or a tab/CR) gets a
    leading apostrophe. Vendor names and descriptions come from untrusted PDFs."""
    if value is None:
        return ""
    if column in TEXT_COLUMNS and isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


class CsvJsonExporter:
    name = "csv-json"

    def write(self, folder: Path, batch: dict[str, Any], records: list[dict[str, Any]]) -> list[str]:
        invoices = [r for r in records if r["document_type"] == "invoice"]
        orders = [r for r in records if r["document_type"] == "purchase_order"]
        files = {
            "invoices.csv": (INVOICE_COLUMNS, invoices),
            "invoice_lines.csv": (LINE_COLUMNS, self._lines(invoices, "invoice_number")),
            "purchase_orders.csv": (PO_COLUMNS, orders),
            "purchase_order_lines.csv": (LINE_COLUMNS, self._lines(orders, "po_number")),
        }
        for file_name, (columns, rows) in files.items():
            with open(folder / file_name, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(columns)
                for row in rows:
                    writer.writerow([safe_cell(c, row.get(c)) for c in columns])
        (folder / "batch.json").write_text(
            json.dumps({**batch, "documents": records}, indent=2, ensure_ascii=False), encoding="utf-8")
        return [*files, "batch.json"]

    @staticmethod
    def was_written(folder: Path) -> bool:
        """The batch folder only appears, complete, when the temporary folder is renamed."""
        return (folder / "batch.json").is_file()

    @staticmethod
    def _lines(records: list[dict], number_field: str) -> list[dict]:
        return [{"document_id": r["document_id"], "number": r.get(number_field), "line_no": i, **line}
                for r in records for i, line in enumerate(r["lines"], 1)]


def record_for(doc: dict, vendors: list[dict]) -> dict[str, Any]:
    f = doc["fields"]
    vendor_id = doc.get("vendor_id")  # fixed when the document was approved
    if vendor_id is None:
        vendor = match_vendor(f.get("vendor_name"), vendors)
        vendor_id = vendor["id"] if vendor else None
    return {
        "document_id": doc["id"], "document_type": f.get("doc_type"),
        "vendor_id": vendor_id, "vendor_name": f.get("vendor_name"),
        "invoice_number": f.get("invoice_number"), "invoice_date": f.get("invoice_date"), "due_date": f.get("due_date"),
        "po_number": f.get("po_number"), "order_date": f.get("order_date"), "currency": f.get("currency"),
        "subtotal": f.get("subtotal"), "tax_rate": f.get("tax_rate"), "tax_amount": f.get("tax_amount"),
        "total": f.get("total"),
        "lines": [{k: line.get(k) for k in ("sku", "description", "quantity", "unit_price", "line_total")}
                  for line in doc["lines"]],
        "approved_by": doc["decided_by"], "approved_at": doc["decided_at"],
        "acknowledged_findings": ";".join(doc["acknowledged"]), "approval_note": doc["decision_note"],
        "corrected_fields": doc["corrected"], "reader": doc["extractor"],
        "source_file": doc["filename"], "source_sha256": doc["sha256"],
    }


# --------------------------------------------------------------------------- one export at a time
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def _try_file_lock(fh: Any) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _file_unlock(fh: Any) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def export_lock(outbox: Path, timeout: float = 30.0) -> Iterator[None]:
    """Held for a whole export. The operating system releases the file lock if the process
    dies, so a lock that can be taken means no other export is running."""
    key = str(Path(outbox).resolve())
    with _thread_locks_guard:
        thread_lock = _thread_locks.setdefault(key, threading.Lock())
    if not thread_lock.acquire(timeout=max(timeout, 0.0)):
        raise ExportBusy("Another export is running. Try again in a moment.")
    try:
        with open(Path(outbox) / ".export.lock", "a+b") as fh:
            deadline = time.monotonic() + timeout
            while not _try_file_lock(fh):
                if time.monotonic() >= deadline:
                    raise ExportBusy("Another export is running. Try again in a moment.")
                time.sleep(0.1)
            try:
                yield
            finally:
                _file_unlock(fh)
    finally:
        thread_lock.release()


def _settle(deps: "Deps", who: Identity, batch_id: str, action: str) -> str:
    """Decide what happened to an unfinished batch. Returns finished, released or in_doubt."""
    outbox = Path(deps.outbox_dir)
    shutil.rmtree(outbox / f".tmp-{batch_id}", ignore_errors=True)
    check = getattr(deps.exporter, "was_written", None)
    written = check(outbox / batch_id) if check else None
    if written is True:
        folder = outbox / batch_id
        deps.store.finish_export(batch_id, deps.exporter.name, sorted(p.name for p in folder.iterdir()) if folder.is_dir() else [])
        outcome = "finished"
    elif written is False:
        deps.store.release_export(batch_id)
        outcome = "released"
    else:
        outcome = "in_doubt"
    deps.store.audit(who.name, who.role, action, outcome, detail={"batch": batch_id})
    return outcome


def run_export(deps: "Deps", who: Identity, lock_timeout: float = 30.0) -> dict[str, Any] | None:
    """Export every approved document. Returns the batch, or None when nothing was waiting."""
    outbox = Path(deps.outbox_dir)
    with export_lock(outbox, lock_timeout):
        for batch_id in deps.store.unfinished_batches():  # with the lock held, only a crash leaves these
            _settle(deps, who, batch_id, "export.recover")
        stamp = datetime.now(timezone.utc)
        batch_id = f"exp_{stamp:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
        docs = deps.store.claim_for_export(batch_id, who.label)
        if not docs:
            deps.store.audit(who.name, who.role, "export", "nothing", detail={"batch": None})
            return None
        tmp, final = outbox / f".tmp-{batch_id}", outbox / batch_id
        vendors = deps.store.vendors()
        records = [record_for(d, vendors) for d in docs]
        batch = {"batch_id": batch_id, "created_at": stamp.isoformat(timespec="seconds"), "created_by": who.label,
                 "exporter": deps.exporter.name, "count": len(records)}
        try:
            tmp.mkdir(parents=True)
            files = deps.exporter.write(tmp, batch, records)
            os.replace(tmp, final)
        except BaseException as exc:
            outcome = _settle(deps, who, batch_id, "export.failed")
            deps.store.audit(who.name, who.role, "export", "failed", detail={"batch": batch_id, "settled": outcome,
                                                                              "error": type(exc).__name__})
            raise
        deps.store.finish_export(batch_id, deps.exporter.name, files)
        for doc in docs:
            deps.store.audit(who.name, who.role, "export", "ok", doc["id"], {"batch": batch_id})
        deps.store.audit(who.name, who.role, "export.batch", "ok",
                         detail={"batch": batch_id, "documents": len(docs), "files": files})
        return {**batch, "documents": [d["id"] for d in docs], "files": files, "folder": str(final)}


def resolve_batch(deps: "Deps", who: Identity, batch_id: str, written: bool, lock_timeout: float = 30.0) -> str:
    """An admin settles a batch left in doubt, after checking the target system:
    written=True marks its documents exported; written=False puts them back to approved."""
    with export_lock(Path(deps.outbox_dir), lock_timeout):
        if not BATCH_ID.fullmatch(batch_id or "") or batch_id not in deps.store.unfinished_batches():
            raise KeyError(f"No unfinished export batch {str(batch_id)[:40]}.")
        if written:
            deps.store.finish_export(batch_id, deps.exporter.name, [])
        else:
            deps.store.release_export(batch_id)
        outcome = "written" if written else "not_written"
        deps.store.audit(who.name, who.role, "export.resolve", outcome, detail={"batch": batch_id})
        return outcome
