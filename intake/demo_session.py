"""The web demo's state: a list of events the browser keeps, replayed on every request.

    add      {"type": "add", "sha256": "<hex>", "reader": "rules"}               a sample PDF arrives
             {"type": "add", "sha256": "<hex>", "reader": "rules", "ai": "budget"}   the AI reader was not used, and why
             {"type": "add", "sha256": "<hex>", "reader": "claude", "reading": {"answer": {...}, "model": "...",
                                                                                "usage": {...}, "ms": 0}}
    approve  {"type": "approve", "sha256": "<hex>", "by": "sara", "ack": ["<finding id>"], "note": "...", "version": 1}
    reject   {"type": "reject", "sha256": "<hex>", "by": "sara", "reason": "..."}
    correct  {"type": "correct", "sha256": "<hex>", "by": "sara", "fields": {...}, "lines": [...], "note": "...",
              "version": 1}
    export   {"type": "export"}

Events name documents by the SHA-256 of their file, and only the demo's own sample PDFs are
accepted. The server signs the event list (an HMAC with a secret only it knows) and refuses a
list whose signature does not match, so the browser cannot add, change or remove an event.

Every request builds a fresh copy of the product in a temporary folder: an SQLite database
seeded from seed/, the date pinned to 11 October 2026, the clock moving two minutes per event.
It replays the events through the product's own pipeline, checks and review rules
(intake/pipeline.py, checks.py, review.py), answers, and deletes the folder. Nothing is kept
on the server between requests, so visitors never see each other's work, and the same events
always give the same result.

An AI reading is the one thing that cannot be redone on replay without paying for it again:
the model's answer is stored inside its signed event, and on every replay it goes through the
Claude reader's own checks again (ClaudeExtractor.parse), so a stored answer is held to the
same rules as a fresh one. Only the layout the rules reader does not know (sample 10) uses the
AI reader. Without it, that file goes to a person, as it would in the product.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterator

from .checks import summary
from .config import Identity, Policy, TokenTable
from .deps import Deps
from .export import CsvJsonExporter, record_for
from .extract import ClaudeExtractor, Extracted, ExtractionFailed, ReaderUnavailable, RuleBasedExtractor
from .pdftext import PdfText, read_text
from .pipeline import check_and_save, ingest
from .review import ActionRefused, approve, correct, reject
from .store import REVIEWABLE, Store
from .values import FIELDS_BY_TYPE, HEADER_FIELDS, LINE_FIELDS, dec, iban_display, money

log = logging.getLogger("intake.demo")
ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "samples" / "pdfs"
SEED = ROOT / "seed"
TODAY = date(2026, 10, 11)
START = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)  # the clock moves two minutes per event
TMP_PREFIX = "intake-demo-"
MAX_EVENTS = 120
MAX_READING = 60_000      # characters of a stored AI answer
MAX_NOTE = 500
MAX_TEXT = 200            # a typed value
MAX_TYPED_LINES = 20
SIG = re.compile(r"[0-9a-f]{64}")
SHA = re.compile(r"[0-9a-f]{64}")
FINDING_ID = re.compile(r"[0-9a-f]{10}")
MODEL_ID = re.compile(r"claude-[a-z0-9.-]{1,60}")

# The AI reader: Claude Haiku 5.5 at effort low, streamed, at most 8,000 tokens of answer, 40 seconds,
# no retries (a retry could outlast the serverless function). Prices in USD per million tokens,
# from Anthropic's pricing page (October 2026), for the cost the page shows.
MODEL = "claude-haiku-5-5"
AI_MAX_TOKENS = 8000
# The whole read stays near 40 seconds (Vercel stops a function at 60): the clock starts before the
# stream opens, no new part is waited for after 25 seconds, and a silence may last 15 at most.
AI_DEADLINE = 25.0
AI_SILENCE = 15.0
PRICES = {"claude-haiku-5-5": (0.10, 0.50)}
MAX_SESSION_BYTES = 240_000  # a signed session larger than this is never handed out (the server reads 300 KB)
STALE_AFTER = 600            # seconds after which a leftover temporary folder is removed at start-up

CLERK = Identity("tom", "clerk")
ADMIN = Identity("ana", "admin")
REVIEWERS = {"sara": Identity("sara", "reviewer"), "omar": Identity("omar", "reviewer")}
NAMES = {"tom": "Tom", "sara": "Sara", "omar": "Omar", "ana": "Ana"}
# Errors that no correction can fix here: the document can only be rejected. The page shows them
# as a LOCKED stamp.
HARD_LOCKS = {"bank_changed", "bank_invalid", "vendor_unknown", "duplicate_invoice", "duplicate_po",
              "currency_not_allowed"}


class SessionError(Exception):
    """The session cannot be continued (changed outside the server, too long, or broken)."""


class ActionError(Exception):
    """A new action the demo cannot take (unknown document, bad input). Nothing is recorded."""


# ---------------------------------------------------------------------------- the sample PDFs
@dataclass(frozen=True)
class Sample:
    key: str
    file: str
    title: str
    note: str
    layout: str = "demo"   # demo: the rules reader knows it; other: needs the AI reader; scan: typed in by a person
    type_in: dict[str, Any] | None = None  # what a person reads off the page, for the page's "fill in" helper
    folder: Path = SAMPLE_DIR

    @property
    def path(self) -> Path:
        return self.folder / self.file


def _typed(fields: dict[str, str], lines: list[tuple[str, str, str, str, str]]) -> dict[str, Any]:
    return {"fields": {"doc_type": "invoice", **fields},
            "lines": [dict(zip(LINE_FIELDS, line, strict=True)) for line in lines]}


SAMPLES: dict[str, Sample] = {s.key: s for s in (
    Sample("01", "01_clean_invoice.pdf", "Clean invoice", "Acme, matches PO-2026-0101"),
    Sample("02", "02_totals_do_not_add_up.pdf", "Totals do not add up", "Brightline, a line and the totals are wrong"),
    Sample("03", "03_unknown_vendor.pdf", "Unknown supplier", "Zenith, not on file, no PO"),
    Sample("04", "04_changed_bank_details.pdf", "New bank details", "Cobalt asks to be paid to a new account"),
    Sample("05", "05_duplicate_invoice.pdf", "Duplicate of invoice 01", "Acme sends INV-A-1001 again as a reminder"),
    Sample("06", "06_exceeds_po.pdf", "More than was ordered", "Acme bills 140 plates on a PO for 100"),
    Sample("07", "07_purchase_order.pdf", "Purchase order", "Compared with the PO register from the ERP"),
    Sample("08", "08_scanned_invoice.pdf", "Scanned invoice", "No text layer: one reviewer types it in, another approves",
           "scan", _typed({"vendor_name": "Acme Demo Components Ltd", "invoice_number": "INV-A-1003",
                           "invoice_date": "2026-10-09", "due_date": "2026-11-08", "po_number": "PO-2026-0106",
                           "currency": "GBP", "vendor_iban": "GB77 DEMO 4000 0110 0000 01", "subtotal": "450.00",
                           "tax_rate": "20", "tax_amount": "90.00", "total": "540.00"},
                          [("AC-100", "Steel bracket, 40 mm", "100", "4.50", "450.00")])),
    Sample("09", "09_price_above_po.pdf", "Price above the PO", "Brightline charges 0.89 a carton; the PO says 0.85"),
    Sample("10", "10_other_layout.pdf", "A layout the rules do not know",
           "Brightline's own layout: the AI reader, or a person", "other",
           _typed({"vendor_name": "Brightline Demo Packaging Ltd", "invoice_number": "BDP/26/0417",
                   "invoice_date": "2026-10-09", "due_date": "2026-11-08", "po_number": "PO-2026-0105",
                   "currency": "GBP", "vendor_sort_code": "40-00-02", "vendor_account": "20000002",
                   "subtotal": "335.00", "tax_rate": "20", "tax_amount": "67.00", "total": "402.00"},
                  [("BP-310", "Carton, double wall, 400 x 300 mm", "300", "0.85", "255.00"),
                   ("BP-505", "Edge protector, 1 m", "200", "0.40", "80.00")])),
)}


@lru_cache(maxsize=64)
def _file(path: Path) -> tuple[bytes, str]:
    data = path.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


def sample_bytes(sample: Sample) -> bytes:
    return _file(sample.path)[0]


def sample_sha(sample: Sample) -> str:
    return _file(sample.path)[1]


def sample_by_sha(sha: str) -> Sample | None:
    return next((s for s in SAMPLES.values() if sample_sha(s) == sha), None)


_texts: dict[str, PdfText] = {}


def cached_text(data: bytes, max_pages: int, timeout: float, workers: int, wait: float) -> PdfText:
    """The text layer, read once per process. Only the demo's own sample files reach this (events
    are signed and name known files), so they are read in this process; uploads from strangers
    would go to the product's worker processes with a time limit instead."""
    key = hashlib.sha256(data).hexdigest()
    if key not in _texts:
        if len(_texts) > 64:
            _texts.clear()
        _texts[key] = read_text(data, max_pages)
    return _texts[key]


