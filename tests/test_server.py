"""The server over real HTTP: tokens, roles, uploads, hostile input, the web page and the CLI."""
import http.client
import json
import re
import shutil
import time
from urllib.parse import urlparse

import httpx
import pytest
from conftest import ADMIN, CLERK, REVIEWER, REVIEWER2, SAMPLES, all_docs, sample

from intake import cli
from intake.config import TokenTable
from samples.make_samples import invoice_pdf


def upload(client: httpx.Client, data: bytes, name: str = "invoice.pdf") -> httpx.Response:
    return client.post("/api/documents", files={"file": (name, data, "application/pdf")})


def audit(server) -> list[tuple[str, str, str]]:
    return [(e["actor"], e["action"], e["outcome"]) for e in server.deps.store.audit_entries(500)]


def raw_request(server, method: str, path: str, headers: dict[str, str], body: bytes = b"") -> tuple[int, bytes]:
    """Send exactly these bytes: no client-side clean-up of the path or the headers."""
    url = urlparse(server.url)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
    conn.putrequest(method, path, skip_accept_encoding=True)
    for name, value in headers.items():
        conn.putheader(name, value)
    conn.endheaders()
    if body:
        conn.send(body)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


# ------------------------------------------------------------------ tokens and roles
def test_health_is_public_everything_else_needs_a_valid_token(server):
    assert httpx.get(f"{server.url}/healthz").json() == {"ok": True}
    with server.client() as anon:
        assert anon.get("/api/documents").status_code == 401
        for header in (f"Basic {REVIEWER}", "Bearer", "Bearer wrong-token-000000000", REVIEWER, f"Token {REVIEWER}",
                       "Bearer " + "x" * 5000):
            response = anon.get("/api/documents", headers={"Authorization": header})
            assert response.status_code == 401 and response.json() == {"error": "missing or invalid bearer token"}
        assert anon.post("/api/exports").status_code == 401
        assert anon.get("/api/audit").status_code == 401


def test_the_token_is_checked_before_any_body_is_read(server):
    # 60 MB announced, nothing sent: the answer comes before the server waits for the body
    status, _ = raw_request(server, "POST", "/api/documents/doc_000000000000/approve",
                            {"Content-Length": "60000000", "Content-Type": "application/json"})
    assert status == 401
    status, _ = raw_request(server, "POST", "/api/documents/doc_000000000000/approve",
                            {"Content-Length": "60000000", "Authorization": f"Bearer {REVIEWER}"})
    assert status == 413  # a valid token still cannot send a huge JSON body


@pytest.mark.parametrize("who, method, path", [
    ("clerk", "post", "/api/documents/{id}/approve"), ("clerk", "post", "/api/documents/{id}/corrections"),
    ("clerk", "post", "/api/documents/{id}/reject"), ("clerk", "post", "/api/exports"), ("clerk", "get", "/api/audit"),
    ("reviewer", "post", "/api/documents"), ("reviewer", "post", "/api/exports"), ("reviewer", "get", "/api/audit"),
    ("admin", "post", "/api/documents"), ("admin", "post", "/api/documents/{id}/approve"),
    ("admin", "post", "/api/documents/{id}/corrections"), ("reviewer", "get", "/api/exports/exp_1/batch.json"),
])
def test_each_role_is_refused_what_it_may_not_do_and_the_refusal_is_audited(server, who, method, path):
    doc_id = upload(server.clerk, sample("01_clean_invoice.pdf")).json()["document"]["id"]
    client = getattr(server, who)
    # the role is checked first: even a missing or broken body gets 403, not 400
    kwargs = {"content": b"{broken", "headers": {"content-type": "application/json"}} if method == "post" else {}
    response = getattr(client, method)(path.format(id=doc_id), **kwargs)
    assert response.status_code == 403 and "cannot" in response.json()["error"]
    assert any(outcome == "denied" for _, _, outcome in audit(server))
    assert server.deps.store.get(doc_id)["status"] == "needs_review"


