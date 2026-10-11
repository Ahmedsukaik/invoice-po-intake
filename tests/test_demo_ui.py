"""The demo page in a real browser (an installed Chrome or Edge, through Playwright): the six step
story at four screen sizes, with the thing each step asks for fully on screen; free use; the AI
reader through a simulated API; hostile text in a PDF; downloads; motion and focus. Skipped when
Playwright or a browser is not available."""
from __future__ import annotations

from typing import Any

import pytest
from demo_fakes import fake_reader

from intake import demo_session
from intake.demo_web import create_app
from intake.localrun import free_port, serve

SIZES = [(1440, 900), (1280, 720), (390, 844), (360, 640)]


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        for channel in ("chrome", "msedge"):
            try:
                launched = p.chromium.launch(channel=channel)
                break
            except Exception:
                continue
        else:
            pytest.skip("no installed Chrome or Edge for Playwright")
        yield launched
        launched.close()


def start(reader: Any = None) -> tuple[str, Any]:
    port = free_port()
    return f"http://127.0.0.1:{port}", serve(create_app(reader=reader), port)


@pytest.fixture(scope="module")
def site():
    url, server = start()
    yield url
    server.should_exit = True


class Page:
    """A page at one screen size, collecting script errors."""

    def __init__(self, browser, url: str, width: int = 1440, height: int = 900, **context: Any):
        phone = width < 900
        self.size = (width, height)
        self.context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=phone, has_touch=phone,
                                           **context)
        self.page = self.context.new_page()
        self.errors: list[str] = []
        self.dialogs: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        self.page.on("dialog", lambda d: (self.dialogs.append(d.message), d.dismiss()))
        self.page.goto(url + "/")
        self.button("Start the demo").wait_for()

    def button(self, name: str):
        return self.page.get_by_role("button", name=name, exact=True)

    def title(self, text: str) -> None:
        self.page.locator(".story-title", has_text=text).wait_for()
        self.page.wait_for_timeout(120)  # one frame for the step to bring its target into view

    def on_screen(self, locator) -> None:
        """Fully inside the window, and nothing (such as the story sheet) on top of it."""
        locator.wait_for(state="visible")
        box = locator.bounding_box()
        width, height = self.size
        assert box, locator
        assert box["x"] >= -0.5 and box["y"] >= -0.5, (locator, box)
        assert box["x"] + box["width"] <= width + 0.5 and box["y"] + box["height"] <= height + 0.5, (locator, box)
        # A stamp or a mark lets clicks through to the sheet: then nothing may cover the sheet at that point.
        assert locator.evaluate("""el => { const r = el.getBoundingClientRect();
            const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
            const own = getComputedStyle(el).pointerEvents === "none" ? (el.closest(".sheet") || el.parentElement) : el;
            return !!hit && (hit === own || own.contains(hit) || hit.contains(own)); }"""), f"{locator} is covered"
        assert self.page.evaluate("document.documentElement.scrollWidth") <= width, "the page scrolls sideways"

    def tap(self, locator) -> None:
        self.on_screen(locator)
        locator.click()

    def close(self) -> None:
        self.context.close()
        assert self.errors == [] and self.dialogs == []