# ---------------------------------------------------------------------------- signing
def _secret() -> bytes:
    configured = os.environ.get("INTAKE_DEMO_SECRET", "")
    if len(configured) >= 32:
        return configured.encode()
    if os.environ.get("VERCEL"):  # several instances: a random secret per instance would end sessions at random
        raise RuntimeError("Set INTAKE_DEMO_SECRET (32 characters or more) for this deployment.")
    log.warning("INTAKE_DEMO_SECRET is not set (or shorter than 32 characters): using a random secret for this "
                "process, so demo sessions end when it restarts.")
    return secrets.token_bytes(32)


_SECRET = _secret()


def _canonical(events: list[Any]) -> bytes:
    return json.dumps({"v": 1, "events": events}, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode()


def signature(events: list[Any]) -> str:
    return hmac.new(_SECRET, _canonical(events), hashlib.sha256).hexdigest()


def signed(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {"v": 1, "events": events, "sig": signature(events)}


def new_session() -> dict[str, Any]:
    return signed([])


def verified_events(session: Any) -> list[dict[str, Any]]:
    """The events of a session the server signed, or SessionError."""
    if not isinstance(session, dict) or session.get("v") != 1 or not isinstance(session.get("events"), list):
        raise SessionError("not a demo session")
    events = session["events"]
    if len(events) > MAX_EVENTS:
        raise SessionError("this demo session is too long")
    sig = session.get("sig")
    try:
        valid = isinstance(sig, str) and SIG.fullmatch(sig) is not None and \
            hmac.compare_digest(sig.encode(), signature(events).encode())
    except ValueError:  # NaN, or another value JSON cannot carry
        valid = False
    if not valid:
        raise SessionError("the session was changed outside this server")
    return events


# ---------------------------------------------------------------------------- AI reader
def make_reader(client: Any) -> ClaudeExtractor:
    """The product's Claude reader with the demo's settings, all given explicitly, so a shell's
    INTAKE_CLAUDE_* settings cannot change it."""
    return ClaudeExtractor(client=client, model=MODEL, effort="low", fallbacks="off", date_order="dmy",
                           max_tokens=AI_MAX_TOKENS, deadline=AI_DEADLINE)


def live_reader_from_env() -> ClaudeExtractor | None:
    """The AI reader when ANTHROPIC_API_KEY is set and INTAKE_DEMO_AI is not "off"; else None,
    and the demo runs on the rules reader. A wrong setting never stops the demo."""
    if os.environ.get("INTAKE_DEMO_AI", "on").strip().lower() in ("off", "0", "false", "no"):
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic

        return make_reader(anthropic.Anthropic(timeout=anthropic.Timeout(AI_SILENCE, connect=5.0), max_retries=0))
    except BaseException:  # SystemExit from a settings check included
        log.exception("the AI reader could not be set up; the demo runs on the rules reader")
        return None


def cost_usd(model: str, usage: dict[str, int]) -> float:
    price = PRICES.get(model)
    if not price:
        return 0.0
    return (usage.get("input_tokens", 0) * price[0] + usage.get("output_tokens", 0) * price[1]) / 1_000_000


def _spend_limit(exc: BaseException) -> bool:
    """Did the API refuse because the key's spend limit is reached? The reader re-raises API errors
    as its own (without the cause), so this looks at the exception being handled when it did."""
    seen: BaseException | None = exc
    for _ in range(4):
        seen = seen.__context__ if seen is not None else None
        if seen is None:
            return False
        text = str(getattr(seen, "body", "") or "") + " " + str(seen)
        if "enforced_spend_limit_reached" in text or "usage limits" in text.lower():
            return True
    return False


def read_with_ai(reader: Any, data: bytes) -> tuple[dict[str, Any] | None, str]:
    """(the reading to store in the event, or None; and what happened: ok, budget, busy, failed)."""
    started = time.monotonic()
    try:
        answer, info = reader.read(data)
    except ReaderUnavailable as exc:
        return None, "budget" if _spend_limit(exc) else "busy"
    except ExtractionFailed as exc:
        return None, "budget" if _spend_limit(exc) else "failed"
    except Exception:
        log.exception("the AI reader failed")
        return None, "failed"
    usage = {k: v for k in ("input_tokens", "output_tokens")
             if isinstance(v := info.get(k), int) and not isinstance(v, bool) and 0 <= v < 10_000_000}
    served = str(info.get("model") or "")
    # Priced by the model asked for: the API may answer with a dated name the price list does not know.
    reading = {"answer": answer, "model": str(getattr(reader, "model", MODEL)),
               "served": served if MODEL_ID.fullmatch(served) else "", "usage": usage,
               "ms": int((time.monotonic() - started) * 1000)}
    if len(json.dumps(answer, ensure_ascii=True)) > MAX_READING:
        return None, "failed"
    return reading, "ok"


_PARSER = make_reader(client=object())  # parse() only: it never calls the API


class _StoredReading:
    """Stands in for the Claude reader on replay: the stored answer, checked again by parse()."""

    def __init__(self, reading: dict[str, Any]):
        self.reading = reading
        self.name = f"claude:{reading['model']}"

    def extract(self, pdf_bytes: bytes, pages: list[str]) -> Extracted:
        out = _PARSER.parse(self.reading["answer"], pages)
        out.extractor = self.name
        out.usage = {"model": self.reading["served"] or self.reading["model"], "request_id": None, **self.reading["usage"]}
        return out


# ---------------------------------------------------------------------------- one request's copy of the product
class DemoStore(Store):
    """The product's store, with the clock and the document ids pinned for replay. The database
    lives for one request in one thread, so it keeps one connection open and does not wait for
    the disk (the product opens a connection per query, for many threads and processes)."""

    def __init__(self, path: str, ids: dict[str, str]):
        self.clock = START
        self._ids = ids
        self._shared: sqlite3.Connection | None = None
        super().__init__(path)

    def now(self) -> str:
        return self.clock.isoformat(timespec="seconds")

    def new_document_id(self, sha256: str) -> str:
        return self._ids.get(sha256) or "doc_" + sha256[:12]

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if self._shared is None:
            self._shared = sqlite3.connect(self._path, timeout=15, isolation_level=None)
            self._shared.row_factory = sqlite3.Row
            self._shared.execute("PRAGMA synchronous=OFF")
        yield self._shared

    def close(self) -> None:
        if self._shared is not None:
            self._shared.close()
            self._shared = None


RULES = RuleBasedExtractor()


@dataclass
class Replay:
    """A throwaway copy of the product, built for one request (use it as a context manager)."""

    folder: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix=TMP_PREFIX)))
    ai: dict[str, dict[str, Any]] = field(default_factory=dict)   # sha -> reading or {"status": ...}
    exports: list[dict[str, Any]] = field(default_factory=list)
    count: int = 0

    def __post_init__(self) -> None:
        try:
            ids = {sample_sha(s): f"doc_{s.key}" for s in SAMPLES.values()}
            self.store = DemoStore(str(self.folder / "intake.db"), ids)
            self.store.load_seed(SEED)
            self.deps = Deps(store=self.store, tokens=TokenTable({}), extractor=RULES, storage_dir=self.folder / "storage",
                             inbox_dir=self.folder / "inbox", outbox_dir=self.folder / "outbox", policy=Policy(),
                             today=lambda: TODAY, text_reader=cached_text)
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> "Replay":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        store = getattr(self, "store", None)
        if store is not None:
            store.close()
        shutil.rmtree(self.folder, ignore_errors=True)

    # ------------------------------------------------------------------ events
    def doc_for(self, sha: str) -> dict:
        doc = self.store.by_hash(sha)
        if doc is None:
            raise SessionError("an event names a document that was never added")
        return doc

    def apply(self, event: Any) -> dict[str, Any] | None:
        """Apply one event. Returns what happened to it ({"ok", "message", "doc"}) for the page."""
        if not isinstance(event, dict):
            raise SessionError("bad event")
        self.count += 1
        self.store.clock = START + timedelta(minutes=2 * self.count)
        kind = event.get("type")
        if kind == "add":
            return self._add(event)
        if kind == "export":
            _only(event, {"type"})
            return self._export()
        if kind not in ("approve", "reject", "correct"):
            raise SessionError("unknown event")
        doc = self.doc_for(_sha(event.get("sha256")))
        by = event.get("by")
        if not isinstance(by, str) or by not in REVIEWERS:
            raise SessionError("unknown reviewer")
        who = REVIEWERS[by]
        try:
            if kind == "approve":
                _only(event, {"type", "sha256", "by", "ack", "note", "version"})
                ack = event.get("ack")
                if not isinstance(ack, list) or len(ack) > 40 or not all(isinstance(a, str) and FINDING_ID.fullmatch(a)
                                                                         for a in ack):
                    raise SessionError("bad ticks")
                approve(self.deps, doc["id"], who, ack, _text(event.get("note"), MAX_NOTE), _version(event))
                return {"ok": True, "doc": doc["id"], "message": "Approved."}
            if kind == "reject":
                _only(event, {"type", "sha256", "by", "reason"})
                reject(self.deps, doc["id"], who, _text(event.get("reason"), MAX_NOTE))
                return {"ok": True, "doc": doc["id"], "message": "Rejected."}
            _only(event, {"type", "sha256", "by", "fields", "lines", "note", "version"})
            correct(self.deps, doc["id"], who, _typed_fields(event.get("fields")), _typed_lines(event.get("lines")),
                    _text(event.get("note"), MAX_NOTE), _version(event))
            return {"ok": True, "doc": doc["id"], "message": "Saved."}
        except ActionRefused as exc:  # the product said no; it wrote that to the audit log, as it would for anyone
            return {"ok": False, "doc": doc["id"], "message": plain(exc.message), "findings": [f["id"] for f in exc.findings]}

    def _add(self, event: dict[str, Any]) -> dict[str, Any]:
        sha = _sha(event.get("sha256"))
        sample = sample_by_sha(sha)
        if sample is None:
            raise SessionError("not one of the demo's files")
        reader = event.get("reader")
        if reader == "claude":
            _only(event, {"type", "sha256", "reader", "reading"})
            reading = _reading(event.get("reading"))
            self.deps.extractor = _StoredReading(reading)
            self.ai[sha] = reading
        elif reader == "rules":
            _only(event, {"type", "sha256", "reader", "ai"})
            status = event.get("ai")
            if status is not None and status not in AI_STATUSES:
                raise SessionError("bad AI status")
            self.deps.extractor = RULES
            if status:
                self.ai.setdefault(sha, {"status": status})
        else:
            raise SessionError("bad reader")
        try:
            result = ingest(self.deps, sample_bytes(sample), sample.file, "upload", CLERK)
        finally:
            self.deps.extractor = RULES
        doc = result.document
        message = "Read and checked." if result.created else \
            "The same file again: recognised by its SHA-256 hash and not stored twice."
        return {"ok": True, "doc": doc["id"], "message": message, "created": result.created}

    def _export(self) -> dict[str, Any]:
        """The product's export, step by step: claim the approved documents into a batch, write the
        CSV and JSON files (CsvJsonExporter), mark them exported, log it. The product's lock and
        crash recovery are left out: this copy lives for one request. The batch id comes from the
        demo's clock, so a replay gives the same files."""
        store, who = self.store, ADMIN
        stamp = store.clock
        batch_id = f"exp_{stamp:%Y%m%d_%H%M%S}_{len(self.exports) + 1:06x}"
        docs = store.claim_for_export(batch_id, who.label)
        if not docs:
            store.audit(who.name, who.role, "export", "nothing", detail={"batch": None})
            return {"ok": True, "message": "Nothing to export: no approved document is waiting.", "batch": None}
        exporter = CsvJsonExporter()
        records = [record_for(d, store.vendors()) for d in docs]
        batch = {"batch_id": batch_id, "created_at": stamp.isoformat(timespec="seconds"), "created_by": who.label,
                 "exporter": exporter.name, "count": len(records)}
        folder = Path(self.deps.outbox_dir) / batch_id
        folder.mkdir(parents=True)
        files = exporter.write(folder, batch, records)
        store.finish_export(batch_id, exporter.name, files)
        for doc in docs:
            store.audit(who.name, who.role, "export", "ok", doc["id"], {"batch": batch_id})
        store.audit(who.name, who.role, "export.batch", "ok", detail={"batch": batch_id, "documents": len(docs),
                                                                       "files": files})
        contents = []
        for name in files:
            text = (folder / name).read_text(encoding="utf-8")
            rows = max(0, len(text.strip().splitlines()) - 1) if name.endswith(".csv") else len(records)
            contents.append({"name": name, "content": text, "rows": rows})
        self.exports.append({"batch": batch_id, "created_at": stamp.isoformat(timespec="seconds"), "count": len(docs),
                             "documents": [d["id"] for d in docs], "files": contents})
        return {"ok": True, "message": f"Exported {len(docs)} document{'s' if len(docs) != 1 else ''}.", "batch": batch_id}