def test_the_happy_path_over_http(server):
    created = upload(server.clerk, sample("01_clean_invoice.pdf"), "01_clean_invoice.pdf")
    assert created.status_code == 201 and created.json()["created"] is True
    doc = created.json()["document"]
    assert server.reviewer.get("/api/documents").json()["documents"][0]["id"] == doc["id"]
    pdf = server.clerk.get(f"/api/documents/{doc['id']}/pdf")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content == sample("01_clean_invoice.pdf")
    assert "object-src" not in pdf.headers["content-security-policy"]  # the browser's PDF viewer may run
    approved = server.reviewer.post(f"/api/documents/{doc['id']}/approve", json={"version": doc["version"]}).json()
    assert approved["status"] == "approved"
    batch = server.admin.post("/api/exports").json()["batch"]
    assert batch["documents"] == [doc["id"]]
    assert server.admin.post("/api/exports").json() == {"batch": None, "message": "Nothing approved is waiting for export."}
    listing = server.admin.get("/api/exports").json()
    assert listing["exports"][0]["batch_id"] == batch["batch_id"] and listing["outbox"]
    csv_file = server.admin.get(f"/api/exports/{batch['batch_id']}/invoices.csv")
    assert csv_file.status_code == 200 and b"INV-A-1001" in csv_file.content
    assert server.admin.get(f"/api/exports/{batch['batch_id']}/..%2F..%2Fintake.db").status_code == 404


def test_clerks_do_not_see_bank_details_in_the_vendor_master(server):
    clerk_view = {v["id"]: v for v in server.clerk.get("/api/vendors").json()["vendors"]}
    reviewer_view = {v["id"]: v for v in server.reviewer.get("/api/vendors").json()["vendors"]}
    assert clerk_view["V001"]["iban"].endswith("0001") and clerk_view["V001"]["iban"].startswith("*")
    assert reviewer_view["V001"]["iban"] == "GB77DEMO40000110000001"


def test_clerks_never_see_bank_details_in_full_on_a_document(server):
    """A clerk who uploads an invoice with changed bank details learns nothing about the vendor
    master's account, and sees the invoice's own bank details only as their last four characters."""
    doc_id = upload(server.clerk, sample("04_changed_bank_details.pdf")).json()["document"]["id"]
    full = server.reviewer.get(f"/api/documents/{doc_id}", params={"text": "true"}).json()
    seen = server.clerk.get(f"/api/documents/{doc_id}", params={"text": "true"}).json()
    invoice_iban, master_iban = "GB40DEMO40009999999999", "GB81DEMO40000330000003"
    assert full["fields"]["vendor_iban"] == invoice_iban and "GB40 DEMO" in full["text_layer"]
    assert seen["fields"]["vendor_iban"] == "*" * 18 + "9999" and "text_layer" not in seen
    clerk_view = json.dumps(seen)
    for secret in (invoice_iban, master_iban, "GB40 DEMO", "GB81", "0330 0000"):
        assert secret not in clerk_view, secret
    assert "bank_changed" in [f["code"] for f in seen["findings"]]
    web, _ = server.browser(CLERK)
    page = web.get(f"/documents/{doc_id}").text
    assert "GB40" not in page and "GB81" not in page and "shown to reviewers only" in page


def test_the_api_shows_the_same_fresh_findings_as_the_page_and_writes_only_changes(server):
    store = server.deps.store
    doc_id = upload(server.clerk, sample("01_clean_invoice.pdf")).json()["document"]["id"]
    store.set_vendor_iban("V001", "GB00DEMO40000110009999")  # the vendor master changes after the upload
    assert "bank_changed" not in [f["code"] for f in store.require(doc_id)["findings"]]
    seen = server.reviewer.get(f"/api/documents/{doc_id}").json()
    assert "bank_changed" in [f["code"] for f in seen["findings"]]  # as of now, as the web page shows it
    stored = store.require(doc_id)
    assert "bank_changed" in [f["code"] for f in stored["findings"]]
    time.sleep(1.1)  # checked_at has one-second steps
    server.reviewer.get(f"/api/documents/{doc_id}")
    web, _ = server.browser(REVIEWER)
    assert "Bank details differ" in web.get(f"/documents/{doc_id}").text
    assert store.require(doc_id)["checked_at"] == stored["checked_at"]  # nothing changed, nothing written


