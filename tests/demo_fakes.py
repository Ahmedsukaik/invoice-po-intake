"""Helpers for the web demo's tests: a simulated Claude API that reads sample 10, and a small
driver for the demo's HTTP API. No live API call is made anywhere in the tests."""
from __future__ import annotations

import json
from typing import Any

import httpx2
from fastapi.testclient import TestClient
from test_claude import SimulatedApi

from intake.demo_session import make_reader


def quoted(value: str, quote: str) -> dict[str, Any]:
    return {"value": value, "page": 1 if value else 0, "quote": quote}


def sample_10_answer(**changes: Any) -> dict[str, Any]:
    """What a careful model returns for samples/pdfs/10_other_layout.pdf: each value as printed,
    with the line it was read from."""
    fields = {
        "vendor_name": quoted("Brightline Demo Packaging Ltd", "Brightline Demo Packaging Ltd"),
        "invoice_number": quoted("BDP/26/0417", "Our ref. BDP/26/0417"),
        "invoice_date": quoted("9 October 2026", "Date of issue 9 October 2026"),
        "due_date": quoted("8 November 2026", "Payment due 8 November 2026"),
        "po_number": quoted("PO-2026-0105", "Your order PO-2026-0105"),
        "order_date": quoted("", ""),
        "currency": quoted("GBP", "Prices in GBP"),
        "vendor_iban": quoted("", ""),
        "vendor_sort_code": quoted("40-00-02", "Bank transfer: sort code 40-00-02, account no. 20000002"),
        "vendor_account": quoted("20000002", "Bank transfer: sort code 40-00-02, account no. 20000002"),
        "subtotal": quoted("£335.00", "Net total £335.00"),
        "tax_rate": quoted("20%", "VAT at 20% £67.00"),
        "tax_amount": quoted("£67.00", "VAT at 20% £67.00"),
        "total": quoted("£402.00", "Amount payable £402.00"),
    }
    fields.update(changes.pop("fields", {}))
    answer = {
        "document_type": "invoice", "fields": fields,
        "lines": [
            {"sku": "BP-310", "description": "Carton, double wall, 400 x 300 mm", "quantity": "300",
             "unit_price": "£0.85", "line_total": "£255.00", "page": 1,
             "quote": "300 BP-310 Carton, double wall, 400 x 300 mm £0.85 £255.00"},
            {"sku": "BP-505", "description": "Edge protector, 1 m", "quantity": "200", "unit_price": "£0.40",
             "line_total": "£80.00", "page": 1, "quote": "200 BP-505 Edge protector, 1 m £0.40 £80.00"},
        ],
        "concerns": [],
    }
    answer.update(changes)
    return answer


class RefusingApi(SimulatedApi):
    """The API answering with an error: the spend limit of the key reached (as a 429 or a 400 that
    names the usage limits), or a plain overload."""

    def __init__(self, status: int, error_type: str, message: str):
        super().__init__(sample_10_answer(), model="claude-haiku-5-5")
        self.error = (status, error_type, message)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        status, error_type, message = self.error
        return httpx2.Response(status, json={"type": "error", "error": {"type": error_type, "message": message}},
                               headers={"request-id": "req_refused"})


def fake_reader(answer: dict[str, Any] | None = None, **api: Any) -> tuple[Any, SimulatedApi]:
    """The demo's real AI reader (the product's ClaudeExtractor, real Anthropic SDK) with the network
    replaced by a simulated, streamed API. Returns (reader, api) so a test can see the requests."""
    simulated = SimulatedApi(answer if answer is not None else sample_10_answer(), model="claude-haiku-5-5", **api)
    return make_reader(simulated.client), simulated


class DemoClient:
    """Drives the demo's HTTP API the way the page does, keeping the signed session."""

    def __init__(self, client: TestClient):
        self.http = client
        data = client.post("/api/new").json()
        self.session: dict[str, Any] = data["session"]
        self.view: dict[str, Any] = data["view"]
        self.result: dict[str, Any] | None = None

    def step(self, action: dict[str, Any], **extra: Any) -> dict[str, Any]:
        res = self.http.post("/api/step", json={"session": self.session, "action": action, **extra})
        assert res.status_code == 200, res.text
        data = res.json()
        self.session, self.view, self.result = data["session"], data["view"], data["result"]
        return data

    def add(self, key: str, **extra: Any) -> dict[str, Any]:
        self.step({"type": "add", "sample": key}, **extra)
        return self.doc(key)

    def doc(self, key: str) -> dict[str, Any]:
        return next(d for d in self.view["documents"] if d["key"] == key)

    def approve(self, key: str, by: str = "sara", ack: list[str] | None = None, note: str = "") -> dict[str, Any]:
        d = self.doc(key)
        self.step({"type": "approve", "doc": d["id"], "by": by, "ack": ack or [], "note": note, "version": d["version"]})
        return self.result or {}

    def codes(self, key: str) -> list[tuple[str, str]]:
        return [(f["code"], f["severity"]) for f in self.doc(key)["findings"]]


def raw_post(client: TestClient, body: str) -> Any:
    return client.post("/api/step", content=body.encode(), headers={"Content-Type": "application/json"})


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True)
