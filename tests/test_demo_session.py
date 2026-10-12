"""The web demo's signed sessions and their replay into a throwaway copy of the product. No live
API call is made: the AI reader is the product's ClaudeExtractor with a simulated API."""
from __future__ import annotations

import copy
import csv
import io
import json
import tempfile
from pathlib import Path

import pytest
from demo_fakes import RefusingApi, fake_reader, sample_10_answer

from intake import demo_session
from intake.demo_session import (
    MAX_EVENTS,
    SAMPLES,
    TMP_PREFIX,
    ActionError,
    SessionError,
    handle,
    new_session,
    sample_sha,
    signed,
    verified_events,
)


class Visitor:
    """Calls demo_session.handle the way the web server does, keeping the signed session."""

    def __init__(self, reader=None, want_ai=True):
        self.session, self.reader, self.want_ai = new_session(), reader, want_ai
        self.out: dict = handle(self.session, {"type": "view"})

    def do(self, action: dict) -> dict | None:
        self.out = handle(self.session, action, reader=self.reader, want_ai=self.want_ai)
        self.session = self.out["session"]
        return self.out["result"]

    def add(self, key: str) -> dict:
        self.do({"type": "add", "sample": key})
        return self.doc(key)

    def doc(self, key: str) -> dict:
        return next(d for d in self.out["view"]["documents"] if d["key"] == key)

    def approve(self, key: str, by: str = "sara", ack: list | None = None, note: str = "") -> dict:
        d = self.doc(key)
        return self.do({"type": "approve", "doc": d["id"], "by": by, "ack": ack or [], "note": note,
                        "version": d["version"]}) or {}

    def codes(self, key: str) -> list[tuple[str, str]]:
        return [(f["code"], f["severity"]) for f in self.doc(key)["findings"]]

    @property
    def view(self) -> dict:
        return self.out["view"]


def leftovers() -> list[str]:
    return sorted(p.name for p in Path(tempfile.gettempdir()).iterdir() if p.name.startswith(TMP_PREFIX))


# ------------------------------------------------------------------ what each sample shows
@pytest.mark.parametrize("key, status, stamp, expected", [
    ("01", "needs_review", None, [("po_matched", "info")]),
    ("02", "needs_review", None, [("line_math", "blocking"), ("total_mismatch", "blocking"),
                                  ("po_amount_exceeded", "blocking")]),
    ("03", "needs_review", "locked", [("vendor_unknown", "error"), ("po_missing", "blocking")]),
    ("04", "needs_review", "locked", [("bank_changed", "error"), ("po_matched", "info")]),
    ("05", "needs_review", None, [("po_matched", "info")]),
    ("06", "needs_review", None, [("po_qty_exceeded", "blocking"), ("po_amount_exceeded", "blocking")]),
    ("07", "needs_review", None, [("po_in_register", "info")]),
    ("08", "needs_manual_entry", None, [("doc_type_unknown", "error"), ("manual_entry", "info")]),
    ("09", "needs_review", None, [("po_price_mismatch", "blocking"), ("po_amount_exceeded", "blocking")]),
    ("10", "needs_manual_entry", None, [("reader_unknown_layout", "error"), ("manual_entry", "info")]),
])
def test_each_sample_on_its_own(key, status, stamp, expected):
    v = Visitor()
    d = v.add(key)
    assert (d["status"], d["stamp"], v.codes(key)) == (status, stamp, expected)
    assert d["id"] == f"doc_{key}" and d["file"] == SAMPLES[key].file


def test_every_value_the_rules_reader_read_names_its_source_line():
    v = Visitor()
    d = v.add("01")
    read = {f["name"]: f for f in d["fields"] if f["value"]}
    assert read["invoice_number"]["source"] == {"page": 1, "text": "Invoice number: INV-A-1001"}
    assert read["total"]["shown"] == "£1,800.00" and read["total"]["source"]["text"] == "Total due 1,800.00"
    assert all(f["source"] for f in read.values())
    assert [line["source"]["text"] for line in d["lines"]] == ["AC-100 Steel bracket, 40 mm 200 4.50 900.00",
                                                              "AC-220 Hinge set, zinc 50 12.00 600.00"]