def test_more_empty_rows_keeps_what_was_typed_and_the_typist_cannot_approve(server):
    store = server.deps.store
    doc_id = upload(server.clerk, sample("08_scanned_invoice.pdf")).json()["document"]["id"]
    web, csrf = server.browser(REVIEWER)
    form = {"csrf": csrf, "version": "1", "f_vendor_name": "Acme Demo Components Ltd", "line_count": "2",
            "l0_sku": "AC-100", "l0_description": "Steel bracket, 40 mm", "note": "from the scan"}
    more = web.post(f"/documents/{doc_id}/edit", data={**form, "more_rows": "1"})
    assert more.status_code == 200 and 'value="Acme Demo Components Ltd"' in more.text and 'value="AC-100"' in more.text
    assert 'name="l6_sku"' in more.text and 'name="l7_sku"' not in more.text  # two rows plus five
    assert store.get(doc_id)["version"] == 1 and not store.corrections(doc_id)  # nothing saved yet
    saved = web.post(f"/documents/{doc_id}/edit", data={**form, "f_doc_type": "invoice"})
    assert saved.status_code == 303
    page = web.get(f"/documents/{doc_id}").text
    assert "Typed in by sara" in page and "another reviewer must approve it" in page


def test_the_approve_button_is_off_for_whoever_changed_a_value(server):
    doc_id = upload(server.clerk, sample("01_clean_invoice.pdf")).json()["document"]["id"]
    web, csrf = server.browser(REVIEWER)
    assert web.post(f"/documents/{doc_id}/edit", data={"csrf": csrf, "version": "1",
                                                        "f_due_date": "2026-11-05"}).status_code == 303
    disabled = re.compile(r'<button class="btn primary"\s+disabled\s*>Approve</button>')
    assert disabled.search(web.get(f"/documents/{doc_id}").text)
    other, _ = server.browser(REVIEWER2)
    assert not disabled.search(other.get(f"/documents/{doc_id}").text)


# ------------------------------------------------------------------ uploads
def test_the_same_file_twice_is_one_document(server):
    first = upload(server.clerk, sample("01_clean_invoice.pdf"))
    second = upload(server.clerk, sample("01_clean_invoice.pdf"), "renamed copy.pdf")
    assert second.status_code == 200 and second.json()["created"] is False
    assert second.json()["document"]["id"] == first.json()["document"]["id"]
    assert len(all_docs(server.deps)) == 1
    assert ("tom", "upload", "duplicate_file") in audit(server)


@pytest.mark.parametrize("data, status", [
    (b"", 400), (b"MZ\x90\x00 this is an exe", 415), (b"<html><script>alert(1)</script></html>", 415),
], ids=["empty", "exe", "html"])
def test_files_that_are_not_pdfs_are_refused_and_audited(server, data, status):
    response = upload(server.clerk, data, "invoice.pdf")
    assert response.status_code == status
    assert all_docs(server.deps) == [] and ("tom", "upload", "refused") in audit(server)


def test_size_limit(server):
    server.deps.max_upload_bytes = 2000
    assert upload(server.clerk, sample("01_clean_invoice.pdf")).status_code == 413  # checked after reading
    big = upload(server.clerk, b"%PDF-" + b"0" * 80_000)  # refused from the Content-Length, before parsing
    assert big.status_code == 413 and all_docs(server.deps) == []


def test_an_upload_without_a_declared_size_is_refused(server):
    def endless():
        yield b"--x\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.pdf\"\r\n\r\n%PDF-"
        for _ in range(3):
            yield b"0" * 1000
    response = server.clerk.post("/api/documents", content=endless(),
                                 headers={"content-type": "multipart/form-data; boundary=x"})
    assert response.status_code == 411 and all_docs(server.deps) == []


def test_upload_needs_a_file_field(server):
    assert server.clerk.post("/api/documents", data={"note": "no file"}).status_code == 400


