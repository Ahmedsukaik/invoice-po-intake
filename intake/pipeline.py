"""Intake: a PDF arrives (HTTP upload or the inbox folder), is stored, read, checked and queued.

Every document ends in the review queue: `needs_review` when it could be read, or
`needs_manual_entry` when it could not (scanned, password protected, unknown layout, too
slow to parse, or the reader failed). Nothing is approved or exported here.
"""
from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .checks import run_checks, summary
from .config import Identity
from .extract import Extracted, ExtractionFailed, ReaderUnavailable, empty_fields
from .pdftext import PdfText, looks_like_pdf, read_text_isolated, sha256
from .values import clean_text

if TYPE_CHECKING:
    from .deps import Deps

INBOX_IDENTITY = Identity("inbox-watcher", "clerk")


class UploadRefused(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass
class IngestResult:
    document: dict
    created: bool  # False: the same file was already in the system


def display_name(filename: str | None) -> str:
    """A file name for display only. The stored file is always named by its hash, so this
    never becomes a path."""
    name = clean_text(Path(str(filename or "").replace("\\", "/")).name, 120)
    name = re.sub(r"[^\w .()+-]", "_", name).strip(" .")
    return name or "unnamed.pdf"


def check_and_save(deps: "Deps", doc: dict) -> list[dict]:
    findings = [f.to_dict() for f in run_checks(doc, deps.store, deps.policy, deps.today())]
    deps.store.save_findings(doc["id"], findings, doc["version"])
    return findings


def recheck_related(deps: "Deps", doc: dict) -> None:
    """A new or decided document can change another one's findings (a duplicate, a PO now used
    up). Waiting documents with the same number, PO or total are checked again."""
    for other in deps.store.related_waiting(doc):
        check_and_save(deps, other)


def read_pdf(deps: "Deps", data: bytes, defer_temporary: bool) -> tuple[PdfText, Extracted, str | None]:
    """(text layer, what the reader found, why a person must type it in or None)."""
    pdf = read_text_isolated(data, deps.max_pages, deps.pdf_timeout, deps.pdf_workers, deps.pdf_wait)
    if pdf.busy and defer_temporary:
        raise ReaderUnavailable(pdf.problem or "the PDF reader was busy")
    extracted = Extracted(fields=empty_fields(), evidence={"lines": []}, extractor="none")
    manual_reason = pdf.problem
    if not manual_reason:
        try:
            extracted = deps.extractor.extract(data, pdf.pages)
        except ReaderUnavailable as exc:
            if defer_temporary:
                raise
            manual_reason = f"{exc}"
        except ExtractionFailed as exc:
            manual_reason = f"The reader could not read it: {exc}"
        except Exception as exc:  # the document still reaches a person
            manual_reason = f"The reader failed ({type(exc).__name__}). Enter the fields by hand, or use 'Read the PDF again'."
        else:
            if extracted.fields.get("doc_type") is None:
                manual_reason = "The reader could not tell what kind of document this is."
    return pdf, extracted, manual_reason


def ingest(deps: "Deps", data: bytes, filename: str | None, source: str, who: Identity,
           defer_temporary: bool = False) -> IngestResult:
    """defer_temporary: raise ReaderUnavailable (and store nothing) when the reader is only
    temporarily unavailable, so the inbox can try the file again on its next scan."""
    name = display_name(filename)
    store = deps.store
    refused = None
    if not data:
        refused = UploadRefused(400, "The file is empty.")
    elif len(data) > deps.max_upload_bytes:
        refused = UploadRefused(413, f"The file is larger than {deps.max_upload_bytes // (1024 * 1024)} MB.")
    elif not looks_like_pdf(data):
        refused = UploadRefused(415, "The file is not a PDF.")
    if refused:
        store.audit(who.name, who.role, "upload", "refused", detail={"file": name, "source": source, "reason": refused.message})
        raise refused

    digest = sha256(data)
    existing = store.by_hash(digest)
    if existing:
        store.audit(who.name, who.role, "upload", "duplicate_file", existing["id"],
                    {"file": name, "source": source, "sha256": digest})
        return IngestResult(existing, False)

    pdf, extracted, manual_reason = read_pdf(deps, data, defer_temporary)
    path = deps.pdf_path(digest)
    if not path.exists():
        tmp = path.with_suffix(f".{os.getpid()}.{time.monotonic_ns()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    status = "needs_manual_entry" if manual_reason else "needs_review"

    doc, created = store.add_document(
        sha256=digest, filename=name, size=len(data), source=source, status=status, fields=extracted.fields,
        evidence=extracted.evidence, lines=extracted.lines, notes=extracted.notes, extractor=extracted.extractor,
        page_count=pdf.page_count, text_layer=pdf.text[:200_000], manual_reason=manual_reason, uploaded_by=who.label,
    )
    if not created:  # the same file arrived twice at the same moment; the other upload won
        store.audit(who.name, who.role, "upload", "duplicate_file", doc["id"], {"file": name, "source": source})
        return IngestResult(doc, False)
    store.audit(who.name, who.role, "upload", "ok", doc["id"],
                {"file": name, "source": source, "size": len(data), "sha256": digest})
    store.audit(f"reader:{extracted.extractor}", "system", "extract", "manual" if manual_reason else "ok", doc["id"],
                {"fields_read": sorted(k for k, v in extracted.fields.items() if v), "lines": len(extracted.lines),
                 "notes": len(extracted.notes), "problem": manual_reason, **extracted.usage})
    findings = check_and_save(deps, doc)
    store.audit("checks", "system", "check", "ok", doc["id"], {"result": summary(findings)})
    recheck_related(deps, doc)
    return IngestResult(store.require(doc["id"]), True)


@dataclass
class InboxItem:
    file: str
    document_id: str | None
    status: str | None
    created: bool
    error: str | None = None


def scan_inbox(deps: "Deps", min_age_seconds: float = 0.0, who: Identity = INBOX_IDENTITY) -> list[InboxItem]:
    """Process every *.pdf in the inbox folder once. Read files move to inbox/processed/,
    refused ones to inbox/failed/ with a .reason.txt next to them. Files changed less than
    min_age_seconds ago are left for the next scan (they may still be copying), and so are
    files the reader could not take right now (rate limit, network, every PDF worker busy).
    Files are read in parallel, as many at a time as there are PDF workers, so one slow PDF
    does not hold up the others."""
    inbox = Path(deps.inbox_dir)
    processed, failed = inbox / "processed", inbox / "failed"
    processed.mkdir(parents=True, exist_ok=True)
    failed.mkdir(parents=True, exist_ok=True)

    def refuse(path: Path, name: str, reason: str) -> InboxItem:
        _move(path, failed, name)
        (failed / f"{name}.reason.txt").write_text(reason + "\n", encoding="utf-8")
        return InboxItem(name, None, None, False, reason)

    def take(path: Path) -> InboxItem | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        if min_age_seconds and time.time() - stat.st_mtime < min_age_seconds:
            return None
        name = display_name(path.name)
        if path.suffix.lower() != ".pdf":
            return refuse(path, name, "Only .pdf files are read from the inbox.")
        if stat.st_size > deps.max_upload_bytes:  # checked before reading the file into memory
            return refuse(path, name, f"The file is larger than {deps.max_upload_bytes // (1024 * 1024)} MB.")
        try:
            data = path.read_bytes()
        except OSError:  # gone or locked (another scanner took it, or it is still being copied): next scan
            return None
        try:
            result = ingest(deps, data, path.name, "inbox", who, defer_temporary=True)
        except UploadRefused as exc:
            return refuse(path, name, exc.message)
        except ReaderUnavailable as exc:
            return InboxItem(name, None, None, False, f"left in the inbox for the next scan: {exc}")
        _move(path, processed, f"{result.document['id']}-{name}")
        return InboxItem(name, result.document["id"], result.document["status"], result.created)

    paths = [p for p in sorted(inbox.iterdir())
             if not p.is_symlink() and p.is_file() and not p.name.startswith((".", "~"))]
    with ThreadPoolExecutor(max_workers=max(1, min(deps.pdf_workers, len(paths) or 1))) as pool:
        return [item for item in pool.map(take, paths) if item is not None]


def _move(path: Path, folder: Path, name: str) -> None:
    target = folder / name
    if target.exists():
        target = folder / f"{time.time_ns()}-{name}"
    try:
        os.replace(path, target)
    except FileNotFoundError:  # another scanner moved it first
        pass
