"""Storage (SQLite): documents, corrections, vendor master, PO register, exports, audit log, sessions.

Statuses of a document
----------------------
needs_review        read and checked; waiting for a reviewer
needs_manual_entry  no usable text (scanned, password protected, unknown layout, reader failed);
                    a reviewer types the fields from the PDF, then it is reviewed like any other
approved            a reviewer approved it; waiting for the next export
rejected            a reviewer rejected it, with a reason; never exported
exporting           an export is writing it right now; if the export crashed, it stays here until the next
                    export (or an admin) can tell whether the batch was written
exported            written to an export batch; never exported again

The vendor master and the PO register are reference data from the ERP (seed files here).
Nothing a reviewer does in this tool writes to them.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .config import Identity, digest
from .values import dec, name_key, normalize, number_key

REVIEWABLE = ("needs_review", "needs_manual_entry")
STATUSES = ("needs_review", "needs_manual_entry", "approved", "rejected", "exporting", "exported")
COMMITTED = ("approved", "exporting", "exported")  # counts against a PO's remaining amount

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    doc_type TEXT,
    number_key TEXT,
    vendor_key TEXT,
    po_ref TEXT,
    total_cents INTEGER,
    fields TEXT NOT NULL,
    original_fields TEXT NOT NULL,
    evidence TEXT NOT NULL,
    lines TEXT NOT NULL,
    original_lines TEXT NOT NULL,
    reader_notes TEXT NOT NULL DEFAULT '[]',
    corrected TEXT NOT NULL DEFAULT '[]',
    findings TEXT NOT NULL DEFAULT '[]',
    checked_at TEXT,
    extractor TEXT NOT NULL,
    page_count INTEGER,
    text_layer TEXT NOT NULL DEFAULT '',
    manual_reason TEXT,
    uploaded_by TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT,
    acknowledged TEXT NOT NULL DEFAULT '[]',
    vendor_id TEXT,
    export_batch TEXT,
    exported_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_doc_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_doc_number ON documents(doc_type, number_key);
CREATE INDEX IF NOT EXISTS idx_doc_po ON documents(po_ref);
CREATE INDEX IF NOT EXISTS idx_doc_total ON documents(doc_type, total_cents);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    corrected_by TEXT NOT NULL,
    corrected_at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS vendors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]',
    iban TEXT,
    sort_code TEXT,
    account_number TEXT,
    currency TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    number TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    order_date TEXT,
    lines TEXT NOT NULL,
    already_invoiced TEXT NOT NULL DEFAULT '0.00',
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exports (
    batch_id TEXT PRIMARY KEY,
    exporter TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    document_ids TEXT NOT NULL DEFAULT '[]',
    files TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS export_items (
    document_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    document_id TEXT,
    outcome TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    token_digest TEXT NOT NULL,
    csrf TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    flash TEXT
);
"""

# PRAGMA user_version of a database made by this code. Opening an older database adds the
# columns it lacks and recomputes the lookup keys; a newer one is refused (see Store._prepare).
SCHEMA_VERSION = 2
# Columns that are NOT NULL without a default in SCHEMA, and the value an upgrade gives existing rows.
UPGRADE_DEFAULTS = {("purchase_orders", "source"): "'erp'"}

# Every column except the text layer, which can be 200 KB per document and is only needed on one page.
LIGHT = ("id, sha256, filename, size, source, status, version, doc_type, number_key, vendor_key, po_ref, total_cents, "
         "fields, original_fields, evidence, lines, original_lines, reader_notes, corrected, findings, checked_at, "
         "extractor, page_count, manual_reason, uploaded_by, uploaded_at, decided_by, decided_at, decision_note, "
         "acknowledged, vendor_id, export_batch, exported_at")
MAX_DETAIL_CHARS = 4000
SESSION_HOURS = 8


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class NotFound(KeyError):
    pass


class Conflict(Exception):
    """The document is not in a state that allows this action, or it changed meanwhile."""


class DatabaseVersionError(Exception):
    """The database cannot be used by this version of the code. The message says what to do."""