def test_hostile_file_names_never_become_paths(server):
    response = upload(server.clerk, sample("01_clean_invoice.pdf"), "..\\..\\..\\Windows\\evil<script>.pdf")
    doc = response.json()["document"]
    assert doc["filename"] == "evil_script_.pdf"
    stored = list(server.deps.storage_dir.iterdir())
    assert [p.name for p in stored] == [f"{doc['sha256']}.pdf"]
    disposition = server.clerk.get(f"/api/documents/{doc['id']}/pdf").headers["content-disposition"]
    assert "evil_script_.pdf" in disposition and "<" not in disposition


@pytest.mark.parametrize("path", ["/api/documents/../../intake.db", "/api/documents/..%2F..%2Fintake.db",
                                  "/api/documents/doc_ZZZ", "/api/documents/doc_0123456789ab'%20OR%20'1'='1",
                                  "/api/documents/doc_0123456789ab/../../../storage"])
def test_document_ids_and_raw_paths_are_strict(server, path):
    status, body = raw_request(server, "GET", path, {"Authorization": f"Bearer {REVIEWER}"})
    assert status == 404 and b"sha256" not in body and b"SQLite" not in body


# ------------------------------------------------------------------ the JSON API's input checks
def test_request_bodies_are_validated(server):
    doc_id = upload(server.clerk, sample("01_clean_invoice.pdf")).json()["document"]["id"]
    base = f"/api/documents/{doc_id}"
    post = server.reviewer.post
    assert post(f"{base}/corrections", json={"fields": {"total": 1800.0}, "version": 1}).status_code == 400  # a float
    assert post(f"{base}/corrections", json={"fields": {}, "status": "approved", "version": 1}).status_code == 400
    assert post(f"{base}/approve", json={"acknowledge": "all", "version": 1}).status_code == 400
    assert post(f"{base}/approve", content=b"{not json", headers={"content-type": "application/json"}).status_code == 400
    missing = post(f"{base}/approve", json={})  # the version the caller looked at is required
    assert missing.status_code == 400 and "version" in missing.json()["error"]
    stale = post(f"{base}/approve", json={"version": 7})
    assert stale.status_code == 409 and "changed after you opened it" in stale.json()["error"]
    bad = server.reviewer.post(f"{base}/corrections", json={"fields": {"total": "1.800,00"}, "version": 1})
    assert bad.status_code == 400 and "number" in bad.json()["error"]
    refused = server.reviewer.post(f"{base}/reject", json={"reason": ""})
    assert refused.status_code == 400
    assert server.reviewer.get("/api/documents", params={"status": "sideways"}).status_code == 400


def test_refused_approval_returns_the_findings_with_their_ids(server):
    doc_id = upload(server.clerk, sample("06_exceeds_po.pdf")).json()["document"]["id"]
    refused = server.reviewer.post(f"/api/documents/{doc_id}/approve", json={"version": 1})
    assert refused.status_code == 409
    findings = refused.json()["findings"]
    assert {f["code"] for f in findings} == {"po_amount_exceeded", "po_qty_exceeded"}
    done = server.reviewer.post(f"/api/documents/{doc_id}/approve", json={
        "version": 1, "acknowledge": [f["id"] for f in findings], "note": "agreed with the buyer"})
    assert done.status_code == 200 and done.json()["status"] == "approved"


def test_the_queue_is_paged(server):
    for name in ("01_clean_invoice.pdf", "02_totals_do_not_add_up.pdf", "03_unknown_vendor.pdf"):
        upload(server.clerk, sample(name))
    page = server.reviewer.get("/api/documents", params={"limit": 2}).json()
    assert len(page["documents"]) == 2 and page["total"] == 3
    rest = server.reviewer.get("/api/documents", params={"limit": 2, "offset": 2}).json()
    assert len(rest["documents"]) == 1


