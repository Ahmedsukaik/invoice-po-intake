"""The web demo's HTTP API, as the page uses it, and its guards."""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import pytest
from demo_fakes import DemoClient, fake_reader, raw_post
from fastapi.testclient import TestClient

from intake import demo_session, demo_web
from intake.demo_session import TMP_PREFIX
from intake.demo_web import MAX_BODY, Limiter, create_app

PUBLIC = Path(__file__).resolve().parent.parent / "public"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(reader=None))


def leftovers() -> list[str]:
    return sorted(p.name for p in Path(tempfile.gettempdir()).iterdir() if p.name.startswith(TMP_PREFIX))


def test_config_lists_the_samples_and_the_reviewers(client):
    cfg = client.get("/api/config").json()
    assert cfg["live"] is False and cfg["model"] is None and cfg["today"] == "2026-10-11"
    assert [s["key"] for s in cfg["samples"]] == [f"{n:02d}" for n in range(1, 11)]
    assert {s["layout"] for s in cfg["samples"]} == {"demo", "scan", "other"}
    assert [r["id"] for r in cfg["reviewers"]] == ["sara", "omar"]
    assert cfg["repo"] == "https://github.com/Ahmedsukaik/invoice-po-intake"


def test_config_says_when_the_ai_reader_is_on():
    reader, _ = fake_reader()
    cfg = TestClient(create_app(reader=reader)).get("/api/config").json()
    assert cfg["live"] is True and cfg["model"] == "claude-haiku-5-5"


def test_the_whole_story_over_http(client):
    demo = DemoClient(client)
    assert demo.add("01")["blocking"] == []
    assert demo.approve("01")["ok"]
    assert demo.add("04")["stamp"] == "locked"
    refused = demo.approve("04")
    assert refused["ok"] is False and "cannot be approved" in refused["message"]
    demo.step({"type": "reject", "doc": "doc_04", "by": "sara", "reason": "New bank details: calling the supplier."})
    over = demo.add("06")
    assert demo.approve("06", ack=over["blocking"], note="Buyer agreed the extra plates by phone.")["ok"]
    assert demo.add("10")["status"] == "needs_manual_entry"
    demo.step({"type": "export"})
    assert demo.view["counts"] == {"waiting": 1, "approved": 0, "exported": 2, "rejected": 1}
    assert [f["name"] for f in demo.view["exports"][0]["files"]][-1] == "batch.json"


def test_the_page_can_reload_and_show_the_same_state(client):
    demo = DemoClient(client)
    demo.add("01")
    before = demo.view
    demo.step({"type": "view"})
    assert demo.view == before and demo.result is None


def test_the_ai_reader_over_http_is_called_once():
    reader, api = fake_reader()
    demo = DemoClient(TestClient(create_app(reader=reader)))
    d = demo.add("10")
    assert d["ai"]["status"] == "ok" and d["status"] == "needs_review"
    demo.step({"type": "view"})
    demo.step({"type": "view"})
    assert len(api.requests) == 1 and demo.view["metrics"]["ai_reads"] == 1


def test_a_visitor_over_the_ai_limit_gets_a_person_instead(monkeypatch):
    monkeypatch.setattr(demo_web, "PER_VISITOR_AI", (1, 600))
    reader, api = fake_reader()
    client = TestClient(create_app(reader=reader))
    assert DemoClient(client).add("10")["ai"]["status"] == "ok"
    second = DemoClient(client).add("10")
    assert second["ai"] == {"status": "visitor_limit"} and second["status"] == "needs_manual_entry"
    assert len(api.requests) == 1


def test_the_instance_has_its_own_ai_limit(monkeypatch):
    monkeypatch.setattr(demo_web, "PER_INSTANCE_AI", (0, 3600))
    reader, api = fake_reader()
    assert DemoClient(TestClient(create_app(reader=reader))).add("10")["ai"] == {"status": "busy"}
    assert api.requests == []


def test_the_page_can_ask_for_the_rules_reader_only():
    reader, api = fake_reader()
    demo = DemoClient(TestClient(create_app(reader=reader)))
    assert demo.add("10", ai=False)["ai"] == {"status": "off"} and api.requests == []


def test_a_changed_session_asks_the_page_to_start_again(client):
    demo = DemoClient(client)
    demo.add("01")
    session = json.loads(json.dumps(demo.session))
    session["events"][0]["reader"] = "claude"
    res = client.post("/api/step", json={"session": session, "action": {"type": "view"}})
    assert res.status_code == 409 and res.json()["error"] == "session"


def test_an_impossible_action_is_explained_and_not_recorded(client):
    demo = DemoClient(client)
    res = client.post("/api/step", json={"session": demo.session, "action": {"type": "approve", "doc": "doc_01",
                                                                            "by": "sara", "version": 1}})
    assert res.status_code == 409 and res.json() == {"error": "action", "message": "That document is not in this demo yet."}


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b"x" * (MAX_BODY + 1)], ids=["not json", "a list", "too big"])
def test_bad_bodies_are_refused(client, raw):
    res = client.post("/api/step", content=raw, headers={"Content-Type": "application/json"})
    assert res.status_code == 409 and res.json()["error"] == "action"


def test_nan_and_deep_nesting_are_refused(client):
    assert raw_post(client, '{"session": NaN, "action": {"type": "view"}}').status_code == 409
    deep = "[" * 5000 + "]" * 5000
    assert raw_post(client, '{"session": ' + deep + "}").status_code == 409
    nested: dict = {}
    cursor = nested
    for _ in range(30):
        cursor["a"] = {}
        cursor = cursor["a"]
    assert client.post("/api/step", json={"session": nested, "action": {"type": "view"}}).status_code == 409


