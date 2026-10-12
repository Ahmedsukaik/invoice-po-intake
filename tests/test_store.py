"""Opening a database made by an earlier version: upgraded in place, or refused with a plain message."""
import json
import sqlite3

import pytest

from intake.store import SCHEMA, SCHEMA_VERSION, DatabaseVersionError, Store
from intake.values import number_key

# The tables as an earlier version created them: no total_cents or vendor_id on documents, no UK
# bank columns on vendors, no flash on sessions, no source on purchase_orders, no user_version.
OLD_SCHEMA = (SCHEMA.replace("    total_cents INTEGER,\n", "")
              .replace("    first_number_key TEXT,\n    first_vendor_key TEXT,\n    first_total_cents INTEGER,\n", "")
              .replace("CREATE INDEX IF NOT EXISTS idx_doc_first_number ON documents(doc_type, first_number_key);\n", "")
              .replace("CREATE INDEX IF NOT EXISTS idx_doc_first_total ON documents(doc_type, first_total_cents);\n", "")
              .replace("    vendor_id TEXT,\n    export_batch", "    export_batch")
              .replace("CREATE INDEX IF NOT EXISTS idx_doc_total ON documents(doc_type, total_cents);\n", "")
              .replace("    sort_code TEXT,\n    account_number TEXT,\n", "")
              .replace(",\n    flash TEXT\n", "\n")
              .replace(",\n    source TEXT NOT NULL\n", "\n"))
FIELDS = {"doc_type": "invoice", "vendor_name": "Acme Demo Components Ltd", "invoice_number": "INV-2026-0001",
          "total": "1800.00"}


def old_database(path) -> None:
    assert OLD_SCHEMA != SCHEMA and "total_cents" not in OLD_SCHEMA
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO documents (id, sha256, filename, size, source, status, doc_type, number_key, fields, "
        "original_fields, evidence, lines, original_lines, extractor, uploaded_by, uploaded_at) "
        "VALUES ('doc_000000000001', 'aa', 'old.pdf', 1, 'upload', 'needs_review', 'invoice', '1NV20261OLD', ?, ?, "
        "'{}', '[]', '[]', 'rules', 'tom (clerk)', '2026-10-01T00:00:00+00:00')", (json.dumps(FIELDS), json.dumps(FIELDS)))
    conn.execute("INSERT INTO purchase_orders (number, vendor_id, currency, lines) VALUES ('PO-1', 'V001', 'GBP', '[]')")
    conn.commit()
    conn.close()


def test_an_older_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "intake.db"
    old_database(path)
    store = Store(str(path))
    assert store.upgraded_from == 0
    with store._conn() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        row = conn.execute("SELECT number_key, total_cents, first_number_key, first_total_cents FROM documents").fetchone()
        assert row["number_key"] == number_key("INV-2026-0001") and row["total_cents"] == 180000
        assert row["first_number_key"] == row["number_key"] and row["first_total_cents"] == 180000  # as first read
        assert conn.execute("SELECT source FROM purchase_orders").fetchone()[0] == "erp"
        assert {"sort_code", "account_number"} <= {r[1] for r in conn.execute("PRAGMA table_info(vendors)")}
    # the old document is found by the new duplicate rules, and the upgrade is in the audit log
    assert [d["id"] for d in store.same_number("invoice", "INV-2026-001", "doc_x")] == ["doc_000000000001"]
    assert [e["action"] for e in store.audit_entries(5)] == ["database_upgrade"]
    assert Store(str(path)).upgraded_from is None  # opening it again changes nothing


def test_a_database_from_a_newer_version_is_refused(tmp_path):
    path = tmp_path / "intake.db"
    Store(str(path))
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(DatabaseVersionError, match="newer version"):
        Store(str(path))