def test_inbox_scan_and_read_again_endpoints(server):
    shutil.copy(SAMPLES / "01_clean_invoice.pdf", server.deps.inbox_dir / "a.pdf")
    (server.deps.inbox_dir / "notes.txt").write_text("not a pdf", encoding="utf-8")
    (server.deps.inbox_dir / "fake.pdf").write_bytes(b"MZ not a pdf")
    assert server.reviewer.post("/api/inbox/scan").status_code == 403
    items = {i["file"]: i for i in server.clerk.post("/api/inbox/scan").json()["items"]}
    assert items["a.pdf"]["status"] == "needs_review" and items["notes.txt"]["error"] and items["fake.pdf"]["error"]
    failed = sorted(p.name for p in (server.deps.inbox_dir / "failed").iterdir())
    assert failed == ["fake.pdf", "fake.pdf.reason.txt", "notes.txt", "notes.txt.reason.txt"]
    assert [p.name for p in server.deps.inbox_dir.iterdir() if p.is_file()] == []
    again = server.clerk.post(f"/api/documents/{items['a.pdf']['document_id']}/read-again")
    assert again.status_code == 200 and again.json()["version"] == 2
    assert server.admin.post(f"/api/documents/{items['a.pdf']['document_id']}/read-again").status_code == 403


# ------------------------------------------------------------------ the web page
def test_pages_need_a_session_and_sign_in_needs_a_valid_token(server):
    with server.client(follow_redirects=False) as anon:
        redirect = anon.get("/documents/doc_0123456789ab")
        assert redirect.status_code == 303 and redirect.headers["location"] == "/login?next=/documents/doc_0123456789ab"
        assert anon.post("/login", data={"token": "wrong-token-0000000000",
                                         "pairing": server.pairing(anon)}).status_code == 401
        signed_in = anon.post("/login", data={"token": REVIEWER, "next": "//evil.example/phish",
                                              "pairing": server.pairing(anon)})
        assert signed_in.status_code == 303 and signed_in.headers["location"] == "/"  # no open redirect
        cookie = signed_in.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert anon.get("/favicon.ico").status_code == 200


def test_sign_in_must_come_from_the_sign_in_page(server):
    """Another site cannot post its own token into the user's browser (sign-in CSRF)."""
    with server.client(follow_redirects=False) as browser:
        assert browser.post("/login", data={"token": REVIEWER}).status_code == 403
        first = server.pairing(browser)
        assert server.pairing(browser) == first  # a second sign-in tab gets the same pairing
        assert browser.post("/login", data={"token": REVIEWER, "pairing": "x" * 32}).status_code == 403
        assert "intake_session" not in browser.cookies


def test_web_forms_need_the_csrf_token(server):
    doc_id = upload(server.clerk, sample("01_clean_invoice.pdf")).json()["document"]["id"]
    web, csrf = server.browser(REVIEWER)
    forged = web.post(f"/documents/{doc_id}/approve", data={"version": "1"})
    assert forged.status_code == 403 and server.deps.store.get(doc_id)["status"] == "needs_review"
    wrong = web.post(f"/documents/{doc_id}/approve", data={"version": "1", "csrf": "x" * 32})
    assert wrong.status_code == 403 and ("sara", "csrf", "denied") in audit(server)
    ok = web.post(f"/documents/{doc_id}/approve", data={"version": "1", "csrf": csrf})
    assert ok.status_code == 303 and server.deps.store.get(doc_id)["status"] == "approved"


def test_reviewers_work_through_the_web_page(server):
    doc_id = upload(server.clerk, sample("06_exceeds_po.pdf")).json()["document"]["id"]
    web, csrf = server.browser(REVIEWER)
    page = web.get(f"/documents/{doc_id}").text
    assert "Quantity above what the PO has left" in page and 'name="ack"' in page
    refused = web.post(f"/documents/{doc_id}/approve", data={"csrf": csrf, "version": "1"})
    assert refused.status_code == 303 and refused.headers["location"] == f"/documents/{doc_id}"  # redirect after POST
    assert "must be checked and ticked" in web.get(f"/documents/{doc_id}").text
    bad = web.post(f"/documents/{doc_id}/edit", data={"csrf": csrf, "version": "1", "f_due_date": "09/11/2026",
                                                      "note": "typo"})
    assert bad.status_code == 400 and 'value="09/11/2026"' in bad.text  # what was typed is kept
    odd = web.post(f"/documents/{doc_id}/edit", data={"csrf": csrf, "version": "1", "line_count": "²"})
    assert odd.status_code == 400 and len(server.deps.store.get(doc_id)["lines"]) == 1
    edited = web.post(f"/documents/{doc_id}/edit", data={"csrf": csrf, "version": "1", "f_due_date": "2026-11-09",
                                                          "note": "typo"})
    assert edited.status_code == 303 and server.deps.store.get(doc_id)["fields"]["due_date"] == "2026-11-09"
    second, second_csrf = server.browser(REVIEWER2)  # sara changed a value, so omar approves (four eyes)
    ids = re.findall(r'name="ack" value="([0-9a-f]{10})"', second.get(f"/documents/{doc_id}").text)
    assert len(ids) == 3
    approved = second.post(f"/documents/{doc_id}/approve", data={
        "csrf": second_csrf, "version": "2", "ack": ids, "note": "agreed with the buyer"})
    assert approved.status_code == 303 and server.deps.store.get(doc_id)["status"] == "approved"
    assert "Needs a person" not in second.get(f"/documents/{doc_id}").text


