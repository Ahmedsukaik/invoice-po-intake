"""The HTTP server: a JSON API (bearer tokens) and a reviewer web page (sign in with a token).

Rules
-----
1. Every /api/* request needs a known bearer token. A middleware checks it, and caps the
   request size, before any body is read. The web page exchanges a token once, on a sign-in
   form paired with the browser, for a session cookie.
2. The role is checked on every route, before the request body is parsed. Refusals of
   signed-in users are written to the audit log; unknown tokens go to the server log.
3. Web forms carry a CSRF token; the cookie is HttpOnly and SameSite=Strict (and Secure
   behind HTTPS). Web actions redirect after POST.
4. Text from PDFs is escaped in the web pages (and the CLI prints it on one line, without
   control characters). Pages allow no scripts at all (Content-Security-Policy).

Run:  python -m intake --port 8200
"""
from __future__ import annotations

import json
import logging
import re
import secrets
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import review
from .checks import CODE_LABELS, SEVERITIES, run_checks, summary
from .config import Identity, Permission, digest
from .deps import Deps
from .export import BATCH_ID, ExportBusy, resolve_batch, run_export
from .pipeline import UploadRefused, ingest, scan_inbox
from .review import ActionRefused
from .store import REVIEWABLE, STATUSES, now
from .values import (BANK_FIELDS, FIELDS_BY_TYPE, HEADER_FIELDS, LINE_FIELDS, dec, iban_display, money,
                     q2_or_none)

log = logging.getLogger("intake")
HERE = Path(__file__).resolve().parent
COOKIE = "intake_session"
LOGIN_COOKIE = "intake_signin"  # pairs the sign-in form with this browser (sign-in CSRF)
DOC_ID = r"^doc_[0-9a-f]{12}$"
API_JSON_LIMIT = 1024 * 1024
PAGE_SIZE = 100
STATUS_LABELS = {
    "needs_review": "Needs review", "needs_manual_entry": "Needs manual entry", "approved": "Approved",
    "rejected": "Rejected", "exporting": "Exporting", "exported": "Exported",
}
TABS = [("open", "To review"), ("approved", "Approved"), ("exported", "Exported"), ("rejected", "Rejected"),
        ("all", "All")]
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'none'; "
                               "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
# The original PDF is shown by the browser's own PDF viewer, which a page policy with
# object-src 'none' would block. It gets a narrower set of headers instead.
PDF_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
               "Content-Security-Policy": "frame-ancestors 'none'"}
FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" '
           'fill="#0f766e"/><text x="16" y="21" font-family="Arial" font-size="13" font-weight="700" fill="#fff" '
           'text-anchor="middle">IP</text></svg>')


class CorrectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: dict[str, str | None] = Field(default_factory=dict, max_length=30)
    lines: list[dict[str, str | None]] | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=1000)
    version: int  # the version the caller looked at; a changed document is refused


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledge: list[str] = Field(default_factory=list, max_length=100)  # finding fingerprints ("id")
    note: str | None = Field(default=None, max_length=1000)
    version: int  # the version the caller looked at; a changed document is refused


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    written: bool


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=1000)


# --------------------------------------------------------------------------- view helpers
def doc_summary(doc: dict) -> dict[str, Any]:
    f = doc["fields"]
    number = f.get("invoice_number") if f.get("doc_type") == "invoice" else f.get("po_number")
    counts = {s: sum(1 for x in doc["findings"] if x["severity"] == s) for s in SEVERITIES}
    return {
        "id": doc["id"], "filename": doc["filename"], "status": doc["status"], "doc_type": f.get("doc_type"),
        "vendor_name": f.get("vendor_name"), "number": number, "currency": f.get("currency"), "total": f.get("total"),
        "findings": counts, "checks": summary(doc["findings"]), "uploaded_by": doc["uploaded_by"],
        "uploaded_at": doc["uploaded_at"], "version": doc["version"],
    }


def mask(value: str | None) -> str | None:
    return None if not value else "*" * max(0, len(value) - 4) + value[-4:]


def sees_bank_details(who: Identity) -> bool:
    """Reviewers and admins see bank details in full; clerks only their last four characters."""
    return who.can("review") or who.can("audit")


