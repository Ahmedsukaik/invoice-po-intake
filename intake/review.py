"""What a reviewer can do: correct fields, re-run checks, read again, approve, reject.

Rules enforced here (and nowhere else):
- Only documents in needs_review or needs_manual_entry can be corrected, approved or rejected.
- Every correction is stored with the old and the new value, who and when.
- A value read from the PDF can be changed but not removed. Changing it is itself a blocking
  finding (value_corrected), so a correction cannot quietly make a problem disappear.
- Bank details and the document type read from the PDF cannot be edited at all. Typed-in
  bank details are a blocking finding (bank_typed).
- An error that no correction can lift (checks.PERMANENT: changed bank details, an unknown
  vendor, a copy of an approved invoice...) found on the values as first read stays in force,
  whatever is corrected later.
- Four eyes: whoever typed in or changed any value on a document (a field, including one the
  reader left empty, or a line) may not approve it. Another reviewer approves.
- Approval re-runs every check on the current values, holding the database write lock, so
  nothing (another approval on the same PO, a correction) can change the picture between the
  checks and the approval. Errors stop it. Each blocking finding must be acknowledged by its
  fingerprint, with a note saying why; if the finding changed since, the old acknowledgement
  does not count.
- If the document changed after the reviewer opened it, the approval is refused. Every
  refusal is written to the audit log.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .checks import match_vendor, run_checks, summary
from .config import Identity
from .pipeline import check_and_save, read_pdf, recheck_related
from .store import REVIEWABLE, Conflict, NotFound
from .values import BANK_FIELDS, HEADER_FIELDS, MAX_LINES, clean_text, normalize, normalize_line

if TYPE_CHECKING:
    from .deps import Deps

class ActionRefused(Exception):
    def __init__(self, status_code: int, message: str, findings: list[dict] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.findings = findings or []


def _load(deps: "Deps", doc_id: str, seen_version: int | None = None) -> dict:
    doc = deps.store.get(doc_id)
    if doc is None:
        raise ActionRefused(404, f"No document {clean_text(doc_id, 40)}.")
    if doc["status"] not in REVIEWABLE:
        raise ActionRefused(409, f"Document {doc_id} is already {doc['status']}.")
    if seen_version is not None and seen_version != doc["version"]:
        raise ActionRefused(409, "The document changed after you opened it. Open it again and review the current values.")
    return doc


def recheck(deps: "Deps", doc_id: str, who: Identity) -> dict:
    doc = deps.store.get(doc_id)
    if doc is None:
        raise ActionRefused(404, f"No document {clean_text(doc_id, 40)}.")
    findings = check_and_save(deps, doc) if doc["status"] in REVIEWABLE else doc["findings"]
    deps.store.audit(who.name, who.role, "recheck", "ok", doc_id, {"result": summary(findings)})
    return deps.store.require(doc_id)


def read_again(deps: "Deps", doc_id: str, who: Identity) -> dict:
    """Run the reader again on the stored PDF (after the reader was unavailable, for example).
    Only for a document still waiting that nobody has corrected, so no typed value is lost."""
    doc = _load(deps, doc_id)
    if doc["corrected"]:
        raise ActionRefused(409, "Someone has already corrected this document, so it is not read again.")
    data = deps.pdf_path(doc["sha256"]).read_bytes()
    pdf, extracted, manual_reason = read_pdf(deps, data, defer_temporary=False)
    try:
        updated = deps.store.reread(doc_id, doc["version"], status="needs_manual_entry" if manual_reason else "needs_review",
                                    fields=extracted.fields, evidence=extracted.evidence, lines=extracted.lines,
                                    notes=extracted.notes, extractor=extracted.extractor, manual_reason=manual_reason)
    except Conflict as exc:
        raise ActionRefused(409, str(exc)) from None
    deps.store.audit(who.name, who.role, "read_again", "manual" if manual_reason else "ok", doc_id,
                     {"reader": extracted.extractor, "problem": manual_reason, **extracted.usage})
    findings = check_and_save(deps, updated)
    deps.store.audit("checks", "system", "check", "ok", doc_id, {"result": summary(findings)})
    recheck_related(deps, updated)
    return deps.store.require(doc_id)


def _planned_changes(doc: dict, fields: dict[str, Any] | None, lines: list[Any] | None
                     ) -> tuple[dict, list, list[tuple[str, Any, Any]]]:
    """Parse the requested corrections. Returns (new fields, new lines, changes) or raises ActionRefused."""
    new_fields = dict(doc["fields"])
    changes: list[tuple[str, Any, Any]] = []
    for name, raw in (fields or {}).items():
        spec = HEADER_FIELDS.get(str(name))
        if spec is None:
            raise ActionRefused(400, f"Unknown field '{clean_text(name, 40)}'.")
        try:
            value = normalize(spec.kind, raw, spec.label)
        except ValueError as exc:
            raise ActionRefused(400, str(exc)) from None
        if value == new_fields.get(name):
            continue
        read_from_pdf = doc["original_fields"].get(name)
        if name == "doc_type" and read_from_pdf:
            raise ActionRefused(400, "The document type read from the PDF cannot be changed here. If it was read "
                                     "wrongly, reject the document and send it again.")
        if name in BANK_FIELDS and read_from_pdf:
            raise ActionRefused(400, "Bank details read from the PDF cannot be changed here. They come from the vendor "
                                     "master; a change there goes through its own controlled process.")
        if value is None and read_from_pdf is not None:
            raise ActionRefused(400, f"{spec.label} was read from the PDF and can be corrected, but not removed. "
                                     "If it does not belong on this document, reject the document.")
        changes.append((name, new_fields.get(name), value))
        new_fields[name] = value
    new_lines = doc["lines"]
    if lines is not None:
        if not isinstance(lines, list) or len(lines) > MAX_LINES:
            raise ActionRefused(400, f"lines must be a list of at most {MAX_LINES} items.")
        parsed = []
        for number, raw in enumerate(lines, 1):
            try:
                line = normalize_line(raw)
            except ValueError as exc:
                raise ActionRefused(400, f"Line {number}: {exc}") from None
            if any(v is not None for v in line.values()):  # an all-empty row is a removed line
                parsed.append(line)
        if parsed != doc["lines"]:
            changes.append(("lines", doc["lines"], parsed))
            new_lines = parsed
    return new_fields, new_lines, changes


def correct(deps: "Deps", doc_id: str, who: Identity, fields: dict[str, Any] | None = None,
            lines: list[Any] | None = None, note: str | None = None, seen_version: int | None = None) -> dict:
    try:
        doc = _load(deps, doc_id, seen_version)
        new_fields, new_lines, changes = _planned_changes(doc, fields, lines)
    except ActionRefused as exc:
        if exc.status_code != 404:
            deps.store.audit(who.name, who.role, "correct", "refused", doc_id, {"reason": exc.message,
                                                                                "fields": sorted(fields or {})})
        raise
    if not changes:
        return doc
    note = clean_text(note, 300) or None
    try:
        updated = deps.store.correct(doc_id, doc["version"], new_fields, new_lines, changes, who.label, note)
    except Conflict as exc:
        deps.store.audit(who.name, who.role, "correct", "conflict", doc_id, {"reason": str(exc)})
        raise ActionRefused(409, str(exc)) from None
    deps.store.audit(who.name, who.role, "correct", "ok", doc_id,
                     {"changes": [{"field": f, "old": o, "new": n} for f, o, n in changes], "note": note})
    findings = check_and_save(deps, updated)
    deps.store.audit("checks", "system", "check", "ok", doc_id, {"result": summary(findings)})
    recheck_related(deps, updated)
    return deps.store.require(doc_id)


def approve(deps: "Deps", doc_id: str, who: Identity, acknowledge: list[str] | None = None, note: str | None = None,
            seen_version: int | None = None) -> dict:
    """acknowledge: the fingerprints ("id" of each finding) of the blocking findings the
    reviewer checked. A finding whose text changed has a new fingerprint and must be checked again."""
    doc = _load(deps, doc_id, seen_version)
    note = clean_text(note, 500)
    acknowledged = {clean_text(c, 40) for c in (acknowledge or [])}
    findings: list[dict] = []
    blocking: list[dict] = []
    correctors = {c["corrected_by"] for c in deps.store.corrections(doc_id)}
    try:
        # The final checks and the approval happen under the write lock: two approvals against the
        # same PO, or a correction, cannot slip in between.
        with deps.store.exclusive() as conn:
            current = deps.store.require(doc_id)
            if current["status"] not in REVIEWABLE:
                raise ActionRefused(409, f"Document {doc_id} is already {current['status']}.")
            if current["version"] != doc["version"]:
                raise ActionRefused(409, "The document changed after you opened it. Open it again and review the "
                                         "current values.")
            findings = [f.to_dict() for f in run_checks(current, deps.store, deps.policy, deps.today())]
            errors = [f for f in findings if f["severity"] == "error"]
            if errors:
                raise ActionRefused(409, "This document has errors that must be fixed first; it cannot be approved "
                                         "as it is.", errors)
            if deps.policy.four_eyes and who.label in correctors:
                raise ActionRefused(409, "You typed in or changed values on this document, so another reviewer must "
                                         "approve it (four-eyes rule).")
            blocking = [f for f in findings if f["severity"] == "blocking"]
            missing = [f for f in blocking if f["id"] not in acknowledged]
            if missing:
                raise ActionRefused(409, "Each blocking finding must be checked and ticked before approving: "
                                         + "; ".join(f["label"] for f in missing) + ".", missing)
            if blocking and len(note) < 10:
                raise ActionRefused(400, "Approving despite blocking findings needs a note saying why "
                                         "(at least 10 characters).")
            vendor = match_vendor(current["fields"].get("vendor_name"), deps.store.vendors())
            approved = deps.store.approve(doc_id, current["version"], who.label, note or None,
                                          sorted({f["code"] for f in blocking}), findings,
                                          vendor["id"] if vendor else None, conn=conn)
    except (ActionRefused, Conflict, NotFound) as exc:
        refusal = exc if isinstance(exc, ActionRefused) else ActionRefused(
            404 if isinstance(exc, NotFound) else 409, str(exc).strip("'"))
        if findings:
            deps.store.save_findings(doc_id, findings, doc["version"])
        deps.store.audit(who.name, who.role, "approve", "refused", doc_id,
                         {"reason": refusal.message, "findings": [f["id"] for f in refusal.findings]})
        raise refusal from None
    deps.store.audit(who.name, who.role, "approve", "ok", doc_id,
                     {"acknowledged": [{"id": f["id"], "code": f["code"]} for f in blocking], "note": note or None,
                      "result": summary(findings)})
    recheck_related(deps, approved)
    return approved


def reject(deps: "Deps", doc_id: str, who: Identity, reason: str | None) -> dict:
    reason = clean_text(reason, 500)
    if len(reason) < 3:
        deps.store.audit(who.name, who.role, "reject", "refused", doc_id if deps.store.get(doc_id) else None,
                         {"reason": "no reason given"})
        raise ActionRefused(400, "A rejection needs a reason.")
    try:
        doc = deps.store.reject(doc_id, who.label, reason)
    except NotFound:
        raise ActionRefused(404, f"No document {clean_text(doc_id, 40)}.") from None
    except Conflict as exc:
        deps.store.audit(who.name, who.role, "reject", "refused", doc_id, {"reason": str(exc)})
        raise ActionRefused(409, str(exc)) from None
    deps.store.audit(who.name, who.role, "reject", "ok", doc_id, {"reason": reason})
    recheck_related(deps, doc)
    return doc