@pytest.mark.parametrize("change", ['"sig": "' + "\\u00e9" * 64 + '"', '"sig": 12', '"v": 1.0e400'],
                         ids=["non-ascii signature", "number signature", "huge number"])
def test_odd_signatures_are_refused_not_crashing(client, change):
    session = client.post("/api/new").json()["session"]
    body = json.dumps(session)[:-1] + ", " + change + "}"
    res = raw_post(client, '{"session": ' + body + ', "action": {"type": "view"}}')
    assert res.status_code == 409


def test_every_kind_of_request_is_counted_per_visitor(monkeypatch):
    monkeypatch.setattr(demo_web, "PER_VISITOR_REQUESTS", (3, 600))
    client = TestClient(create_app(reader=None))
    session = client.post("/api/new").json()["session"]
    codes = [client.post("/api/step", json={"session": session, "action": {"type": "view"}}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 409]


def test_x_forwarded_for_is_ignored_unless_running_on_vercel(monkeypatch):
    monkeypatch.setattr(demo_web, "PER_VISITOR_REQUESTS", (2, 600))
    monkeypatch.delenv("VERCEL", raising=False)
    client = TestClient(create_app(reader=None))
    session = client.post("/api/new").json()["session"]
    codes = [client.post("/api/step", json={"session": session, "action": {"type": "view"}},
                         headers={"x-forwarded-for": f"10.0.0.{i}"}).status_code for i in range(3)]
    assert codes == [200, 200, 409]


def test_a_visitor_can_have_only_two_requests_in_progress():
    limiter = Limiter()
    assert limiter.enter("a") and limiter.enter("a") and not limiter.enter("a")
    limiter.leave("a")
    assert limiter.enter("a") and limiter.enter("b")


def test_the_limiter_forgets_the_oldest_visitors_first(monkeypatch):
    monkeypatch.setattr(demo_web, "MAX_KEYS", 3)
    limiter = Limiter()
    for key in "abcd":
        limiter.allow(key, 1, 600)
    assert limiter.allow("a", 1, 600) and not limiter.allow("d", 1, 600)


def test_the_temporary_folder_is_removed_after_every_request(client, monkeypatch):
    before = leftovers()
    demo = DemoClient(client)
    demo.add("01")
    demo.approve("01")
    client.post("/api/step", json={"session": demo.session, "action": {"type": "add", "sample": "nope"}})
    client.post("/api/step", json={"session": {"v": 1, "events": [{"type": "x"}], "sig": "0" * 64}})

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(demo_session, "view", boom)
    broken = TestClient(create_app(reader=None), raise_server_exceptions=False)
    res = broken.post("/api/step", json={"session": demo.session, "action": {"type": "view"}})
    assert res.status_code == 500 and res.json()["error"] == "server" and "boom" not in res.text
    assert leftovers() == before


def test_the_sample_pdfs_are_served_and_nothing_else(client):
    res = client.get("/api/samples/04/pdf")
    assert res.status_code == 200 and res.headers["content-type"] == "application/pdf"
    assert res.content.startswith(b"%PDF-") and "inline" in res.headers["content-disposition"]
    for key in ("99", "..", "01.pdf", "%2e%2e"):
        assert client.get(f"/api/samples/{key}/pdf").status_code == 404


def test_the_page_its_files_and_the_previews_are_served_locally(client):
    page = client.get("/")
    assert page.status_code == 200 and "Invoice and PO Intake" in page.text
    for name in ("app.js", "style.css", "tokens.css", "samples/previews.json", "samples/01_clean_invoice.png"):
        assert client.get("/" + name).status_code == 200, name
    assert client.get("/healthz").json() == {"status": "ok"}


def test_every_sample_has_a_preview_whose_lines_match_the_text_layer():
    from intake.pdftext import read_text
    previews = json.loads((PUBLIC / "samples" / "previews.json").read_text(encoding="utf-8"))
    for sample in demo_session.SAMPLES.values():
        preview = previews[sample.file]
        assert (PUBLIC / preview["png"]).is_file()
        text = read_text(sample.path.read_bytes()).pages[0]
        expected = [line.strip() for line in text.split("\n") if line.strip()]
        assert [line["text"] for line in preview["lines"]] == expected, sample.file
        assert all(0 <= x <= 1 for line in preview["lines"] for x in line["box"])


def test_the_page_has_no_contact_details_and_one_link():
    for name in ("index.html", "app.js"):
        page = (PUBLIC / name).read_text(encoding="utf-8")
        assert not re.findall(r"[\w.+-]+@[a-z][\w-]*\.[a-z]{2,}", page), name
        assert "mailto:" not in page and "tel:" not in page and "wa.me" not in page
        links = set(re.findall(r"https?://[^\s\"'`<>)]+", page))
        assert links <= {"https://github.com/Ahmedsukaik/invoice-po-intake", "https://fonts.googleapis.com",
                         "https://fonts.gstatic.com", "http://www.w3.org/2000/svg",
                         "https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,500;8..60,600"
                         "&family=Public+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap"}, name


def test_the_page_uses_plain_words_and_no_dashes_as_punctuation():
    for name in ("index.html", "app.js"):
        page = (PUBLIC / name).read_text(encoding="utf-8")
        assert "—" not in page and "–" not in page, name
