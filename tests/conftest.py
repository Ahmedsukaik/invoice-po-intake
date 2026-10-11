from __future__ import annotations

import re
import ssl
import sys
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intake.config import Identity  # noqa: E402
from intake.deps import Deps  # noqa: E402
from intake.localrun import free_port, local_deps, serve  # noqa: E402
from intake.pipeline import ingest  # noqa: E402
from intake.server import build_app  # noqa: E402

SAMPLES = ROOT / "samples" / "pdfs"
TODAY = date(2026, 10, 11)
CLERK, REVIEWER, REVIEWER2, ADMIN = ("test-clerk-token-000000001", "test-reviewer-token-0000001",
                                     "test-reviewer-token-0000002", "test-admin-token-000000001")
TOKENS = {
    CLERK: {"name": "tom", "role": "clerk"},
    REVIEWER: {"name": "sara", "role": "reviewer"},
    REVIEWER2: {"name": "omar", "role": "reviewer"},
    ADMIN: {"name": "ana", "role": "admin"},
}
# The test servers speak plain http on 127.0.0.1. One shared TLS context saves httpx from loading
# its certificate bundle again for every client (most of a second each on Windows).
LOCAL_TLS = ssl.create_default_context()
TOM = Identity("tom", "clerk")
SARA = Identity("sara", "reviewer")
OMAR = Identity("omar", "reviewer")
ANA = Identity("ana", "admin")


@pytest.fixture
def deps(tmp_path) -> Deps:
    d = local_deps(tmp_path, TOKENS)
    d.today = lambda: TODAY
    return d


def sample(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()


def add(deps: Deps, name_or_bytes: str | bytes, filename: str | None = None) -> dict:
    """Ingest a sample by name (or raw bytes) as the clerk; return the stored document."""
    data = sample(name_or_bytes) if isinstance(name_or_bytes, str) else name_or_bytes
    return ingest(deps, data, filename or (name_or_bytes if isinstance(name_or_bytes, str) else "test.pdf"),
                  "upload", TOM).document


def codes(doc_or_findings: Any) -> list[str]:
    findings = doc_or_findings["findings"] if isinstance(doc_or_findings, dict) else doc_or_findings
    return [f["code"] if isinstance(f, dict) else f.code for f in findings]


def ack(doc_or_findings: Any, *only: str) -> list[str]:
    """The ids of the blocking findings (optionally only these codes): what a reviewer ticks."""
    findings = doc_or_findings["findings"] if isinstance(doc_or_findings, dict) else doc_or_findings
    return [f["id"] for f in findings if f["severity"] == "blocking" and (not only or f["code"] in only)]


def set_status(deps: Deps, doc_id: str, status: str) -> None:
    with deps.store._conn() as conn:
        conn.execute("UPDATE documents SET status = ? WHERE id = ?", (status, doc_id))


def all_docs(deps: Deps) -> list[dict]:
    return deps.store.list_documents(None, 500)[0]


def stored_doc(deps: Deps, fields: dict, lines: list | None = None, status: str = "needs_review") -> dict:
    """Put a document straight into the store (no PDF), for duplicate and PO tests."""
    doc, _ = deps.store.add_document(
        sha256=uuid.uuid4().hex * 2, filename="stored.pdf", size=1, source="test", status=status, fields=fields,
        evidence={}, lines=lines or [], notes=[], extractor="test", page_count=1, text_layer="", manual_reason=None,
        uploaded_by="test")
    return doc


@dataclass
class Server:
    url: str
    deps: Deps
    clerk: httpx.Client
    reviewer: httpx.Client
    reviewer2: httpx.Client
    admin: httpx.Client

    def client(self, token: str | None = None, **kw: Any) -> httpx.Client:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.Client(base_url=self.url, headers=headers, verify=LOCAL_TLS, **kw)

    @staticmethod
    def pairing(web: httpx.Client) -> str:
        """Open the sign-in page (which sets the pairing cookie) and return its hidden pairing value."""
        found = re.search(r'name="pairing" value="([^"]+)"', web.get("/login").text)
        assert found
        return found.group(1)

    def browser(self, token: str = REVIEWER) -> tuple[httpx.Client, str]:
        """A signed-in web session: (client with the session cookie, CSRF token from a page)."""
        web = httpx.Client(base_url=self.url, follow_redirects=False, verify=LOCAL_TLS)
        assert web.post("/login", data={"token": token, "next": "/", "pairing": self.pairing(web)}).status_code == 303
        csrf = re.search(r'name="csrf" value="([^"]+)"', web.get("/").text)
        assert csrf
        return web, csrf.group(1)


@pytest.fixture
def server(deps) -> Server:
    port = free_port()
    running = serve(build_app(deps), port)
    url = f"http://127.0.0.1:{port}"
    s = Server(url, deps, *(httpx.Client(base_url=url, headers={"Authorization": f"Bearer {t}"}, timeout=30,
                                         verify=LOCAL_TLS)
                            for t in (CLERK, REVIEWER, REVIEWER2, ADMIN)))
    yield s
    for c in (s.clerk, s.reviewer, s.reviewer2, s.admin):
        c.close()
    running.should_exit = True