AI_STATUSES = ("off", "unavailable", "visitor_limit", "busy", "budget", "failed")


# ---------------------------------------------------------------------------- shape checks
def _only(event: dict[str, Any], keys: set[str]) -> None:
    if set(event) - keys:
        raise SessionError("unexpected keys in an event")


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise SessionError("bad document reference")
    return value


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise SessionError("bad text")
    return value


def _version(event: dict[str, Any]) -> int:
    v = event.get("version")
    if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 10_000:
        raise SessionError("bad version")
    return v


def _typed_fields(value: Any) -> dict[str, str | None]:
    if not isinstance(value, dict) or len(value) > len(HEADER_FIELDS):
        raise SessionError("bad fields")
    out: dict[str, str | None] = {}
    for name, raw in value.items():
        if name not in HEADER_FIELDS or not (raw is None or (isinstance(raw, str) and len(raw) <= MAX_TEXT)):
            raise SessionError("bad field")
        out[name] = raw
    return out


def _typed_lines(value: Any) -> list[dict[str, str | None]] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_TYPED_LINES:
        raise SessionError("bad lines")
    lines = []
    for raw in value:
        if not isinstance(raw, dict) or set(raw) - set(LINE_FIELDS):
            raise SessionError("bad line")
        if not all(v is None or (isinstance(v, str) and len(v) <= MAX_TEXT) for v in raw.values()):
            raise SessionError("bad line value")
        lines.append({k: raw.get(k) for k in LINE_FIELDS})
    return lines