def for_viewer(doc: dict, who: Identity) -> dict:
    """The document as this person may see it. For clerks: bank details masked in the fields, in
    their evidence and in corrections, and no text layer (it holds the bank details in full).
    Findings never spell out the vendor master's bank details, for anyone (see checks._last4)."""
    if sees_bank_details(who):
        return doc
    out = dict(doc)
    for key in ("fields", "original_fields"):
        out[key] = {k: (mask(v) if k in BANK_FIELDS else v) for k, v in (doc.get(key) or {}).items()}
    out["evidence"] = {k: ({**v, "text": "(shown to reviewers only)"} if k in BANK_FIELDS and isinstance(v, dict) else v)
                       for k, v in (doc.get("evidence") or {}).items()}
    out["text_layer"] = ""
    return out


def corrections_for(deps: Deps, doc_id: str, who: Identity) -> list[dict]:
    rows = deps.store.corrections(doc_id)
    if sees_bank_details(who):
        return rows
    return [{**c, "old_value": mask(c["old_value"]), "new_value": mask(c["new_value"])} if c["field"] in BANK_FIELDS
            else c for c in rows]


def doc_detail(deps: Deps, doc: dict, who: Identity, with_text: bool = False) -> dict[str, Any]:
    out = {k: v for k, v in for_viewer(doc, who).items() if k != "text_layer"}
    if with_text and sees_bank_details(who):
        out["text_layer"] = (deps.store.get(doc["id"], with_text=True) or {}).get("text_layer")
    out["corrections"] = corrections_for(deps, doc["id"], who)
    return out


def line_rows(doc: dict, tolerance: Decimal) -> list[dict[str, Any]]:
    rows = []
    evidence = doc["evidence"].get("lines") or []
    for i, line in enumerate(doc["lines"]):
        qty, price, amount = dec(line.get("quantity")), dec(line.get("unit_price")), dec(line.get("line_total"))
        expected = q2_or_none(qty * price) if qty is not None and price is not None else None
        ok = expected is not None and amount is not None and abs(expected - amount) <= tolerance
        rows.append({**line, "n": i + 1, "expected": expected, "ok": ok,
                     "evidence": evidence[i] if i < len(evidence) else None})
    return rows


def when(ts: str | None) -> str:
    return (ts or "").replace("T", " ").replace("+00:00", " UTC")


def make_env() -> Environment:
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(default=True))
    env.filters["money"] = lambda v: money(dec(v)) if dec(v) is not None else ""
    env.filters["when"] = when
    env.filters["iban"] = iban_display
    env.globals.update(STATUS_LABELS=STATUS_LABELS, HEADER_FIELDS=HEADER_FIELDS, LINE_FIELDS=LINE_FIELDS,
                       FIELDS_BY_TYPE=FIELDS_BY_TYPE, TABS=TABS, CODE_LABELS=CODE_LABELS, BANK_FIELDS=BANK_FIELDS)
    return env


def statuses_for(tab: str) -> tuple[str, ...] | None:
    if tab == "open":
        return REVIEWABLE
    if tab == "all":
        return None
    if tab not in STATUSES:
        raise HTTPException(400, f"status must be one of: open, all, {', '.join(STATUSES)}")
    return (tab,)