def test_a_copy_of_an_approved_invoice_is_locked():
    v = Visitor()
    v.add("01")
    assert v.approve("01")["ok"]
    v.add("05")
    assert ("duplicate_invoice", "error") in v.codes("05") and v.doc("05")["stamp"] == "locked"
    assert not v.approve("05")["ok"]


# ------------------------------------------------------------------ the story, step by step
def test_the_six_step_story():
    v = Visitor()
    first = v.add("01")                                                    # 1: read and matched
    assert first["blocking"] == [] and first["errors"] == 0 and "PO-2026-0101" in first["findings"][0]["message"]
    assert v.approve("01") == {"ok": True, "doc": "doc_01", "message": "Approved."}  # 2: approve
    assert v.doc("01")["stamp"] == "approved" and v.doc("01")["decided"]["by"] == "Sara"
    locked = v.add("04")                                                   # 3: new bank account
    assert locked["stamp"] == "locked" and locked["hard_lock"] and locked["locked"] == [
        "Bank details differ from those on file"]  # the product's finding, in the demo's plain words
    refused = v.approve("04")
    assert refused["ok"] is False and "cannot be approved" in refused["message"]
    assert v.doc("04")["status"] == "needs_review"
    assert v.do({"type": "reject", "doc": "doc_04", "by": "sara", "reason": "Bank details differ from the master."})["ok"]
    over = v.add("06")                                                     # 4: over the PO
    assert [c for c, _ in v.codes("06")] == ["po_qty_exceeded", "po_amount_exceeded"] and len(over["blocking"]) == 2
    assert not v.approve("06", ack=over["blocking"][:1], note="Agreed with the buyer.")["ok"]
    assert not v.approve("06", ack=over["blocking"], note="ok")["ok"]  # a note needs 10 characters
    assert v.approve("06", ack=over["blocking"], note="Buyer agreed the extra plates by phone.")["ok"]
    assert v.doc("06")["decided"]["acknowledged"] == ["Amount above what the PO has left",  # stored sorted by code
                                                      "Quantity above what the PO has left"]
    unseen = v.add("10")                                                   # 5: a layout the rules do not know
    assert unseen["status"] == "needs_manual_entry" and unseen["ai"] == {"status": "unavailable"}
    assert v.do({"type": "export"})["ok"]                                  # 6: export, audit trail, cost
    [batch] = v.view["exports"]
    files = {f["name"]: f for f in batch["files"]}
    rows = list(csv.DictReader(io.StringIO(files["invoices.csv"]["content"])))
    assert [(r["invoice_number"], r["approved_by"], r["total"]) for r in rows] == [
        ("INV-A-1001", "sara (reviewer)", "1800.00"), ("INV-A-1002", "sara (reviewer)", "1344.00")]
    assert "Buyer agreed" in rows[1]["approval_note"]
    assert json.loads(files["batch.json"]["content"])["count"] == 2
    assert v.view["counts"] == {"waiting": 1, "approved": 0, "exported": 2, "rejected": 1}
    assert v.do({"type": "export"})["batch"] is None  # nothing is exported twice
    texts = [a["text"] for a in v.view["audit"]]
    assert any("Tried to approve it. Refused" in t for t in texts)
    assert any("Exported 2 documents as CSV and JSON" in t for t in texts)
    assert v.view["metrics"] == {"ai_reads": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


def test_four_eyes_with_two_reviewers():
    v = Visitor()
    scan = v.add("08")
    hint = scan["type_in"]
    assert hint and hint["fields"]["invoice_number"] == "INV-A-1003"
    assert v.do({"type": "correct", "doc": "doc_08", "by": "sara", "fields": hint["fields"], "lines": hint["lines"],
                 "note": "Typed in from the scan.", "version": scan["version"]})["ok"]
    typed = v.doc("08")
    assert typed["four_eyes"] == ["sara"] and typed["status_label"] == "Typed in, waiting"
    assert ("bank_typed", "blocking") in v.codes("08") and typed["errors"] == 0
    own = v.approve("08", by="sara", ack=typed["blocking"], note="Checked against the scan.")
    assert own["ok"] is False and "a second person must approve" in own["message"]
    assert not v.approve("08", by="omar")["ok"]  # the bank details typed in by a person must be ticked
    assert v.approve("08", by="omar", ack=typed["blocking"], note="Compared the IBAN with the vendor master.")["ok"]
    assert v.doc("08")["decided"]["by"] == "Omar"


# ------------------------------------------------------------------ replay
def test_the_same_events_give_the_same_page_every_time():
    v = Visitor()
    v.add("01")
    v.approve("01")
    v.add("06")
    v.add("04")
    v.do({"type": "export"})
    first = handle(v.session, {"type": "view"})
    second = handle(copy.deepcopy(v.session), {"type": "view"})
    assert first == second
    assert first["view"]["audit"][-1]["time"] == "09:02"  # the clock is pinned to the replay, not the wall clock


def test_events_name_documents_by_their_hash():
    v = Visitor()
    v.add("01")
    v.approve("01")
    events = v.session["events"]
    assert events[0] == {"type": "add", "sha256": sample_sha(SAMPLES["01"]), "reader": "rules"}
    assert events[1]["sha256"] == sample_sha(SAMPLES["01"]) and "doc" not in events[1]


def test_the_date_is_pinned_to_11_october_2026():
    assert Visitor().view["today"] == "2026-10-11"


# ------------------------------------------------------------------ signing
def test_a_changed_event_is_refused():
    v = Visitor()
    v.add("06")
    d = v.doc("06")
    v.approve("06", ack=d["blocking"], note="Agreed with the buyer by phone.")
    tampered = copy.deepcopy(v.session)
    tampered["events"][-1]["note"] = "Something else"
    with pytest.raises(SessionError):
        handle(tampered, {"type": "view"})


@pytest.mark.parametrize("change", ["drop", "add", "reorder", "resign", "sig"])
def test_forged_sessions_are_refused(change):
    v = Visitor()
    v.add("01")
    v.add("06")
    forged = copy.deepcopy(v.session)
    events = forged["events"]
    if change == "drop":
        events.pop()
    elif change == "add":
        events.append({"type": "approve", "sha256": sample_sha(SAMPLES["06"]), "by": "sara", "ack": [], "note": "",
                       "version": 1})
    elif change == "reorder":
        events.reverse()
    elif change == "resign":
        import hashlib
        import hmac
        forged["sig"] = hmac.new(b"x" * 32, demo_session._canonical(events), hashlib.sha256).hexdigest()
    else:
        forged["sig"] = 12
    with pytest.raises(SessionError):
        verified_events(forged)


def test_an_ai_reading_cannot_be_forged_or_changed():
    reader, _ = fake_reader()
    v = Visitor(reader=reader)
    v.add("10")
    assert v.session["events"][0]["reader"] == "claude"
    changed = copy.deepcopy(v.session)
    changed["events"][0]["reading"]["answer"]["fields"]["total"]["value"] = "£4,020.00"
    with pytest.raises(SessionError):
        handle(changed, {"type": "view"})
    forged = {"v": 1, "events": [{"type": "add", "sha256": sample_sha(SAMPLES["10"]), "reader": "claude",
                                  "reading": {"answer": sample_10_answer(), "model": "claude-haiku-5-5",
                                              "usage": {"input_tokens": 1, "output_tokens": 1}, "ms": 1}}]}
    forged["sig"] = "0" * 64
    with pytest.raises(SessionError):
        handle(forged, {"type": "view"})


def test_a_signed_event_with_a_bad_shape_still_stops_the_replay():
    for event in ({"type": "add", "sha256": "0" * 64, "reader": "rules"},       # not a demo file
                  {"type": "add", "sha256": sample_sha(SAMPLES["01"]), "reader": "rules", "extra": 1},
                  {"type": "approve", "sha256": sample_sha(SAMPLES["01"]), "by": "sara", "ack": [], "note": "",
                   "version": 1},                                                # a document never added
                  {"type": "robot"}):
        with pytest.raises(SessionError):
            handle(signed([event]), {"type": "view"})


def test_the_session_has_a_length_limit():
    with pytest.raises(SessionError):
        verified_events(signed([{"type": "export"}] * (MAX_EVENTS + 1)))
    full = signed([{"type": "export"}] * MAX_EVENTS)
    with pytest.raises(ActionError):
        handle(full, {"type": "export"})


def test_unknown_samples_and_documents_are_refused_without_recording_anything():
    v = Visitor()
    with pytest.raises(ActionError):
        v.do({"type": "add", "sample": "../../etc/passwd"})
    with pytest.raises(ActionError):
        v.do({"type": "approve", "doc": "doc_01", "by": "sara", "version": 1})
    with pytest.raises(ActionError):
        v.add("01") and v.do({"type": "approve", "doc": "doc_01", "by": "mallory", "version": 1})
    assert len(v.session["events"]) == 1


def test_the_secret_is_required_on_vercel(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("INTAKE_DEMO_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        demo_session._secret()
    monkeypatch.setenv("INTAKE_DEMO_SECRET", "s" * 40)
    assert demo_session._secret() == b"s" * 40


# ------------------------------------------------------------------ the temporary folder
def test_the_temporary_folder_is_removed_after_every_request(monkeypatch):
    before = leftovers()
    v = Visitor()
    v.add("01")
    v.approve("01")
    v.do({"type": "export"})
    with pytest.raises(ActionError):
        v.do({"type": "add", "sample": "nope"})
    tampered = copy.deepcopy(v.session)
    tampered["events"].pop()
    with pytest.raises(SessionError):
        handle(tampered, {"type": "view"})

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(demo_session, "view", boom)
    with pytest.raises(RuntimeError):
        handle(v.session, {"type": "view"})
    assert leftovers() == before


# ------------------------------------------------------------------ the AI reader
def test_the_ai_reader_reads_sample_10_with_source_lines_once():
    reader, api = fake_reader()
    v = Visitor(reader=reader)
    d = v.add("10")
    assert d["status"] == "needs_review" and d["reader"] == "claude:claude-haiku-5-5"
    assert v.codes("10") == [("po_matched", "info")]
    values = {f["name"]: f for f in d["fields"] if f["value"]}
    assert values["total"]["value"] == "402.00" and values["total"]["source"]["text"] == "Amount payable £402.00"
    assert values["invoice_date"]["value"] == "2026-10-09" and values["vendor_sort_code"]["value"]
    assert d["ai"]["status"] == "ok" and d["ai"]["input_tokens"] == 3100 and d["ai"]["output_tokens"] == 900
    assert d["ai"]["cost_usd"] == pytest.approx((3100 * 0.10 + 900 * 0.50) / 1_000_000)
    assert v.view["metrics"]["ai_reads"] == 1
    body = api.body
    assert body["model"] == "claude-haiku-5-5" and body["max_tokens"] == 8000 and body["stream"] is True
    assert body["output_config"]["effort"] == "low" and "fallbacks" not in body
    for _ in range(3):  # replays use the stored reading: no second call
        v.do({"type": "view"})
    v.add("10")  # the same file again is recognised before anything is read
    assert len(api.requests) == 1


def test_a_stored_reading_goes_through_the_readers_checks_again():
    answer = sample_10_answer(fields={"total": {"value": "£420.00", "page": 1, "quote": "Amount payable £420.00"}})
    reader, _ = fake_reader(answer)
    v = Visitor(reader=reader)
    v.add("10")
    codes = v.codes("10")
    assert ("evidence_not_found", "blocking") in codes and ("total_mismatch", "blocking") in codes
    assert handle(v.session, {"type": "view"})["view"]["documents"][0]["findings"] == v.doc("10")["findings"]


@pytest.mark.parametrize("status, error_type, message, expected", [
    (429, "rate_limit_error", "This key has enforced_spend_limit_reached for the month.", "budget"),
    (400, "invalid_request_error", "You have reached your specified API usage limits.", "budget"),
    (429, "rate_limit_error", "Too many requests.", "busy"),
    (529, "overloaded_error", "Overloaded", "busy"),
    (400, "invalid_request_error", "Something else is wrong with the request.", "failed"),
])
def test_without_the_ai_reader_sample_10_goes_to_a_person(status, error_type, message, expected):
    from intake.demo_session import make_reader
    api = RefusingApi(status, error_type, message)
    v = Visitor(reader=make_reader(api.client))
    d = v.add("10")
    assert d["status"] == "needs_manual_entry" and d["ai"] == {"status": expected}
    assert ("reader_unknown_layout", "error") in v.codes("10") and d["type_in"]
    assert v.session["events"][0] == {"type": "add", "sha256": sample_sha(SAMPLES["10"]), "reader": "rules",
                                      "ai": expected}
    assert len(api.requests) == 1  # no retries


def test_the_visitor_can_switch_the_ai_reader_off():
    reader, api = fake_reader()
    v = Visitor(reader=reader, want_ai=False)
    assert v.add("10")["ai"] == {"status": "off"} and api.requests == []


def test_the_web_servers_limit_is_asked_only_right_before_an_ai_read():
    reader, api = fake_reader()
    asked = []

    def gate():
        asked.append(1)
        return "visitor_limit"

    out = handle(new_session(), {"type": "add", "sample": "01"}, reader=reader, ai_gate=gate)
    assert asked == []
    out = handle(out["session"], {"type": "add", "sample": "10"}, reader=reader, ai_gate=gate)
    assert asked == [1] and api.requests == [] and out["view"]["documents"][0]["ai"] == {"status": "visitor_limit"}


def test_ai_settings_never_stop_the_demo(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert demo_session.live_reader_from_env() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-local-test-not-a-real-key")
    monkeypatch.setenv("INTAKE_CLAUDE_MODEL", "not a model")
    monkeypatch.setenv("INTAKE_CLAUDE_EFFORT", "extreme")
    reader = demo_session.live_reader_from_env()
    assert reader is not None and reader.model == "claude-haiku-5-5" and reader.effort == "low"
    monkeypatch.setenv("INTAKE_DEMO_AI", "off")
    assert demo_session.live_reader_from_env() is None
    monkeypatch.setenv("INTAKE_DEMO_AI", "on")

    def broken(*args, **kwargs):
        raise SystemExit("a settings check failed")

    monkeypatch.setattr(demo_session, "make_reader", broken)
    assert demo_session.live_reader_from_env() is None


HOSTILE_NAME = "<b>Hi</b> Demo Ltd"
HOSTILE_LINE = "<img src=x onerror=alert(1)>"


def hostile_sample(folder: Path) -> "demo_session.Sample":
    """A PDF whose text carries HTML: the page must show it as text, never run it."""
    from samples.make_samples import invoice_pdf
    vendor = {"name": HOSTILE_NAME, "address": "<i>1 Road</i>", "email": None, "iban": "GB77 DEMO 4000 0110 0000 01"}
    lines = [("AC-100", HOSTILE_LINE, "200", "4.50", "900.00")]
    (folder / "99_hostile.pdf").write_bytes(invoice_pdf(vendor, "INV-9", "2026-10-05", None, "PO-2026-0101", lines,
                                                        "900.00", "20", "180.00", "1,080.00"))
    return demo_session.Sample("99", "99_hostile.pdf", "Hostile", "test", folder=folder)


def test_hostile_text_in_a_pdf_is_returned_as_plain_data(tmp_path, monkeypatch):
    monkeypatch.setitem(SAMPLES, "99", hostile_sample(tmp_path))
    v = Visitor()
    d = v.add("99")
    assert d["vendor"] == HOSTILE_NAME and d["lines"][0]["description"] == HOSTILE_LINE  # kept exactly, as data
    assert any(HOSTILE_NAME in f["message"] for f in d["findings"])


# ------------------------------------------------------------------ hardening from the first review
def test_a_slow_ai_answer_goes_to_a_person_before_the_function_times_out(monkeypatch):
    from test_claude import SlowApi
    monkeypatch.setattr(demo_session, "AI_DEADLINE", 0.3)
    api = SlowApi(gap=0.12, model="claude-haiku-5-5")
    v = Visitor(reader=demo_session.make_reader(api.client))
    d = v.add("10")
    assert d["ai"] == {"status": "busy"} and d["status"] == "needs_manual_entry"


def test_the_cost_is_priced_by_the_model_asked_for():
    reader, _ = fake_reader(served="claude-haiku-5-5-20261001")
    v = Visitor(reader=reader)
    d = v.add("10")
    reading = v.session["events"][0]["reading"]
    assert reading["model"] == "claude-haiku-5-5" and reading["served"] == "claude-haiku-5-5-20261001"
    assert d["ai"]["cost_usd"] > 0 and v.view["metrics"]["cost_usd"] > 0


def test_a_session_too_large_to_send_back_is_never_signed():
    v = Visitor()
    v.add("08")
    bulky = [{"sku": "x" * 200, "description": "y" * 200, "quantity": "1", "unit_price": "1", "line_total": "1"}] * 20
    sizes = []
    with pytest.raises(ActionError, match="session is full"):
        for _ in range(60):
            v.do({"type": "correct", "doc": "doc_08", "by": "sara", "fields": {}, "lines": bulky, "note": "z" * 500,
                  "version": v.doc("08")["version"]})
            sizes.append(len(json.dumps(v.session)))
    assert sizes and max(sizes) <= demo_session.MAX_SESSION_BYTES + 200
    assert handle(v.session, {"type": "view"})["view"]["documents"]  # the last session handed out still works


def test_stale_temporary_folders_are_cleared_at_start_up():
    import os
    import time
    old = Path(tempfile.mkdtemp(prefix=TMP_PREFIX))
    fresh = Path(tempfile.mkdtemp(prefix=TMP_PREFIX))
    (old / "intake.db").write_text("x")
    an_hour_ago = time.time() - 3600
    os.utime(old, (an_hour_ago, an_hour_ago))
    try:
        assert demo_session.clear_stale_folders() >= 1
        assert not old.exists() and fresh.exists()
    finally:
        fresh.rmdir()


def test_the_page_gets_plain_words_and_ticks_still_name_the_product_findings():
    v = Visitor()
    v.add("01")
    v.approve("01")
    copy = v.add("05")
    duplicate = next(f for f in copy["findings"] if f["code"] == "duplicate_invoice")
    assert "invoice 01" in duplicate["message"] and "doc_01" not in duplicate["message"]
    bank = v.add("04")
    message = next(f for f in bank["findings"] if f["code"] == "bank_changed")
    assert message["message"] == ("The IBAN on this invoice is not the one on file for Cobalt. Call Cobalt on the number "
                                  "on file, not the one on the invoice.")
    over = v.add("06")  # ticks use the ids the page shows: the product's own fingerprints, unchanged
    assert v.approve("06", ack=over["blocking"], note="Agreed with the buyer by phone.")["ok"]
    for d in v.view["documents"]:
        for f in d["findings"]:
            assert "vendor master" not in f["message"] and "2-way" not in f["message"] and "doc_" not in f["message"]
    assert all("vendor master" not in a["text"] and "doc_" not in a["text"] for a in v.view["audit"])


def test_renumbering_the_first_copy_cannot_get_both_copies_exported():
    """The second review's probe: Sara changes 01's number, date and PO; Omar approves 01 with every
    finding ticked; 05, the same invoice, must stay locked, and only one copy is exported."""
    v = Visitor()
    v.add("01")
    v.add("05")
    first = v.doc("01")
    assert v.do({"type": "correct", "doc": "doc_01", "by": "sara", "fields": {
        "invoice_number": "INV-A-1001-R", "invoice_date": "2026-09-01", "due_date": "2026-10-01",
        "po_number": "PO-2026-0104"}, "lines": None, "note": "x", "version": first["version"]})["ok"]
    assert v.approve("01", by="omar", ack=v.doc("01")["blocking"], note="Checked with the supplier by phone.")["ok"]
    copy = v.doc("05")
    assert copy["stamp"] == "locked" and ("duplicate_invoice", "error") in v.codes("05")
    assert not v.approve("05", by="sara", ack=copy["blocking"], note="Checked with the supplier by phone.")["ok"]
    assert not v.approve("05", by="omar")["ok"]
    v.do({"type": "export"})
    [batch] = v.view["exports"]
    rows = list(csv.DictReader(io.StringIO(next(f for f in batch["files"] if f["name"] == "invoices.csv")["content"])))
    assert [r["invoice_number"] for r in rows] == ["INV-A-1001-R"]