def _reading(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"answer", "model", "served", "usage", "ms"}:
        raise SessionError("bad AI reading")
    answer, model, served, usage, ms = raw["answer"], raw["model"], raw["served"], raw["usage"], raw["ms"]
    if not isinstance(served, str) or (served and not MODEL_ID.fullmatch(served)):
        raise SessionError("bad model")
    if not isinstance(answer, dict) or len(json.dumps(answer, ensure_ascii=True)) > MAX_READING:
        raise SessionError("bad AI answer")
    if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
        raise SessionError("bad model")
    if not isinstance(usage, dict) or set(usage) - {"input_tokens", "output_tokens"} or not all(
            isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10_000_000 for v in usage.values()):
        raise SessionError("bad usage")
    if not isinstance(ms, int) or isinstance(ms, bool) or not 0 <= ms < 600_000:
        raise SessionError("bad timing")
    return {"answer": answer, "model": model, "served": served, "usage": dict(usage), "ms": ms}


# ---------------------------------------------------------------------------- new actions from the page
def _always() -> str | None:
    return None


def new_event(r: Replay, action: dict[str, Any], *, reader: Any = None, want_ai: bool = True,
              ai_gate: Callable[[], str | None] = _always) -> dict[str, Any]:
    """Turn the page's action into an event, checking it first. May call the AI reader: ai_gate()
    is asked just before (the web server's limits); it returns None, or why not."""
    kind = action.get("type")
    if not isinstance(kind, str):
        raise ActionError("Unknown action.")
    if kind == "add":
        key = action.get("sample")
        sample = SAMPLES.get(key) if isinstance(key, str) else None
        if sample is None:
            raise ActionError("There is no such sample document.")
        sha = sample_sha(sample)
        if sample.layout != "other":
            return {"type": "add", "sha256": sha, "reader": "rules"}
        if r.store.by_hash(sha):  # the same file again: the product recognises it before reading
            return {"type": "add", "sha256": sha, "reader": "rules"}
        status = "off" if not want_ai else "unavailable" if reader is None else ai_gate()
        if status is None:
            reading, status = read_with_ai(reader, sample_bytes(sample))
            if reading is not None:
                return {"type": "add", "sha256": sha, "reader": "claude", "reading": reading}
        return {"type": "add", "sha256": sha, "reader": "rules", "ai": status}
    if kind == "export":
        return {"type": "export"}
    if kind not in ("approve", "reject", "correct"):
        raise ActionError("Unknown action.")
    doc_id = action.get("doc")
    doc = r.store.get(doc_id) if isinstance(doc_id, str) and len(doc_id) <= 40 else None
    if doc is None:
        raise ActionError("That document is not in this demo yet.")
    by = action.get("by")
    if not isinstance(by, str) or by not in REVIEWERS:
        raise ActionError("Choose who is reviewing: Sara or Omar.")
    base = {"type": kind, "sha256": doc["sha256"], "by": by}
    try:
        if kind == "reject":
            return {**base, "reason": _text(action.get("reason"), MAX_NOTE).strip()}
        version = _version(action)
        if kind == "approve":
            ack = action.get("ack", [])
            if not isinstance(ack, list) or len(ack) > 40 or not all(isinstance(a, str) and FINDING_ID.fullmatch(a)
                                                                     for a in ack):
                raise ActionError("Bad ticks.")
            return {**base, "ack": sorted(set(ack))[:40], "note": _text(action.get("note"), MAX_NOTE).strip(),
                    "version": version}
        return {**base, "fields": _typed_fields(action.get("fields") or {}), "lines": _typed_lines(action.get("lines")),
                "note": _text(action.get("note"), MAX_NOTE).strip(), "version": version}
    except SessionError as exc:
        raise ActionError("That input is too long or not in the expected shape.") from exc