# --------------------------------------------------------------------------- the app
def build_app(deps: Deps, allowed_hosts: list[str] | None = None, secure_cookies: bool = False) -> FastAPI:
    """secure_cookies: mark cookies Secure even when the server itself sees plain http
    (it does behind a TLS proxy). __main__ turns it on for any non-loopback address."""
    app = FastAPI(title="Invoice and PO intake", docs_url=None, redoc_url=None, openapi_url=None)
    if allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    env = make_env()
    store = deps.store

    @app.middleware("http")
    async def gate(request: Request, call_next: Callable) -> Response:
        """/api/*: a valid token and a bounded body, checked before anything is read."""
        path = request.url.path
        if path.startswith("/api/"):
            who = deps.tokens.identify(request.headers.get("authorization"))
            if who is None:
                log.warning("refused %s %r: missing or invalid bearer token", request.method, path[:200])
                return JSONResponse({"error": "missing or invalid bearer token"}, status_code=401)
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                limit = deps.max_upload_bytes + 64 * 1024 if path == "/api/documents" else API_JSON_LIMIT
                raw = request.headers.get("content-length")
                if raw is None and request.headers.get("transfer-encoding"):
                    return JSONResponse({"error": "Send the request with a Content-Length header."}, status_code=411)
                if raw is not None and (not raw.isascii() or not raw.isdecimal() or int(raw) > limit):
                    return JSONResponse({"error": "The request is too large."}, status_code=413)
            request.state.who = who
        response = await call_next(request)
        for name, value in (PDF_HEADERS if response.headers.get("content-type") == "application/pdf"
                            else SECURITY_HEADERS).items():
            response.headers.setdefault(name, value)
        return response

    # ---- errors: JSON for the API, a page for the browser ----------------------------
    def is_api(request: Request) -> bool:
        return request.url.path.startswith("/api/")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if is_api(request) or request.url.path == "/healthz":
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))
        if exc.status_code == 401:
            target = request.url.path if request.method == "GET" else "/"
            return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
        return page(request, "error.html", {"message": exc.detail}, web_session(request), exc.status_code)

    @app.exception_handler(ActionRefused)
    async def refused(request: Request, exc: ActionRefused) -> Response:
        return JSONResponse({"error": exc.message, "findings": exc.findings}, status_code=exc.status_code)

    @app.exception_handler(ExportBusy)
    async def busy(request: Request, exc: ExportBusy) -> Response:
        if is_api(request):
            return JSONResponse({"error": str(exc)}, status_code=409)
        return page(request, "error.html", {"message": str(exc)}, web_session(request), 409)

    # ---- identity ------------------------------------------------------------------------
    def api_who(request: Request) -> Identity:
        return request.state.who  # set by the gate; /api/* never runs without it

    def web_session(request: Request) -> tuple[Identity, str] | None:
        found = store.session(request.cookies.get(COOKIE))
        if not found:
            return None
        who, csrf, token_digest = found
        if deps.tokens.by_digest(token_digest) != who:  # the token was removed or its role changed
            return None
        return who, csrf

    def web_identity(request: Request) -> tuple[Identity, str]:
        found = web_session(request)
        if found is None:
            raise HTTPException(401, "sign in first")
        return found

    def need(who: Identity, permission: Permission | tuple[Permission, ...], action: str,
             doc_id: str | None = None) -> None:
        allowed = (permission,) if isinstance(permission, str) else permission
        if not any(who.can(p) for p in allowed):
            known = doc_id if doc_id and re.fullmatch(DOC_ID, doc_id) else None
            store.audit(who.name, who.role, action, "denied", known, {"needs": "/".join(allowed)})
            raise HTTPException(403, f"role '{who.role}' cannot {action.replace('_', ' ')}")

    async def read_json(request: Request, model: type[BaseModel]) -> Any:
        """The body is parsed only after the caller's role was checked."""
        raw = await request.body()
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise HTTPException(400, "invalid request: the body is not valid JSON") from None
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ())) or "body"
            raise HTTPException(400, f"invalid request: {where}: {first.get('msg', 'invalid')}") from None

    async def form_with_csrf(request: Request, me: tuple[Identity, str]) -> Any:
        form = await request.form()
        sent = str(form.get("csrf") or "")
        if not sent or not secrets.compare_digest(sent, me[1]):
            store.audit(me[0].name, me[0].role, "csrf", "denied", None, {"path": request.url.path[:100]})
            raise HTTPException(403, "The form expired or did not come from this site. Reload the page and try again.")
        return form

    def fresh(doc: dict) -> dict:
        """A waiting document with its findings as of now, the same for the web page, the API and
        the command line. They are written back only when they changed, and never for a document
        that was decided meanwhile (save_findings checks the status and the version)."""
        if doc["status"] not in REVIEWABLE:
            return doc
        findings = [f.to_dict() for f in run_checks(doc, store, deps.policy, deps.today())]
        if findings != doc["findings"]:
            store.save_findings(doc["id"], findings, doc["version"])
        return {**doc, "findings": findings, "checked_at": now()}

    def get_doc(doc_id: str, with_text: bool = False) -> dict:
        doc = store.get(doc_id, with_text=with_text) if re.fullmatch(DOC_ID, doc_id) else None
        if doc is None:
            raise HTTPException(404, "no such document")
        return doc

    async def read_upload(upload: Any) -> tuple[bytes, str]:
        if not isinstance(upload, UploadFile):
            raise HTTPException(400, "send the PDF as a multipart form field named 'file'")
        data = await upload.read(deps.max_upload_bytes + 1)
        return data, upload.filename or "upload.pdf"

    def check_length(request: Request) -> None:
        """Web uploads must declare their size too, so an endless body is refused before it is read."""
        raw = request.headers.get("content-length")
        if raw is None:
            raise HTTPException(411, "Send the upload with a Content-Length header.")
        if not raw.isascii() or not raw.isdecimal() or int(raw) > deps.max_upload_bytes + 64 * 1024:
            raise HTTPException(413, "The file is too large.")

    def page(request: Request, template: str, ctx: dict[str, Any], who: tuple[Identity, str] | None,
             status_code: int = 200) -> HTMLResponse:
        identity, csrf = who if who else (None, "")
        html = env.get_template(template).render(request=request, me=identity, csrf=csrf, **ctx)
        return HTMLResponse(html, status_code=status_code)

    def pdf_response(doc: dict) -> Response:
        path = deps.pdf_path(doc["sha256"])
        if not path.is_file():
            raise HTTPException(404, "the stored PDF is missing")
        return FileResponse(path, media_type="application/pdf", content_disposition_type="inline",
                            filename=doc["filename"] if doc["filename"].lower().endswith(".pdf") else "document.pdf")

    def export_file(batch_id: str, name: str) -> Response:
        batch = store.export_batch(batch_id) if BATCH_ID.fullmatch(batch_id) else None
        if batch is None or name not in batch["files"]:
            raise HTTPException(404, "no such export file")
        path = Path(deps.outbox_dir) / batch_id / name
        if not path.is_file():
            raise HTTPException(404, "the export file is no longer in the outbox")
        media = "text/csv" if name.endswith(".csv") else "application/json" if name.endswith(".json") else \
            "application/octet-stream"
        return FileResponse(path, media_type=media, filename=f"{batch_id}-{name}")

    def export_or_explain(who: Identity) -> dict | None:
        try:
            return run_export(deps, who)
        except ExportBusy:
            raise
        except Exception as exc:  # the exporter failed; run_export has settled the batch and logged it
            log.exception("export failed")
            raise HTTPException(502, f"The export failed ({type(exc).__name__}). Nothing was marked exported unless "
                                     "the exporter could confirm it; see the audit log. A batch left in doubt is "
                                     "settled with resolve-export.") from None

    # ======================================================================= JSON API
    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(FAVICON, media_type="image/svg+xml")

    @app.get("/api/documents")
    async def api_list(request: Request, status: str = "open", limit: int = PAGE_SIZE, offset: int = 0) -> dict:
        who = api_who(request)
        need(who, "view", "list_documents")
        docs, total = await run_in_threadpool(store.list_documents, statuses_for(status), limit, offset)
        return {"documents": [doc_summary(d) for d in docs], "counts": store.counts(), "total": total,
                "limit": max(1, min(limit, 500)), "offset": max(0, offset)}

    @app.post("/api/documents", status_code=201)
    async def api_upload(request: Request, response: Response) -> dict:
        who = api_who(request)
        need(who, "upload", "upload")
        form = await request.form()
        data, name = await read_upload(form.get("file"))
        try:
            result = await run_in_threadpool(ingest, deps, data, name, "upload", who)
        except UploadRefused as exc:
            raise HTTPException(exc.status_code, exc.message) from None
        if not result.created:
            response.status_code = 200
        return {"created": result.created, "document": doc_detail(deps, result.document, who)}

    @app.get("/api/documents/{doc_id}")
    async def api_get(request: Request, doc_id: str, text: bool = False) -> dict:
        who = api_who(request)
        need(who, "view", "view_document", doc_id)
        return doc_detail(deps, await run_in_threadpool(fresh, get_doc(doc_id)), who, with_text=text)

    @app.get("/api/documents/{doc_id}/pdf")
    async def api_pdf(request: Request, doc_id: str) -> Response:
        who = api_who(request)
        need(who, "view", "view_pdf", doc_id)
        return pdf_response(get_doc(doc_id))

    @app.post("/api/documents/{doc_id}/corrections")
    async def api_correct(request: Request, doc_id: str) -> dict:
        who = api_who(request)
        need(who, "review", "correct", doc_id)
        body = await read_json(request, CorrectionBody)
        get_doc(doc_id)
        doc = await run_in_threadpool(review.correct, deps, doc_id, who, body.fields, body.lines, body.note,
                                      body.version)
        return doc_detail(deps, doc, who)

    @app.post("/api/documents/{doc_id}/recheck")
    async def api_recheck(request: Request, doc_id: str) -> dict:
        who = api_who(request)
        need(who, "review", "recheck", doc_id)
        get_doc(doc_id)
        return doc_detail(deps, await run_in_threadpool(review.recheck, deps, doc_id, who), who)

    @app.post("/api/documents/{doc_id}/read-again")
    async def api_read_again(request: Request, doc_id: str) -> dict:
        who = api_who(request)
        need(who, ("upload", "review"), "read_again", doc_id)
        get_doc(doc_id)
        return doc_detail(deps, await run_in_threadpool(review.read_again, deps, doc_id, who), who)

    @app.post("/api/documents/{doc_id}/approve")
    async def api_approve(request: Request, doc_id: str) -> dict:
        who = api_who(request)
        need(who, "review", "approve", doc_id)
        body = await read_json(request, ApproveBody)
        get_doc(doc_id)
        return doc_detail(deps, await run_in_threadpool(review.approve, deps, doc_id, who, body.acknowledge,
                                                        body.note, body.version), who)

    @app.post("/api/documents/{doc_id}/reject")
    async def api_reject(request: Request, doc_id: str) -> dict:
        who = api_who(request)
        need(who, "review", "reject", doc_id)
        body = await read_json(request, RejectBody)
        get_doc(doc_id)
        return doc_detail(deps, await run_in_threadpool(review.reject, deps, doc_id, who, body.reason), who)

    @app.post("/api/exports")
    async def api_export(request: Request) -> dict:
        who = api_who(request)
        need(who, "export", "export")
        batch = await run_in_threadpool(export_or_explain, who)
        return {"batch": batch, "message": None if batch else "Nothing approved is waiting for export."}

    @app.post("/api/exports/{batch_id}/resolve")
    async def api_resolve(request: Request, batch_id: str) -> dict:
        who = api_who(request)
        need(who, "export", "resolve_export")
        body = await read_json(request, ResolveBody)
        try:
            outcome = await run_in_threadpool(resolve_batch, deps, who, batch_id, body.written)
        except KeyError as exc:
            raise HTTPException(404, exc.args[0] if exc.args else "no such batch") from None
        return {"batch_id": batch_id, "resolved": outcome}

    @app.get("/api/exports")
    async def api_exports(request: Request) -> dict:
        who = api_who(request)
        need(who, "export", "list_exports")
        return {"exports": store.exports(), "outbox": str(Path(deps.outbox_dir).resolve())}

    @app.get("/api/exports/{batch_id}/{name}")
    async def api_export_file(request: Request, batch_id: str, name: str) -> Response:
        who = api_who(request)
        need(who, "export", "download_export")
        return export_file(batch_id, name)

    @app.get("/api/audit")
    async def api_audit(request: Request, limit: int = 50, document_id: str | None = None) -> dict:
        who = api_who(request)
        need(who, "audit", "read_audit")
        return {"entries": store.audit_entries(limit, document_id)}

    @app.post("/api/inbox/scan")
    async def api_scan(request: Request) -> dict:
        who = api_who(request)
        need(who, "upload", "scan_inbox")
        return {"items": [vars(i) for i in await run_in_threadpool(scan_inbox, deps)]}

    @app.get("/api/vendors")
    async def api_vendors(request: Request) -> dict:
        who = api_who(request)
        need(who, "view", "list_vendors")
        vendors = store.vendors()
        if not (who.can("review") or who.can("audit")):  # clerks see only the last four characters
            for v in vendors:
                v.update(iban=mask(v["iban"]), sort_code=mask(v["sort_code"]), account_number=mask(v["account_number"]))
        return {"vendors": vendors}

    @app.get("/api/purchase-orders")
    async def api_pos(request: Request) -> dict:
        who = api_who(request)
        need(who, "view", "list_purchase_orders")
        return {"purchase_orders": store.purchase_orders()}

    # ======================================================================= web pages
    def safe_next(target: str | None) -> str:
        target = target or "/"
        return target if target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"

    def login_form(request: Request, next_page: str, error: str | None, status_code: int) -> HTMLResponse:
        """The sign-in form, paired with this browser by a random value in a cookie and a hidden
        field, so another site cannot sign this browser in with its own token. An existing pairing
        is reused, so two sign-in tabs do not cancel each other."""
        current = request.cookies.get(LOGIN_COOKIE) or ""
        pairing = current if re.fullmatch(r"[A-Za-z0-9_-]{32}", current) else secrets.token_urlsafe(24)
        response = page(request, "login.html", {"next": safe_next(next_page), "error": error, "pairing": pairing},
                        None, status_code)
        response.set_cookie(LOGIN_COOKIE, pairing, httponly=True, samesite="strict", path="/login", max_age=1800,
                            secure=secure_cookies or request.url.scheme == "https")
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/") -> HTMLResponse:
        return login_form(request, next, None, 200)

    @app.post("/login")
    async def login(request: Request) -> Response:
        form = await request.form()
        next_page = str(form.get("next") or "/")
        sent, expected = str(form.get("pairing") or ""), request.cookies.get(LOGIN_COOKIE) or ""
        if not sent or not expected or not secrets.compare_digest(sent, expected):
            log.warning("web sign-in refused: the form was not paired with this browser")
            return login_form(request, next_page, "The sign-in form expired. Please try again.", 403)
        token = str(form.get("token") or "").strip()
        who = deps.tokens.identify_token(token)
        if who is None:
            log.warning("web sign-in refused: unknown token")
            return login_form(request, next_page, "That token is not valid.", 401)
        session_id, _ = store.create_session(who, digest(token))
        store.audit(who.name, who.role, "sign_in", "ok")
        response = RedirectResponse(safe_next(next_page), status_code=303)
        response.set_cookie(COOKIE, session_id, httponly=True, samesite="strict", path="/", max_age=8 * 3600,
                            secure=secure_cookies or request.url.scheme == "https")
        response.delete_cookie(LOGIN_COOKIE, path="/login")
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        await form_with_csrf(request, web_identity(request))
        store.end_session(request.cookies.get(COOKIE))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    def queue(request: Request, status: str = "open", page_no: int = 1) -> HTMLResponse:
        me = web_identity(request)
        need(me[0], "view", "list_documents")
        page_no = max(1, page_no)
        docs, total = store.list_documents(statuses_for(status), PAGE_SIZE, (page_no - 1) * PAGE_SIZE)
        counts = store.counts()
        counts["open"] = counts["needs_review"] + counts["needs_manual_entry"]
        counts["all"] = sum(v for k, v in counts.items() if k in STATUSES)
        return page(request, "queue.html", {"docs": [doc_summary(d) for d in docs], "status": status,
                                            "counts": counts, "total": total, "page_no": page_no,
                                            "first": (page_no - 1) * PAGE_SIZE + 1 if docs else 0,
                                            "last": (page_no - 1) * PAGE_SIZE + len(docs)}, me)

    def detail_context(doc: dict, me: Identity, flash: dict | None = None) -> dict:
        corrections = corrections_for(deps, doc["id"], me)
        correctors = sorted({c["corrected_by"] for c in corrections})
        doc = for_viewer(doc, me)
        by_field: dict[str, list[dict]] = {}
        for f in doc["findings"]:
            if f.get("field"):
                by_field.setdefault(f["field"], []).append(f)
        doc_type = doc["fields"].get("doc_type")
        flash = flash or {}
        return {
            "doc": doc, "field_names": FIELDS_BY_TYPE.get(doc_type or "", ()),
            "lines": line_rows(doc, deps.policy.line_tolerance),
            "by_field": by_field, "corrections": corrections, "masked": not sees_bank_details(me),
            "typed_by": [c.split(" (")[0] for c in correctors] if doc.get("manual_reason") else [],
            "second_reviewer_needed": deps.policy.four_eyes and me.label in correctors,
            "history": list(reversed(store.audit_entries(100, doc["id"]))),
            "blocking": [f for f in doc["findings"] if f["severity"] == "blocking"],
            "errors": [f for f in doc["findings"] if f["severity"] == "error"],
            "reviewable": doc["status"] in REVIEWABLE, "error": flash.get("error"), "notice": flash.get("notice"),
            "refused_ids": flash.get("refused") or [], "can_read_again": doc["status"] in REVIEWABLE and not doc["corrected"],
        }

    @app.get("/documents/{doc_id}", response_class=HTMLResponse)
    async def detail(request: Request, doc_id: str) -> HTMLResponse:
        me = web_identity(request)
        need(me[0], "view", "view_document", doc_id)
        doc = await run_in_threadpool(fresh, get_doc(doc_id, with_text=True))  # checked against today's data
        flash = store.pop_flash(request.cookies.get(COOKIE))
        return page(request, "document.html", detail_context(doc, me[0], flash if isinstance(flash, dict) else None),
                    me)

    @app.get("/documents/{doc_id}/pdf")
    def web_pdf(request: Request, doc_id: str) -> Response:
        who, _ = web_identity(request)
        need(who, "view", "view_pdf", doc_id)
        return pdf_response(get_doc(doc_id))

    async def web_action(request: Request, doc_id: str, action: str, permission: Any,
                         run: Callable[[Identity, Any], dict], notice: str) -> Response:
        me = web_identity(request)
        form = await form_with_csrf(request, me)
        need(me[0], permission, action, doc_id)
        get_doc(doc_id)
        session = request.cookies.get(COOKIE)
        try:
            await run_in_threadpool(run, me[0], form)
        except ActionRefused as exc:
            store.set_flash(session, {"error": exc.message, "refused": [f["id"] for f in exc.findings]})
        else:
            store.set_flash(session, {"notice": notice})
        return RedirectResponse(f"/documents/{doc_id}", status_code=303)

    def version_of(form: Any) -> int:
        """Web forms always carry the version the reviewer saw; a missing one counts as stale."""
        raw = str(form.get("version") or "")
        return int(raw) if raw.isascii() and raw.isdecimal() and len(raw) < 10 else -1

    @app.post("/documents/{doc_id}/approve")
    async def web_approve(request: Request, doc_id: str) -> Response:
        return await web_action(request, doc_id, "approve", "review", lambda who, form: review.approve(
            deps, doc_id, who, [str(a) for a in form.getlist("ack")], str(form.get("note") or ""), version_of(form)),
            "Approved.")

    @app.post("/documents/{doc_id}/reject")
    async def web_reject(request: Request, doc_id: str) -> Response:
        return await web_action(request, doc_id, "reject", "review",
                                lambda who, form: review.reject(deps, doc_id, who, str(form.get("reason") or "")),
                                "Rejected.")

    @app.post("/documents/{doc_id}/recheck")
    async def web_recheck(request: Request, doc_id: str) -> Response:
        return await web_action(request, doc_id, "recheck", "review",
                                lambda who, form: review.recheck(deps, doc_id, who), "The checks ran again.")

    @app.post("/documents/{doc_id}/read-again")
    async def web_read_again(request: Request, doc_id: str) -> Response:
        return await web_action(request, doc_id, "read_again", ("upload", "review"),
                                lambda who, form: review.read_again(deps, doc_id, who), "The PDF was read again.")

    def edit_values(doc: dict, extra: int) -> dict[str, Any]:
        rows = [dict(line) for line in doc["lines"]] + [{} for _ in range(max(2, min(extra, 100)))]
        return {"values": dict(doc["fields"]), "rows": rows}

    @app.get("/documents/{doc_id}/edit", response_class=HTMLResponse)
    def edit_page(request: Request, doc_id: str, extra: int = 2) -> HTMLResponse:
        me = web_identity(request)
        need(me[0], "review", "correct", doc_id)
        doc = get_doc(doc_id)
        if doc["status"] not in REVIEWABLE:
            raise HTTPException(409, f"Document {doc_id} is already {doc['status']}.")
        return page(request, "edit.html", {"doc": doc, "error": None, **edit_values(doc, extra)}, me)

    @app.post("/documents/{doc_id}/edit")
    async def edit_save(request: Request, doc_id: str) -> Response:
        me = web_identity(request)
        form = await form_with_csrf(request, me)
        need(me[0], "review", "correct", doc_id)
        doc = get_doc(doc_id)
        fields = {name: str(form.get(f"f_{name}") or "") for name in HEADER_FIELDS if f"f_{name}" in form}
        lines = None  # without line_count the form did not include the line table: lines stay as they are
        submitted_rows: list[dict] = []
        if "line_count" in form:
            raw_count = str(form.get("line_count") or "")
            if not (raw_count.isascii() and raw_count.isdecimal() and int(raw_count) <= 200):
                return page(request, "edit.html", {"doc": doc, "error": "The line table in the form could not be "
                                                   "read. Reload the page and try again.", **edit_values(doc, 2)}, me, 400)
            lines = [{name: str(form.get(f"l{i}_{name}") or "") for name in LINE_FIELDS} for i in range(int(raw_count))]
            submitted_rows = lines
        if form.get("more_rows"):  # five more empty rows; nothing is saved, nothing typed is lost
            rows = (submitted_rows or edit_values(doc, 0)["rows"]) + [{} for _ in range(5)]
            return page(request, "edit.html", {"doc": doc, "error": None, "values": {**doc["fields"], **fields},
                                               "rows": rows[:200], "note": str(form.get("note") or "")}, me)
        try:
            updated = await run_in_threadpool(review.correct, deps, doc_id, me[0], fields, lines,
                                              str(form.get("note") or ""), version_of(form))
        except ActionRefused as exc:  # show the form again with what the reviewer typed
            values = {**doc["fields"], **fields}
            rows = submitted_rows or edit_values(doc, 2)["rows"]
            return page(request, "edit.html", {"doc": doc, "error": exc.message, "values": values, "rows": rows,
                                               "note": str(form.get("note") or "")}, me, exc.status_code)
        store.set_flash(request.cookies.get(COOKIE), {"notice": "Saved. The checks ran again."
                                                      if updated["version"] != doc["version"] else "Nothing changed."})
        return RedirectResponse(f"/documents/{doc_id}", status_code=303)

    @app.get("/upload", response_class=HTMLResponse)
    def upload_page(request: Request) -> HTMLResponse:
        me = web_identity(request)
        need(me[0], "upload", "upload")
        return page(request, "upload.html", {"error": None}, me)

    @app.post("/upload")
    async def upload_form(request: Request) -> Response:
        me = web_identity(request)
        check_length(request)
        form = await form_with_csrf(request, me)
        need(me[0], "upload", "upload")
        try:
            data, name = await read_upload(form.get("file"))
            result = await run_in_threadpool(ingest, deps, data, name, "upload", me[0])
        except UploadRefused as exc:
            return page(request, "upload.html", {"error": exc.message}, me, exc.status_code)
        except HTTPException as exc:
            return page(request, "upload.html", {"error": exc.detail}, me, exc.status_code)
        if not result.created:
            store.set_flash(request.cookies.get(COOKIE), {"notice": "This file was already in the system; nothing new "
                                                          "was added. This is the existing document."})
        return RedirectResponse(f"/documents/{result.document['id']}", status_code=303)

    @app.get("/exports", response_class=HTMLResponse)
    def exports_page(request: Request) -> HTMLResponse:
        me = web_identity(request)
        need(me[0], "export", "list_exports")
        flash = store.pop_flash(request.cookies.get(COOKIE)) or {}
        return page(request, "exports.html", {"exports": store.exports(), "waiting": store.counts()["approved"],
                                              "outbox": str(Path(deps.outbox_dir).resolve()),
                                              "notice": flash.get("notice"), "error": flash.get("error")}, me)

    @app.post("/exports")
    async def export_form(request: Request) -> Response:
        me = web_identity(request)
        await form_with_csrf(request, me)
        need(me[0], "export", "export")
        try:
            batch = await run_in_threadpool(export_or_explain, me[0])
        except HTTPException as exc:
            store.set_flash(request.cookies.get(COOKIE), {"error": exc.detail})
        else:
            store.set_flash(request.cookies.get(COOKIE), {"notice": f"Export written: {batch['count']} document(s)."
                                                          if batch else "Nothing approved was waiting."})
        return RedirectResponse("/exports", status_code=303)

    @app.get("/exports/{batch_id}/{name}")
    def web_export_file(request: Request, batch_id: str, name: str) -> Response:
        me = web_identity(request)
        need(me[0], "export", "download_export")
        return export_file(batch_id, name)

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request) -> HTMLResponse:
        me = web_identity(request)
        need(me[0], "audit", "read_audit")
        return page(request, "audit.html", {"entries": store.audit_entries(300)}, me)

    @app.get("/purchase-orders", response_class=HTMLResponse)
    def po_page(request: Request) -> HTMLResponse:
        me = web_identity(request)
        need(me[0], "view", "list_purchase_orders")
        rows = []
        for po in store.purchase_orders():
            net = sum((Decimal(str(l["quantity"])) * Decimal(str(l["unit_price"])) for l in po["lines"]), Decimal("0"))
            rows.append({**po, "net": str(net)})
        return page(request, "purchase_orders.html", {"pos": rows, "vendors": {v["id"]: v for v in store.vendors()}}, me)

    return app