@pytest.mark.parametrize("width, height", SIZES, ids=[f"{w}x{h}" for w, h in SIZES])
def test_the_six_step_story_with_each_target_on_screen(browser, site, width, height):
    ui = Page(browser, site, width, height)
    page = ui.page
    assert "Invoices read, checked against the PO, and approved by a person." in page.locator("#story").inner_text()
    ui.tap(ui.button("Start the demo"))

    ui.title("An invoice arrives.")                                   # 1
    ui.tap(ui.button("Bring in invoice 01"))
    ui.title("Read, checked and matched to PO-2026-0101.")
    ui.on_screen(page.locator(".paper-pane .mark").first if width >= 900 else page.locator(".paper-pane .sheet"))
    ui.tap(page.get_by_role("button", name="Next: your turn"))
    ui.title("Your turn: approve it.")                                # 2
    approve = page.locator("[data-approve]")
    assert "ring" in (approve.get_attribute("class") or "")
    ui.tap(approve)
    ui.title("Approved, stamped and logged.")
    if width >= 900 or page.locator(".paper-pane").is_visible():
        ui.on_screen(page.locator(".paper-pane .stamp", has_text="Approved"))
    ui.tap(page.get_by_role("button", name="Next: new bank details"))

    ui.title("A supplier asks to be paid to a new account.")          # 3
    ui.tap(ui.button("Bring in invoice 04"))
    ui.title("New bank details: Approve is locked.")
    ui.on_screen(page.locator(".lockbox .stamp-sm", has_text="Locked"))
    if width >= 900:
        ui.on_screen(page.locator(".paper-pane .stamp", has_text="Locked"))
        ui.on_screen(page.locator(".paper-pane .pen-ring"))
    assert page.locator(".btn-locked").is_disabled()
    ui.tap(page.locator("[data-reject]"))
    assert "Calling Cobalt" in page.locator("textarea[data-reason]").input_value()  # a reason, ready to send
    ui.tap(page.get_by_role("button", name="Send rejection"))
    ui.title("Rejected, and it never reaches the export.")
    ui.tap(page.get_by_role("button", name="Next: an invoice over its order"))

    ui.title("More than was ordered.")                                 # 4
    ui.tap(ui.button("Bring in invoice 06"))
    ui.title("Tick each finding, say why, then approve.")
    for _ in range(2):
        tick = page.locator(".tick-row.ring")
        ui.tap(tick)
        page.wait_for_timeout(120)
    ui.tap(page.locator("[data-suggest]"))
    page.wait_for_timeout(120)
    approve = page.locator("[data-approve].ring")
    ui.tap(approve)
    ui.title("Approved, with two findings signed off.")
    ui.tap(page.get_by_role("button", name="Next: a layout it has never seen"))

    ui.title("A layout the rules have never seen.")                   # 5
    ui.tap(ui.button("Bring in invoice 10"))
    ui.title("Unknown layout: it goes to a person.")
    assert "not switched on" in page.evaluate("document.body.innerText")  # why, in the story or the review
    if width < 900:
        ui.on_screen(page.locator(".typebox"))
    else:
        ui.on_screen(page.locator(".paper-pane .sheet"))
    ui.tap(page.get_by_role("button", name="Next: export and audit trail"))

    ui.title("Export what was approved.")                             # 6
    ui.tap(page.locator("[data-export].ring"))
    ui.title("What you just did, all logged.")
    ui.on_screen(page.locator(".file-chip", has_text="invoices.csv"))
    audit = page.locator(".audit").inner_text()
    assert "Tried to approve" not in audit and "Rejected it" in audit and "Exported 2 documents" in audit
    ui.tap(page.get_by_role("button", name="Try it yourself"))
    page.locator(".story-title", has_text="Bring in any sample").wait_for()
    ui.tap(page.locator("#story [data-add='05']"))  # free use: the story offers the next samples to try
    page.locator(".doc-vendor", has_text="Acme").wait_for()
    ui.close()


def test_no_jargon_on_the_way(browser, site):
    ui = Page(browser, site)
    ui.button("Start the demo").click()
    ui.button("Bring in invoice 01").click()
    ui.title("Read, checked and matched")
    visible = ui.page.evaluate("document.body.innerText").lower()
    for word in ("token", "sha-256", "doc_01", "fingerprint", "needs_review", "http"):
        assert word not in visible, word
    ui.close()


def test_hovering_a_value_marks_its_line_on_the_page(browser, site):
    ui = Page(browser, site)
    ui.button("Start the demo").click()
    ui.button("Bring in invoice 01").click()
    ui.title("Read, checked and matched")
    ui.page.locator(".f-row[data-key='total']").hover()
    active = ui.page.locator(".mark.active")
    assert active.count() == 1 and "total" in (active.get_attribute("data-mark") or "")
    assert ui.page.locator(".mark").count() >= 10  # every value and line read from the PDF
    ui.close()


def test_the_page_keeps_its_place_after_a_reload(browser, site):
    ui = Page(browser, site)
    ui.button("Start the demo").click()
    ui.button("Bring in invoice 01").click()
    ui.page.locator("[data-approve]").click()
    ui.title("Approved, stamped and logged.")
    ui.page.reload()
    ui.title("Approved, stamped and logged.")
    assert ui.page.locator(".paper-pane .stamp", has_text="Approved").count() == 1
    ui.close()


def test_four_eyes_with_a_scan_in_free_use(browser, site):
    ui = Page(browser, site)
    page = ui.page
    page.get_by_role("button", name="Skip to free use").click()
    page.locator(".add-row[data-add='08']").click()
    page.locator(".decision [data-type]").click()
    page.locator("[data-fill]").click()
    assert page.locator("[data-tf='invoice_number']").input_value() == "INV-A-1003"
    page.locator("[data-savetype]").click()
    page.locator(".four-eyes").wait_for()
    assert page.locator("[data-approve]").is_disabled()  # Sara typed it in, so Sara cannot approve it
    page.locator(".four-eyes [data-reviewer='omar']").click()
    page.locator(".tick-row").first.click()
    page.locator("textarea[data-note]").fill("Compared the IBAN with the vendor master.")
    page.get_by_role("button", name="Approve as Omar").click()
    page.locator(".decided", has_text="Approved by Omar").wait_for()
    ui.close()


def test_a_duplicate_of_an_approved_invoice_is_locked(browser, site):
    ui = Page(browser, site)
    page = ui.page
    page.get_by_role("button", name="Skip to free use").click()
    page.locator(".add-row[data-add='01']").click()
    page.locator("[data-approve]").click()
    page.locator(".decided").wait_for()
    page.locator(".add-row[data-add='05']").click()
    page.locator(".lockbox", has_text="Same invoice already in the system").wait_for()
    assert page.locator(".paper-pane .stamp", has_text="Locked").count() == 1
    ui.close()