def handle(session: Any, action: Any, *, reader: Any = None, want_ai: bool = True,
           ai_gate: Callable[[], str | None] = _always) -> dict[str, Any]:
    """One request: check the session, replay it, take the new action (if any), and answer with
    the new signed session and what the page shows. The temporary folder is always removed."""
    events = verified_events(session)
    action = action if isinstance(action, dict) else {}
    with Replay() as r:
        for event in events:
            r.apply(event)
        result = None
        if action.get("type", "view") != "view":
            if len(events) >= MAX_EVENTS:
                raise ActionError("This demo has reached its limit of actions. Replay the demo to start again.")
            event = new_event(r, action, reader=reader, want_ai=want_ai, ai_gate=ai_gate)
            if len(_canonical([*events, event])) > MAX_SESSION_BYTES:
                raise ActionError("This demo session is full. Replay the demo to start again.")
            result = r.apply(event)
            events = [*events, event]
        return {"session": signed(events), "view": view(r), "result": result}


def clear_stale_folders(max_age: float = STALE_AFTER) -> int:
    """Remove temporary folders left by requests that were cut off (a function stopped mid-request,
    say). Folders younger than max_age may belong to requests still running and are kept."""
    removed, now = 0, time.time()
    try:
        entries = list(Path(tempfile.gettempdir()).iterdir())
    except OSError:
        return 0
    for path in entries:
        try:
            if path.name.startswith(TMP_PREFIX) and path.is_dir() and now - path.stat().st_mtime > max_age:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------- what the page shows