def test_roles_apply_on_the_web_page_too(server):
    doc_id = upload(server.clerk, sample("01_clean_invoice.pdf")).json()["document"]["id"]
    web, csrf = server.browser(CLERK)
    assert "Waiting for a reviewer" in web.get(f"/documents/{doc_id}").text
    assert web.post(f"/documents/{doc_id}/approve", data={"csrf": csrf}).status_code == 403
    assert web.get("/audit").status_code == 403 and web.get("/exports").status_code == 403
    admin, admin_csrf = server.browser(ADMIN)
    assert admin.get("/audit").status_code == 200
    assert admin.post("/exports", data={"csrf": admin_csrf}).status_code == 303
    assert "Nothing approved was waiting" in admin.get("/exports").text


def test_text_from_a_pdf_cannot_inject_html(server):
    vendor = {"name": "<script>alert(1)</script> Evil Ltd", "address": "<img src=x onerror=alert(2)>", "email": None,
              "iban": ""}
    data = invoice_pdf(vendor, "INV-1", "2026-10-05", None, None, [("AC-100", "<b>bold</b>", "1", "1.00", "1.00")],
                       "1.00", "20", "0.20", "1.20", iban=None)
    doc_id = upload(server.clerk, data).json()["document"]["id"]
    web, _ = server.browser(REVIEWER)
    response = web.get(f"/documents/{doc_id}")
    assert "<script>alert(1)" not in response.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<img src=x" not in response.text and "<b>bold</b>" not in response.text
    csp = response.headers["content-security-policy"]
    assert "script-src 'none'" in csp and response.headers["x-content-type-options"] == "nosniff"


def test_a_removed_token_ends_its_web_sessions(server, deps):
    web, _ = server.browser(REVIEWER)
    assert web.get("/").status_code == 200
    from conftest import TOKENS
    smaller = {t: info for t, info in TOKENS.items() if t != REVIEWER}
    deps.tokens = TokenTable(smaller)  # as after a restart with that token removed from .env
    assert web.get("/").status_code == 303 and web.get("/").headers["location"].startswith("/login")