def test_the_export_downloads_files_made_in_the_browser(browser, site):
    ui = Page(browser, site)
    page = ui.page
    page.get_by_role("button", name="Skip to free use").click()
    page.locator(".add-row[data-add='01']").click()
    page.locator("[data-approve]").click()
    page.locator(".decided").wait_for()
    page.locator(".books-link").click()
    page.locator("[data-export]").click()
    with page.expect_download() as info:
        page.locator(".file-chip", has_text="invoices.csv").click()
    download = info.value
    assert download.suggested_filename.endswith("-invoices.csv")
    text = open(download.path(), encoding="utf-8").read()
    assert "INV-A-1001" in text and "sara (reviewer)" in text
    ui.close()


def test_with_the_ai_reader_sample_10_is_read_with_source_lines_and_costed(browser):
    reader, api = fake_reader()
    url, server = start(reader)
    try:
        ui = Page(browser, url)
        page = ui.page
        ui.button("Start the demo").click()
        ui.button("Bring in invoice 01").click()
        page.locator("[data-approve]").click()
        ui.title("Approved, stamped")
        for name in ("Next: new bank details", "Bring in invoice 04"):
            page.get_by_role("button", name=name).click()
        page.locator("[data-reject]").click()
        page.get_by_role("button", name="Send rejection").click()
        page.get_by_role("button", name="Next: an invoice over its order").click()
        ui.button("Bring in invoice 06").click()
        page.locator(".tick-row.ring").click()
        page.locator(".tick-row.ring").click()
        page.locator("[data-suggest]").click()
        page.locator("[data-approve]").click()
        page.get_by_role("button", name="Next: a layout it has never seen").click()
        assert "The AI reader, Claude Haiku 5.5, reads it." in page.locator("#story").inner_text()
        ui.button("Bring in invoice 10").click()
        ui.title("Read by AI, each value with its source line.")
        assert "Read by AI" in page.locator(".doc-head").inner_text()
        assert page.locator(".paper-pane .mark").count() >= 10
        assert "$0.0008" in page.locator(".ai-box").inner_text()
        page.get_by_role("button", name="Next: export and audit trail").click()
        page.locator("[data-export]").click()
        ui.title("What you just did")
        figures = page.locator(".story-figures").inner_text()
        assert "$0.0008" in figures and "$0.76" in figures
        assert len(api.requests) == 1
        ui.close()
    finally:
        server.should_exit = True


def test_hostile_text_in_a_pdf_is_shown_as_text(browser, tmp_path, monkeypatch):
    from test_demo_session import HOSTILE_LINE, HOSTILE_NAME, hostile_sample
    monkeypatch.setitem(demo_session.SAMPLES, "99", hostile_sample(tmp_path))
    url, server = start()
    try:
        ui = Page(browser, url)
        page = ui.page
        page.get_by_role("button", name="Skip to free use").click()
        page.locator(".add-row[data-add='99']").click()
        page.locator(".doc-vendor", has_text=HOSTILE_NAME).wait_for()
        page.wait_for_timeout(300)
        assert HOSTILE_LINE in page.locator(".lines-table").inner_text()
        assert page.locator(".doc-vendor").inner_text() == HOSTILE_NAME  # the tags show as text, not as markup
        assert page.locator("img[src='x']").count() == 0
        assert page.evaluate("window.__xss") is None
        ui.close()  # also asserts that no alert() was opened
    finally:
        server.should_exit = True


def test_motion_stops_when_the_visitor_asks_for_less(browser, site):
    ui = Page(browser, site, reduced_motion="reduce")
    ui.button("Start the demo").click()
    ui.button("Bring in invoice 01").click()
    ui.page.locator("[data-approve]").click()
    stamp = ui.page.locator(".paper-pane .stamp")
    stamp.wait_for()
    assert stamp.evaluate("el => getComputedStyle(el).animationName") == "none"
    ui.close()


def test_keyboard_focus_is_visible(browser, site):
    ui = Page(browser, site)
    for _ in range(6):
        ui.page.keyboard.press("Tab")
        if ui.page.evaluate("document.activeElement.textContent.trim()") == "Start the demo":
            break
    focused = ui.page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    assert ui.page.evaluate("document.activeElement.textContent.trim()") == "Start the demo" and focused != "none"
    ui.close()


def test_dark_mode_keeps_the_sheet_white_and_the_text_readable(browser, site):
    ui = Page(browser, site, color_scheme="dark")
    ui.button("Start the demo").click()
    ui.button("Bring in invoice 01").click()
    ui.title("Read, checked and matched")
    page = ui.page
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(19, 22, 27)"
    assert page.locator(".paper-pane .sheet").evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(255, 255, 255)"
    ui.close()