# The product speaks to accounts payable staff ("vendor master", "2-way match"). The demo page
# speaks to anyone: the same findings, the same ids, in plainer words.
PLAIN = [
    # whole sentences first, so they read as sentences rather than patched phrases
    ("They come from the vendor master; a change there goes through its own controlled process.",
     "Payment uses the details on file, which change only through their own checks."),
    ("so another reviewer must approve it (four-eyes rule).", "so a second person must approve it."),
    (" It needs OCR or manual entry.", ""),
    ("Bank details differ from the vendor master", "Bank details differ from those on file"),
    ("Not a supplier in the vendor master", "Not a supplier on file"),
    ("is not in the vendor master", "is not on file"),
    ("the vendor master's ends", "the one on file ends"),
    ("compared with the vendor master", "compared with those on file"),
    ("match the vendor master", "match those on file"),
    ("Payment uses the vendor master.", "Payment uses the details on file."),
    ("with the vendor master", "with the details on file"),
    ("from the vendor master", "on file"),
    ("in the vendor master", "on file"),
    ("the vendor master", "the supplier's file"),
    ("Goods receipts are not checked (2-way match).", "Checked against the PO only."),
    ("(four-eyes rule)", "(a second person must approve)"),
    ("It needs OCR or manual entry.", ""),
]
NO_MASTER = re.compile(r"The vendor master has no (.+?) for ")
DOC_ID = re.compile(r"\bdoc_(\d{2})\b")


def plain(text: str | None) -> str | None:
    """Visitor wording for a product message: no internal ids, no accounts payable jargon."""
    if not text:
        return text
    text = NO_MASTER.sub(r"There is no \1 on file for ", text)
    for old, new in PLAIN:
        text = text.replace(old, new)
    return DOC_ID.sub(lambda m: f"{'purchase order' if m.group(1) == '07' else 'invoice'} {m.group(1)}", text)