# ------------------------------------------------------------------ CLI
def run_cli(server, token, *args, capsys) -> tuple[int, str, str]:
    code = cli.main(["--url", server.url, "--token", token, *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_end_to_end(server, capsys, tmp_path):
    pdf = tmp_path / "06_exceeds_po.pdf"
    pdf.write_bytes(sample("06_exceeds_po.pdf"))
    code, out, _ = run_cli(server, CLERK, "upload", str(pdf), capsys=capsys)
    doc_id = re.search(r"doc_[0-9a-f]{12}", out).group(0)
    assert code == 0 and "added" in out
    code, out, _ = run_cli(server, REVIEWER, "list", capsys=capsys)
    assert code == 0 and doc_id in out and "2 blocking" in out and "1,344.00" in out
    code, _, err = run_cli(server, REVIEWER, "approve", doc_id, "--version", "1", capsys=capsys)
    assert code == 1 and "po_qty_exceeded" in err
    code, out, _ = run_cli(server, REVIEWER, "correct", doc_id, "--version", "1", "due_date=2026-11-09",
                           "--note", "typo", capsys=capsys)
    assert code == 0 and "(corrected)" in out and "version 2" in out
    code, _, err = run_cli(server, REVIEWER2, "approve", doc_id, "--version", "1", capsys=capsys)  # what `show` said
    assert code == 1 and "changed after you opened it" in err
    code, out, _ = run_cli(server, REVIEWER2, "show", doc_id, capsys=capsys)
    ids = re.findall(r"\[ack id ([0-9a-f]{10})\]", out)
    args = [a for i in ids for a in ("--ack", i)]
    code, out, _ = run_cli(server, REVIEWER2, "approve", doc_id, "--version", "2", *args, "--note",
                           "agreed with the buyer", capsys=capsys)
    assert code == 0 and "[approved]" in out
    code, out, _ = run_cli(server, ADMIN, "export", capsys=capsys)
    assert code == 0 and "1 document(s)" in out
    code, out, _ = run_cli(server, ADMIN, "audit", "--limit", "50", capsys=capsys)
    assert code == 0 and "approve" in out and "export" in out
    code, _, err = run_cli(server, CLERK, "export", capsys=capsys)
    assert code == 1 and "Error 403" in err


def test_cli_output_cannot_be_spoofed_by_document_text(capsys):
    hostile = "Evil Ltd\r\ndoc_000000000000  [approved]  fake line\x1b[2J‮"
    cli.print_document({"id": "doc_aaaaaaaaaaaa", "status": "needs_review", "filename": hostile, "version": 1,
                        "extractor": "rules", "fields": {"doc_type": "invoice", "vendor_name": hostile, "total": "1.00"},
                        "lines": [{"sku": "A-1", "description": hostile, "quantity": "1", "unit_price": "1.00",
                                   "line_total": "1.00"}],
                        "findings": [{"severity": "blocking", "code": "x", "message": hostile, "id": "abc"}],
                        "corrected": [], "corrections": []})
    out = capsys.readouterr().out
    assert not any(line.startswith("doc_000000000000") for line in out.splitlines())
    assert "\x1b" not in out and "\r" not in out and "‮" not in out


def test_cli_says_clearly_when_the_server_is_not_there(capsys):
    code = cli.main(["--url", "http://127.0.0.1:9", "--token", REVIEWER, "list"])
    assert code == 1 and "Could not reach" in capsys.readouterr().err


# ------------------------------------------------------------------ exports over HTTP
def test_an_admin_settles_an_export_left_in_doubt(server):
    class CannotTell:
        name = "fake-erp"

        def write(self, folder, batch, records):
            raise ConnectionError("no answer from the ERP")

    doc_id = upload(server.clerk, sample("01_clean_invoice.pdf")).json()["document"]["id"]
    server.reviewer.post(f"/api/documents/{doc_id}/approve", json={"version": 1})
    server.deps.exporter = CannotTell()
    failed = server.admin.post("/api/exports")
    assert failed.status_code == 502 and "resolve-export" in failed.json()["error"]
    assert server.deps.store.get(doc_id)["status"] == "exporting"  # in doubt: not exported again, not lost
    batch_id = server.admin.get("/api/exports").json()["exports"][0]["batch_id"]
    assert server.reviewer.post(f"/api/exports/{batch_id}/resolve", json={"written": True}).status_code == 403
    assert server.admin.post("/api/exports/exp_nope/resolve", json={"written": True}).status_code == 404
    done = server.admin.post(f"/api/exports/{batch_id}/resolve", json={"written": True})
    assert done.json() == {"batch_id": batch_id, "resolved": "written"}
    assert server.deps.store.get(doc_id)["status"] == "exported"


def test_odd_characters_in_a_refused_path_do_not_reach_the_log_raw(server, caplog):
    with server.client() as anon, caplog.at_level("WARNING", logger="intake"):
        anon.get("/api/documents/%0aFAKE%20LOG%20LINE%1b[31m")
    lines = [r.getMessage() for r in caplog.records if "bearer token" in r.getMessage()]
    assert lines and all("\n" not in m and "\x1b" not in m for m in lines)