def _shrink(value: Any, depth: int = 0) -> Any:
    """Cut long strings inside audit details so one huge value cannot push the rest out."""
    if isinstance(value, str):
        return value if len(value) <= 300 else f"{value[:300]}...(+{len(value) - 300} chars)"
    if depth > 5:
        return "..."
    if isinstance(value, dict):
        return {str(k)[:60]: _shrink(v, depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, (list, tuple)):
        return [_shrink(v, depth + 1) for v in value[:100]]
    return value


def cents(amount: object) -> int | None:
    value = dec(amount)
    return int((value * 100).to_integral_value()) if value is not None else None


def derived(fields: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None, int | None]:
    """(doc_type, number_key, vendor_key, po_ref, total_cents): indexed copies used for lookups."""
    doc_type = fields.get("doc_type")
    number = fields.get("invoice_number") if doc_type == "invoice" else fields.get("po_number")
    po_ref = ((fields.get("po_number") or "").upper() or None) if doc_type == "invoice" else None
    return (doc_type, number_key(number) or None, name_key(fields.get("vendor_name")) or None, po_ref,
            cents(fields.get("total")))


class Store:
    def __init__(self, db_path: str):
        self._path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            self.upgraded_from = self._prepare(conn)

    def _prepare(self, conn: sqlite3.Connection) -> int | None:
        """Create the tables, or bring a database made by an earlier version up to SCHEMA_VERSION:
        add the columns it lacks and recompute the lookup keys (number_key and the others), whose
        rules may have changed. Returns the old version when it upgraded, else None."""
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise DatabaseVersionError(
                f"{self._path} was made by a newer version of this program (database version {version}; this "
                f"version knows up to {SCHEMA_VERSION}). Run the newer version, or start with a new database file.")
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if version == SCHEMA_VERSION or "documents" not in existing:
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return None
        expected = sqlite3.connect(":memory:")
        try:
            expected.executescript(SCHEMA)
            wanted = {t: expected.execute(f"PRAGMA table_info({t})").fetchall()
                      for (t,) in expected.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                                   "AND name NOT LIKE 'sqlite_%'")}
        finally:
            expected.close()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table, columns in wanted.items():
                if table not in existing:
                    continue  # created below
                have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                for _, name, col_type, notnull, default, pk in columns:
                    if name in have:
                        continue
                    default = default if default is not None else UPGRADE_DEFAULTS.get((table, name))
                    if pk or (notnull and default is None):
                        raise DatabaseVersionError(
                            f"{self._path} was made by an older version of this program, and the column "
                            f"{table}.{name} cannot be added to it automatically. Export what is approved with the "
                            "old version, then move the database file aside and start again; the PDFs in the "
                            "storage folder can be dropped into the inbox again.")
                    clause = f" NOT NULL DEFAULT {default}" if notnull else (f" DEFAULT {default}" if default else "")
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}{clause}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.executescript(SCHEMA)  # new tables and indexes
        with_keys = conn.execute("SELECT id, fields FROM documents").fetchall()
        conn.execute("BEGIN IMMEDIATE")
        for row in with_keys:
            conn.execute("UPDATE documents SET doc_type = ?, number_key = ?, vendor_key = ?, po_ref = ?, "
                         "total_cents = ? WHERE id = ?", (*derived(json.loads(row["fields"])), row["id"]))
        conn.execute("COMMIT")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._audit_upgrade(conn, version, len(with_keys))
        return version

    @staticmethod
    def _audit_upgrade(conn: sqlite3.Connection, old: int, documents: int) -> None:
        conn.execute("INSERT INTO audit_log (ts, actor, role, action, document_id, outcome, detail) "
                     "VALUES (?, 'system', 'system', 'database_upgrade', NULL, 'ok', ?)",
                     (now(), json.dumps({"from": old, "to": SCHEMA_VERSION, "documents": documents})))

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction. BEGIN IMMEDIATE takes the write lock up front, so a
        read-check-write sequence inside it cannot interleave with another writer."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @contextmanager
    def exclusive(self) -> Iterator[sqlite3.Connection]:
        """Hold the database write lock for a check-then-write sequence. Other connections can
        still read (and see only committed data), but no other write can land in between.
        Inside, write only through the connection given; any other write would wait for this one."""
        with self._tx() as conn:
            yield conn

    # ------------------------------------------------------------------ reference data (from the ERP)
    def load_seed(self, seed_dir: Path, replace: bool = False) -> tuple[int, int]:
        """Load seed/vendors.json and seed/purchase_orders.json. Without replace, only into empty tables.
        In a real deployment this is a read-only sync from the ERP."""
        vendors = json.loads((Path(seed_dir) / "vendors.json").read_text(encoding="utf-8"))
        pos = json.loads((Path(seed_dir) / "purchase_orders.json").read_text(encoding="utf-8"))
        with self._tx() as conn:
            if replace:
                conn.execute("DELETE FROM vendors")
                conn.execute("DELETE FROM purchase_orders")
            if conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0] == 0:
                for v in vendors:
                    conn.execute(
                        "INSERT INTO vendors (id, name, aliases, iban, sort_code, account_number, currency, active) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (v["id"], v["name"], json.dumps(v.get("aliases", [])), normalize("iban", v.get("iban")),
                         normalize("sortcode", v.get("sort_code")), normalize("account", v.get("account_number")),
                         v["currency"], 1 if v.get("active", True) else 0),
                    )
            if conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 0:
                for po in pos:
                    conn.execute(
                        "INSERT OR IGNORE INTO purchase_orders (number, vendor_id, currency, status, order_date, lines, "
                        "already_invoiced, source) VALUES (?,?,?,?,?,?,?, 'erp')",
                        (po["number"].upper(), po["vendor_id"], po["currency"], po.get("status", "open"),
                         po.get("order_date"), json.dumps(po["lines"]), po.get("already_invoiced", "0.00")),
                    )
        return len(vendors), len(pos)

    def vendors(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM vendors ORDER BY id").fetchall()
        return [{**dict(r), "aliases": json.loads(r["aliases"]), "active": bool(r["active"])} for r in rows]

    def set_vendor_bank(self, vendor_id: str, iban: str | None = None, sort_code: str | None = None,
                        account_number: str | None = None) -> None:
        """Changing bank details belongs to the ERP's own controlled vendor-master process.
        This exists for that sync (and the tests), not for reviewers."""
        with self._tx() as conn:
            conn.execute("UPDATE vendors SET iban = ?, sort_code = ?, account_number = ? WHERE id = ?",
                         (normalize("iban", iban), normalize("sortcode", sort_code),
                          normalize("account", account_number), vendor_id))

    def set_vendor_iban(self, vendor_id: str, iban: str | None) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE vendors SET iban = ? WHERE id = ?", (normalize("iban", iban), vendor_id))

    def po(self, number: str | None) -> dict | None:
        if not number:
            return None
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM purchase_orders WHERE number = ?", (number.upper(),)).fetchone()
        return {**dict(row), "lines": json.loads(row["lines"])} if row else None

    def purchase_orders(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM purchase_orders ORDER BY number").fetchall()
        return [{**dict(r), "lines": json.loads(r["lines"])} for r in rows]

    # ------------------------------------------------------------------ documents
    @staticmethod
    def _doc(row: sqlite3.Row) -> dict:
        out = dict(row)
        for key in ("fields", "original_fields", "evidence", "lines", "original_lines", "reader_notes", "corrected",
                    "findings", "acknowledged"):
            out[key] = json.loads(out[key])
        for key in ("number_key", "vendor_key", "po_ref", "total_cents"):
            out.pop(key, None)
        out.setdefault("text_layer", None)
        return out

    def add_document(self, *, sha256: str, filename: str, size: int, source: str, status: str,
                     fields: dict, evidence: dict, lines: list, notes: list, extractor: str,
                     page_count: int | None, text_layer: str, manual_reason: str | None,
                     uploaded_by: str) -> tuple[dict, bool]:
        """Store a new document. If the same file (same SHA-256) is already stored, that
        document is returned instead and nothing new is created."""
        doc_id = "doc_" + uuid.uuid4().hex[:12]
        doc_type, nkey, vkey, po_ref, total = derived(fields)
        with self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO documents (id, sha256, filename, size, source, status, doc_type, number_key, vendor_key, "
                    "po_ref, total_cents, fields, original_fields, evidence, lines, original_lines, reader_notes, "
                    "extractor, page_count, text_layer, manual_reason, uploaded_by, uploaded_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (doc_id, sha256, filename, size, source, status, doc_type, nkey, vkey, po_ref, total,
                     json.dumps(fields), json.dumps(fields), json.dumps(evidence), json.dumps(lines),
                     json.dumps(lines), json.dumps(notes), extractor, page_count, text_layer, manual_reason,
                     uploaded_by, now()),
                )
            except sqlite3.IntegrityError:
                row = conn.execute(f"SELECT {LIGHT} FROM documents WHERE sha256 = ?", (sha256,)).fetchone()
                if row is None:
                    raise
                return self._doc(row), False
            row = conn.execute(f"SELECT {LIGHT} FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return self._doc(row), True

    def reread(self, doc_id: str, expected_version: int, *, status: str, fields: dict, evidence: dict, lines: list,
               notes: list, extractor: str, manual_reason: str | None) -> dict:
        """Replace what the reader found, for a document nobody has corrected yet."""
        doc_type, nkey, vkey, po_ref, total = derived(fields)
        marks = ",".join("?" * len(REVIEWABLE))
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE documents SET status = ?, doc_type = ?, number_key = ?, vendor_key = ?, po_ref = ?, "
                "total_cents = ?, fields = ?, original_fields = ?, evidence = ?, lines = ?, original_lines = ?, "
                "reader_notes = ?, extractor = ?, manual_reason = ?, version = version + 1 "
                f"WHERE id = ? AND version = ? AND corrected = '[]' AND status IN ({marks})",
                (status, doc_type, nkey, vkey, po_ref, total, json.dumps(fields), json.dumps(fields),
                 json.dumps(evidence), json.dumps(lines), json.dumps(lines), json.dumps(notes), extractor,
                 manual_reason, doc_id, expected_version, *REVIEWABLE),
            )
            if cur.rowcount != 1:
                raise Conflict("Only a document that is still waiting and has not been corrected can be read again.")
            row = conn.execute(f"SELECT {LIGHT} FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return self._doc(row)

    def by_hash(self, sha256: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(f"SELECT {LIGHT} FROM documents WHERE sha256 = ?", (sha256,)).fetchone()
        return self._doc(row) if row else None

    def get(self, doc_id: str, with_text: bool = False) -> dict | None:
        columns = f"{LIGHT}, text_layer" if with_text else LIGHT
        with self._conn() as conn:
            row = conn.execute(f"SELECT {columns} FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return self._doc(row) if row else None

    def require(self, doc_id: str) -> dict:
        doc = self.get(doc_id)
        if doc is None:
            raise NotFound(doc_id)
        return doc

    def list_documents(self, statuses: tuple[str, ...] | None = None, limit: int = 200,
                       offset: int = 0) -> tuple[list[dict], int]:
        """(documents, total count), newest first."""
        where, args = "", []
        if statuses:
            where = f" WHERE status IN ({','.join('?' * len(statuses))})"
            args = list(statuses)
        with self._conn() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM documents{where}", args).fetchone()[0]
            rows = conn.execute(f"SELECT {LIGHT} FROM documents{where} ORDER BY uploaded_at DESC, rowid DESC "
                                "LIMIT ? OFFSET ?", (*args, max(1, min(limit, 500)), max(0, offset))).fetchall()
        return [self._doc(r) for r in rows], total

    def counts(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM documents GROUP BY status").fetchall()
        out = {s: 0 for s in STATUSES}
        out.update({r["status"]: r["n"] for r in rows})
        return out

    def same_number(self, doc_type: str, number: str | None, exclude_id: str) -> list[dict]:
        """Other documents of this type with the same number (see number_key), except rejected ones."""
        key = number_key(number)
        if not key:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT {LIGHT} FROM documents WHERE doc_type = ? AND number_key = ? AND id != ? "
                "AND status != 'rejected'", (doc_type, key, exclude_id)).fetchall()
        return [self._doc(r) for r in rows]

    def invoices_near_total(self, total: str | None, tolerance: Decimal, exclude_id: str) -> list[dict]:
        """Other invoices, not rejected, whose total is within the tolerance of this one."""
        center = cents(total)
        if center is None:
            return []
        spread = cents(tolerance) or 0
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT {LIGHT} FROM documents WHERE doc_type = 'invoice' AND total_cents BETWEEN ? AND ? "
                "AND id != ? AND status != 'rejected'", (center - spread, center + spread, exclude_id)).fetchall()
        return [self._doc(r) for r in rows]

    def invoices_for_po(self, po_number: str, exclude_id: str) -> list[dict]:
        """Invoices already approved or exported against this PO."""
        marks = ",".join("?" * len(COMMITTED))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT {LIGHT} FROM documents WHERE doc_type = 'invoice' AND po_ref = ? AND id != ? "
                f"AND status IN ({marks})", (po_number.upper(), exclude_id, *COMMITTED)).fetchall()
        return [self._doc(r) for r in rows]

    def related_waiting(self, doc: dict) -> list[dict]:
        """Documents still waiting for review whose findings may change because of this one:
        same number, same PO, or the same total."""
        doc_type, nkey, _, po_ref, total = derived(doc["fields"])
        marks = ",".join("?" * len(REVIEWABLE))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT {LIGHT} FROM documents WHERE id != ? AND status IN ({marks}) AND "
                "((number_key = ? AND doc_type = ?) OR po_ref = ? OR (total_cents BETWEEN ? AND ?))",
                (doc["id"], *REVIEWABLE, nkey, doc_type, po_ref, (total or -10) - 2, (total or -10) + 2)).fetchall()
        return [self._doc(r) for r in rows]

    def save_findings(self, doc_id: str, findings: list[dict], version: int) -> bool:
        """Store the latest check results, unless the document changed since they were computed or
        is no longer waiting: the findings of an approved or rejected document are the ones it was
        decided on, and stay as they are. Returns whether anything was written."""
        marks = ",".join("?" * len(REVIEWABLE))
        with self._tx() as conn:
            cur = conn.execute(f"UPDATE documents SET findings = ?, checked_at = ? WHERE id = ? AND version = ? "
                               f"AND status IN ({marks})", (json.dumps(findings), now(), doc_id, version, *REVIEWABLE))
            return cur.rowcount > 0

    def correct(self, doc_id: str, expected_version: int, fields: dict, lines: list,
                changes: list[tuple[str, Any, Any]], by: str, note: str | None) -> dict:
        """Apply corrections in one transaction. Each change is (field, old, new); all are recorded."""
        doc_type, nkey, vkey, po_ref, total = derived(fields)
        marks = ",".join("?" * len(REVIEWABLE))
        with self._tx() as conn:
            row = conn.execute("SELECT corrected FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if row is None:
                raise NotFound(doc_id)
            corrected = sorted(set(json.loads(row["corrected"])) | {c[0] for c in changes})
            cur = conn.execute(
                "UPDATE documents SET fields = ?, lines = ?, doc_type = ?, number_key = ?, vendor_key = ?, po_ref = ?, "
                f"total_cents = ?, corrected = ?, version = version + 1 WHERE id = ? AND version = ? AND status IN ({marks})",
                (json.dumps(fields), json.dumps(lines), doc_type, nkey, vkey, po_ref, total, json.dumps(corrected),
                 doc_id, expected_version, *REVIEWABLE),
            )
            if cur.rowcount != 1:
                raise Conflict("The document changed or was decided while you were editing it. Open it again.")
            stamp = now()
            for field_name, old, new in changes:
                conn.execute(
                    "INSERT INTO corrections (document_id, field, old_value, new_value, corrected_by, corrected_at, note) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (doc_id, field_name, None if old is None else json.dumps(old) if not isinstance(old, str) else old,
                     None if new is None else json.dumps(new) if not isinstance(new, str) else new, by, stamp, note),
                )
            row = conn.execute(f"SELECT {LIGHT} FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return self._doc(row)

    def corrections(self, doc_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM corrections WHERE document_id = ? ORDER BY id", (doc_id,)).fetchall()
        return [dict(r) for r in rows]

    def approve(self, doc_id: str, expected_version: int, by: str, note: str | None, acknowledged: list[str],
                findings: list[dict], vendor_id: str | None, conn: sqlite3.Connection | None = None) -> dict:
        """Mark approved, only if nobody changed or decided the document meanwhile. Pass `conn`
        from exclusive() to approve inside a transaction that already holds the write lock."""
        if conn is None:
            with self._tx() as own:
                return self.approve(doc_id, expected_version, by, note, acknowledged, findings, vendor_id, own)
        marks = ",".join("?" * len(REVIEWABLE))
        cur = conn.execute(
            "UPDATE documents SET status = 'approved', decided_by = ?, decided_at = ?, decision_note = ?, "
            "acknowledged = ?, findings = ?, checked_at = ?, vendor_id = ? "
            f"WHERE id = ? AND version = ? AND status IN ({marks})",
            (by, now(), note, json.dumps(acknowledged), json.dumps(findings), now(), vendor_id, doc_id,
             expected_version, *REVIEWABLE),
        )
        if cur.rowcount != 1:
            raise Conflict("The document changed or was decided meanwhile. Open it again and review the current version.")
        row = conn.execute(f"SELECT {LIGHT} FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return self._doc(row)

    def reject(self, doc_id: str, by: str, reason: str) -> dict:
        marks = ",".join("?" * len(REVIEWABLE))
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE documents SET status = 'rejected', decided_by = ?, decided_at = ?, decision_note = ? "
                f"WHERE id = ? AND status IN ({marks})",
                (by, now(), reason[:500], doc_id, *REVIEWABLE),
            )
            row = conn.execute(f"SELECT {LIGHT} FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            raise NotFound(doc_id)
        if cur.rowcount != 1:
            raise Conflict(f"Document {doc_id} is already {row['status']}.")
        return self._doc(row)

    # ------------------------------------------------------------------ exports
    def claim_for_export(self, batch_id: str, by: str) -> list[dict]:
        """Atomically move every approved document into this batch. A document can be claimed
        by one batch only; a second export running at the same time gets the rest (or none)."""
        with self._tx() as conn:
            conn.execute("UPDATE documents SET status = 'exporting', export_batch = ? WHERE status = 'approved'",
                         (batch_id,))
            rows = conn.execute(f"SELECT {LIGHT} FROM documents WHERE export_batch = ? AND status = 'exporting' "
                                "ORDER BY decided_at, rowid", (batch_id,)).fetchall()
            if rows:
                conn.execute("INSERT INTO exports (batch_id, created_by, created_at, document_ids) VALUES (?,?,?,?)",
                             (batch_id, by, now(), json.dumps([r["id"] for r in rows])))
        return [self._doc(r) for r in rows]

    def finish_export(self, batch_id: str, exporter: str, files: list[str]) -> None:
        with self._tx() as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM documents WHERE export_batch = ? AND status = 'exporting'", (batch_id,)).fetchall()]
            for doc_id in ids:  # the primary key makes a second export of the same document impossible
                conn.execute("INSERT INTO export_items (document_id, batch_id) VALUES (?,?)", (doc_id, batch_id))
            stamp = now()
            conn.execute("UPDATE documents SET status = 'exported', exported_at = ? WHERE export_batch = ? "
                         "AND status = 'exporting'", (stamp, batch_id))
            conn.execute("UPDATE exports SET exporter = ?, finished_at = ?, files = ? WHERE batch_id = ?",
                         (exporter, stamp, json.dumps(files), batch_id))

    def release_export(self, batch_id: str) -> None:
        """The batch was not written: its documents go back to approved for the next export."""
        with self._tx() as conn:
            conn.execute("UPDATE documents SET status = 'approved', export_batch = NULL WHERE export_batch = ? "
                         "AND status = 'exporting'", (batch_id,))
            conn.execute("DELETE FROM exports WHERE batch_id = ? AND finished_at IS NULL", (batch_id,))

    def unfinished_batches(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT batch_id FROM exports WHERE finished_at IS NULL ORDER BY created_at").fetchall()
        return [r["batch_id"] for r in rows]

    def exports(self, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM exports ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "document_ids": json.loads(r["document_ids"]), "files": json.loads(r["files"])} for r in rows]

    def export_batch(self, batch_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM exports WHERE batch_id = ?", (batch_id,)).fetchone()
        return {**dict(row), "document_ids": json.loads(row["document_ids"]), "files": json.loads(row["files"])} \
            if row else None

    # ------------------------------------------------------------------ audit log
    def audit(self, actor: str, role: str, action: str, outcome: str = "ok", document_id: str | None = None,
              detail: Any = None) -> None:
        text = json.dumps(_shrink(detail), default=str, ensure_ascii=False) if detail is not None else None
        if text and len(text) > MAX_DETAIL_CHARS:
            text = text[:MAX_DETAIL_CHARS] + "...(truncated)"
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, actor, role, action, document_id, outcome, detail) VALUES (?,?,?,?,?,?,?)",
                (now(), actor[:80], role[:20], action[:60], document_id, outcome[:20], text),
            )

    def audit_entries(self, limit: int = 50, document_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM audit_log", []
        if document_id:
            sql += " WHERE document_id = ?"
            args.append(document_id)
        with self._conn() as conn:
            rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, max(1, min(limit, 1000)))).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ web sessions
    def create_session(self, who: Identity, token_digest: str, hours: int = SESSION_HOURS) -> tuple[str, str]:
        """A random session id for the browser cookie (only its hash is stored) and a CSRF token.
        The session remembers which token signed in, so removing that token ends the session too."""
        session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
        with self._conn() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))
            conn.execute("INSERT INTO sessions (id_hash, name, role, token_digest, csrf, expires_at) VALUES (?,?,?,?,?,?)",
                         (digest(session_id), who.name, who.role, token_digest, csrf, expires))
        return session_id, csrf

    def session(self, session_id: str | None) -> tuple[Identity, str, str] | None:
        """(identity, csrf token, token digest) for a live session, or None."""
        if not session_id or len(session_id) > 200:
            return None
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id_hash = ? AND expires_at > ?",
                               (digest(session_id), now())).fetchone()
        return (Identity(row["name"], row["role"]), row["csrf"], row["token_digest"]) if row else None

    def end_session(self, session_id: str | None) -> None:
        if session_id:
            with self._conn() as conn:
                conn.execute("DELETE FROM sessions WHERE id_hash = ?", (digest(session_id),))

    def set_flash(self, session_id: str | None, message: Any) -> None:
        """A message for the next page this browser opens (redirect after POST)."""
        if session_id:
            with self._conn() as conn:
                conn.execute("UPDATE sessions SET flash = ? WHERE id_hash = ?",
                             (json.dumps(message), digest(session_id)))

    def pop_flash(self, session_id: str | None) -> Any:
        if not session_id:
            return None
        with self._tx() as conn:
            row = conn.execute("SELECT flash FROM sessions WHERE id_hash = ?", (digest(session_id),)).fetchone()
            if row is None or row["flash"] is None:
                return None
            conn.execute("UPDATE sessions SET flash = NULL WHERE id_hash = ?", (digest(session_id),))
        return json.loads(row["flash"])