def _plain_finding(f: dict[str, Any], vendor: str | None) -> dict[str, Any]:
    """The same finding (same id, so a tick still names it) with a plain message."""
    message = f["message"]
    if f["code"] == "bank_changed" and vendor:
        first = message.startswith("As first read from the PDF: ")
        what = "IBAN" if "the IBAN" in message else "bank account"
        short = vendor.split()[0]
        message = (f"{'As first read from the PDF: ' if first else ''}The {what} on this invoice is not the one on file "
                   f"for {short}. Call {short} on the number on file, not the one on the invoice.")
    return {**f, "message": plain(message), "label": plain(f["label"])}


SYMBOLS = {"GBP": "£", "EUR": "€", "USD": "$"}
STATUS_LABELS = {"needs_review": "To review", "needs_manual_entry": "Needs typing in", "approved": "Approved",
                 "rejected": "Rejected", "exporting": "Exporting", "exported": "Exported"}


def _nice_date(value: str | None) -> str | None:
    try:
        d = date.fromisoformat(value) if value else None
    except ValueError:
        return value
    return f"{d.day} {d:%b %Y}" if d else None


def _shown(kind: str, value: str | None, currency: str | None) -> str | None:
    if value is None:
        return None
    if kind in ("amount", "price"):
        amount = dec(value)
        return f"{SYMBOLS.get(currency or '', '')}{money(amount) if kind == 'amount' and amount is not None else value}"
    if kind == "percent":
        return f"{value}%"
    if kind == "date":
        return _nice_date(value)
    if kind == "iban":
        return iban_display(value)
    if kind == "doc_type":
        return "Invoice" if value == "invoice" else "Purchase order" if value == "purchase_order" else value
    return value


def _clock(stamp: str | None) -> str | None:
    try:
        return datetime.fromisoformat(stamp).strftime("%H:%M") if stamp else None
    except ValueError:
        return None


def _name(label: str | None) -> str | None:
    """'sara (reviewer)' -> 'Sara'."""
    if not label:
        return None
    first = label.split(" (")[0]
    return NAMES.get(first, first)


def _document(r: Replay, doc: dict) -> dict[str, Any]:
    f = doc["fields"]
    waiting = doc["status"] in REVIEWABLE
    findings = check_and_save(r.deps, doc) if waiting else doc["findings"]  # current, as the product shows them
    currency = f.get("currency")
    corrected = set(doc["corrected"])
    evidence = doc.get("evidence") or {}
    doc_type = f.get("doc_type")
    names = FIELDS_BY_TYPE.get(doc_type or "invoice", FIELDS_BY_TYPE["invoice"])
    if doc_type is None or "doc_type" in corrected:  # typed in by a person: the type is one of the values
        names = ("doc_type", *names)
    fields = []
    for name in names:
        spec = HEADER_FIELDS[name]
        fields.append({"name": name, "label": spec.label, "kind": spec.kind, "value": f.get(name),
                       "shown": _shown(spec.kind, f.get(name), currency), "source": evidence.get(name),
                       "corrected": name in corrected, "was": _shown(spec.kind, doc["original_fields"].get(name), currency)
                       if name in corrected else None})
    line_sources = evidence.get("lines") or []
    lines = [{**{k: line.get(k) for k in LINE_FIELDS},
              "shown": {k: _shown(LINE_FIELDS[k].kind, line.get(k), currency) for k in LINE_FIELDS},
              "source": line_sources[i] if i < len(line_sources) else None} for i, line in enumerate(doc["lines"])]
    findings = [_plain_finding(x, f.get("vendor_name")) for x in findings]
    errors = [x for x in findings if x["severity"] == "error"]
    blocking = [x for x in findings if x["severity"] == "blocking"]
    corrections = r.store.corrections(doc["id"])
    correctors = {c["corrected_by"] for c in corrections}
    hard = [x for x in errors if x["code"] in HARD_LOCKS]
    if not waiting:
        stamp = {"approved": "approved", "exporting": "approved", "exported": "exported", "rejected": "rejected"}[doc["status"]]
    else:
        stamp = "locked" if hard else None
    ai = r.ai.get(doc["sha256"])
    ai_view = None
    if ai and "status" in ai:
        ai_view = {"status": ai["status"]}
    elif ai:
        ai_view = {"status": "ok", "model": ai["model"], "ms": ai["ms"], **ai["usage"],
                   "cost_usd": round(cost_usd(ai["model"], ai["usage"]), 6)}
    number = f.get("invoice_number") if doc_type == "invoice" else f.get("po_number")
    sample = sample_by_sha(doc["sha256"])
    return {
        "id": doc["id"], "key": sample.key if sample else None, "file": doc["filename"], "status": doc["status"],
        "status_label": "Typed in, waiting" if doc["status"] == "needs_manual_entry" and corrected
        else STATUS_LABELS.get(doc["status"], doc["status"]),
        "version": doc["version"], "doc_type": doc_type, "number": number, "vendor": f.get("vendor_name"),
        "total": _shown("amount", f.get("total"), currency), "po_number": f.get("po_number"),
        "reader": doc["extractor"], "manual_reason": plain(doc.get("manual_reason")), "waiting": waiting,
        "fields": fields, "lines": lines, "findings": findings,
        "summary": summary(findings),
        "errors": len(errors), "blocking": [x["id"] for x in blocking],
        "locked": [x["label"] for x in errors] if errors else [],
        "hard_lock": bool(hard), "stamp": stamp,
        "four_eyes": sorted(n for n, who in REVIEWERS.items() if who.label in correctors),
        "corrections": [{"field": HEADER_FIELDS[c["field"]].label if c["field"] in HEADER_FIELDS else "Lines",
                         "by": _name(c["corrected_by"]), "at": _clock(c["corrected_at"])} for c in corrections],
        "decided": {"by": _name(doc["decided_by"]), "at": _clock(doc["decided_at"]), "date": "11 Oct 2026",
                    "note": doc["decision_note"],
                    "acknowledged": [next((x["label"] for x in findings if x["code"] == code), code)
                                     for code in doc["acknowledged"]]} if doc["decided_by"] else None,
        "type_in": sample.type_in if sample and doc["status"] == "needs_manual_entry" else None,
        "ai": ai_view,
    }


def _count(n: Any, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _audit_entry(row: dict) -> tuple[str, str]:
    """(who, what they did) in plain words, from one row of the product's audit log."""
    try:
        d = json.loads(row["detail"]) if row["detail"] else {}
    except ValueError:
        d = {}
    d = d if isinstance(d, dict) else {}
    actor, action, outcome = row["actor"], row["action"], row["outcome"]
    who = f"{NAMES.get(actor, actor)} ({row['role']})"
    if actor.startswith("reader:"):
        reader = actor.removeprefix("reader:")
        model = reader.removeprefix("claude:").replace("claude-", "").replace("-", " ", 1).replace("-", ".").title()
        who = {"rules": "Rules reader", "none": "Reader"}.get(reader, f"AI reader, Claude {model}")
        if outcome == "manual":
            return who, f"Could not read it, so it goes to a person: {d.get('problem')}"
        read = f"Read {_count(len(d.get('fields_read') or []), 'value')} and {_count(d.get('lines', 0), 'line')}"
        if d.get("input_tokens") is not None:
            read += f" ({d['input_tokens']:,} tokens in, {d.get('output_tokens') or 0:,} out)"
        return who, read + "."
    if action == "upload":
        if outcome == "duplicate_file":
            return who, f"Uploaded {d.get('file')} again: the same file, recognised by its SHA-256, so not stored twice."
        return who, f"Uploaded {d.get('file')}."
    if action == "check":
        return "Checks", f"{str(d.get('result'))[:1].upper()}{str(d.get('result'))[1:]}."
    if action == "approve":
        if outcome == "refused":
            return who, f"Tried to approve it. Refused: {d.get('reason')}"
        acked = d.get("acknowledged") or []
        count = f"{len(acked)} finding{'s' if len(acked) != 1 else ''}"
        signed_off = f", signing off {count}: “{d.get('note')}”" if acked else ""
        return who, f"Approved it{signed_off}" if acked else "Approved it."
    if action == "reject":
        if outcome == "refused":
            return who, f"Tried to reject it. Refused: {d.get('reason')}"
        return who, f"Rejected it: “{d.get('reason')}”"
    if action == "correct":
        if outcome != "ok":
            return who, f"Tried to change values. Refused: {d.get('reason')}"
        changes = d.get("changes") or []
        return who, f"Typed in or changed {len(changes)} value{'s' if len(changes) != 1 else ''}."
    if action == "export":
        if outcome == "nothing":
            return who, "Ran an export: nothing was waiting."
        return who, f"Exported it (batch {d.get('batch')})."
    if action == "export.batch":
        return who, f"Exported {d.get('documents')} document{'s' if d.get('documents') != 1 else ''} as CSV and JSON."
    return who, f"{action} ({outcome})."


def view(r: Replay) -> dict[str, Any]:
    docs, _ = r.store.list_documents(None, 500)
    documents = [_document(r, doc) for doc in docs]
    labels = {d["id"]: d["number"] or d["file"] for d in documents}
    audit = []
    for row in r.store.audit_entries(300):
        who, what = _audit_entry(row)
        what = plain(what) or ""
        audit.append({"time": _clock(row["ts"]), "actor": row["actor"], "role": row["role"], "action": row["action"],
                      "outcome": row["outcome"], "doc": row["document_id"], "doc_label": labels.get(row["document_id"] or ""),
                      "who": who, "text": what})
    readings = [x for x in r.ai.values() if "status" not in x]
    usage = {k: sum(x["usage"].get(k, 0) for x in readings) for k in ("input_tokens", "output_tokens")}
    return {
        "today": TODAY.isoformat(), "documents": documents, "audit": audit, "exports": r.exports,
        "counts": {"waiting": sum(d["waiting"] for d in documents),
                   "approved": sum(d["status"] in ("approved", "exporting") for d in documents),
                   "exported": sum(d["status"] == "exported" for d in documents),
                   "rejected": sum(d["status"] == "rejected" for d in documents)},
        "metrics": {"ai_reads": len(readings), **usage,
                    "cost_usd": round(sum(cost_usd(x["model"], x["usage"]) for x in readings), 6)},
        "events": r.count,
    }


__all__ = ["SAMPLES", "Sample", "SessionError", "ActionError", "handle", "new_session", "signed", "verified_events",
           "live_reader_from_env", "make_reader", "read_with_ai", "cost_usd"]
