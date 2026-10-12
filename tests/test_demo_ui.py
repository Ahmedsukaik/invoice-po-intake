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

    def story_text_whole(self) -> None:
        """The story's text is shown in full (or not at all), never cut off mid-sentence."""
        for el in self.page.locator(".story-text").all():
            if el.is_visible():
                assert el.evaluate("e => e.scrollHeight <= e.clientHeight + 1"), el.inner_text()

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
    ui.story_text_whole()
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
    ui.on_screen(page.locator(".lockbox .crop-img"))  # the IBAN line, cut from the page, readable at any size
    assert "IBAN: GB40 DEMO 4000 9999 9999 99" in (page.locator(".lockbox .crop").get_attribute("aria-label") or "")
    ui.tap(page.locator("[data-reject]"))
    assert "Calling Cobalt" in page.locator("textarea[data-reason]").input_value()  # a reason, ready to send
    reason, send = page.locator("textarea[data-reason]").bounding_box(), page.locator("[data-reject]").bounding_box()
    assert reason and send and reason["y"] + reason["height"] <= send["y"]  # the box comes before the button
    ui.tap(page.get_by_role("button", name="Send rejection"))
    ui.title("Rejected, and it never reaches the export.")
    ui.tap(page.get_by_role("button", name="Next: an invoice over its order"))

    ui.title("More than was ordered.")                                 # 4
    ui.tap(ui.button("Bring in invoice 06"))
    ui.title("Tick each finding, say why, then approve.")
    assert page.locator(".signoff .crop").count() == 2  # line 1 and the subtotal, as on the page
    widths = page.locator(".signoff .crop-img").evaluate_all("els => els.map((e) => e.getBoundingClientRect().width)")
    assert len(widths) >= 3 and max(widths) <= 330  # the long line comes in pieces, none shrunk below reading size
    for _ in range(2):
        tick = page.locator(".tick-row.ring")
        ui.tap(tick)
        page.wait_for_timeout(120)
    ui.tap(page.locator("[data-suggest]"))
    page.wait_for_timeout(120)
    approve = page.locator("[data-approve].ring")
    ui.tap(approve)
    ui.title("Approved, with two findings signed off.")
    ui.story_text_whole()
    ui.tap(page.get_by_role("button", name="Next: a layout it has never seen"))

    ui.title("A layout the rules have never seen.")                   # 5
    ui.tap(ui.button("Bring in invoice 10"))
    ui.title("Unknown layout: held for a person, never guessed.")
    assert "off in this demo" not in page.evaluate("document.body.innerText")  # said once, in the cost card
    if width < 900:
        ui.on_screen(page.locator(".typebox"))
    else:
        ui.on_screen(page.locator(".paper-pane .sheet"))
    ui.tap(page.get_by_role("button", name="Next: export and audit trail"))

    ui.title("Export what was approved.")                             # 6
    ui.tap(page.locator("[data-export].ring"))
    ui.title("1 approved for payment, 1 bank change stopped, 1 overrun signed off, 1 held for a person.")
    ui.on_screen(page.locator(".story-note"))
    ui.on_screen(page.locator(".file-chip", has_text="invoices.csv"))
    assert page.locator(".export-card .btn", has_text="Nothing left to export").is_disabled()
    assert "The AI reader is off in this demo right now" in page.locator(".cost-card").inner_text()
    audit = page.locator(".audit").inner_text()
    assert "Tried to approve" not in audit and "Rejected it" in audit and "Exported 2 documents" in audit
    page.locator(".audit-card").scroll_into_view_if_needed()  # read the audit trail, far down the export view
    ui.tap(page.get_by_role("button", name="Try it yourself"))
    page.locator(".story-title", has_text="Bring in any sample").wait_for()
    ui.tap(page.locator("#story [data-add='05']"))  # free use: the story offers the next samples to try
    page.locator(".doc-vendor", has_text="Acme").wait_for()
    assert page.locator("#story .tried", has_text="05, a duplicate").count() == 1  # tried: ticked, not crossed out
    if width < 900:
        assert page.locator("#review-pane").evaluate("el => el.scrollTop") == 0  # a new document opens at its top
    for key in ("08", "09"):
        ui.tap(page.locator(f"#story [data-add='{key}']"))
        page.locator(".doc-file", has_text=key + "_").wait_for()
    ui.tap(page.locator("#story [data-go='books']", has_text="Export them"))
    ui.on_screen(page.locator(".export-card .btn"))  # the export view opens at its top, not where it was left
    ui.close()


def test_no_jargon_on_the_way(browser, site):
    ui = Page(browser, site)
    ui.button("Start the demo").click()
    ui.button("Bring in invoice 01").click()
    ui.title("Read, checked and matched")
    visible = ui.page.evaluate("document.body.innerText").lower()
    for word in ("token", "sha-256", "doc_01", "fingerprint", "needs_review", "http", "vendor master", "rules reader",
                 "four eyes", "four-eyes", "2-way", "ocr"):
        assert word not in visible, word
    ui.close()


def test_plain_words_on_the_locked_and_the_duplicate_invoice(browser, site):
    ui = Page(browser, site)
    page = ui.page
    page.get_by_role("button", name="Skip to free use").click()
    page.locator(".add-row[data-add='04']").click()
    lock = page.locator(".lockbox")
    lock.wait_for()
    assert "The IBAN on this invoice is not the one on file for Cobalt. Call Cobalt on the number on file" in lock.inner_text()
    assert "Nobody can approve this. Reject it." in lock.inner_text()
    page.locator(".add-row[data-add='01']").click()
    page.locator("[data-approve]").click()
    page.locator(".decided").wait_for()
    page.locator(".add-row[data-add='05']").click()
    text = page.locator(".lockbox").inner_text()
    assert "already approved as invoice 01" in text and "doc_01" not in text and "vendor master" not in text
    ui.close()


def test_on_a_touch_screen_one_tap_on_a_value_shows_its_line(browser, site):
    ui = Page(browser, site, 390, 844)
    page = ui.page
    assert page.evaluate("matchMedia('(hover: none)').matches")
    ui.button("Start the demo").tap()
    ui.button("Bring in invoice 01").tap()
    ui.title("Read, checked and matched")
    assert "Every value in Review came from the line marked on the page; tap one" in page.locator("#story").inner_text()
    page.locator(".switch-row [data-pane='review']").tap()
    row = page.locator(".f-row[data-key='total']")
    row.scroll_into_view_if_needed()
    box = row.bounding_box()
    assert box
    page.touchscreen.tap(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.locator(".paper-pane").wait_for(state="visible")
    active = page.locator(".mark.active")
    assert active.count() == 1 and "total" in (active.get_attribute("data-mark") or "")
    ui.close()


def test_step_1_points_at_the_total_then_the_po_number(browser, site):
    ui = Page(browser, site)
    ui.button("Start the demo").click()
    ui.page.mouse.move(5, 5)
    ui.button("Bring in invoice 01").click()
    ui.title("Read, checked and matched")
    ui.page.mouse.move(5, 5)
    assert "total" in (ui.page.locator(".mark.active").get_attribute("data-mark") or "")
    ui.page.locator(".mark.active[data-mark~='po_number']").wait_for(timeout=4000)
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
        ui.title("1 read by AI")
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


def test_when_both_reviewers_changed_values_the_page_says_reject_it(browser, site):
    """Sara types the scan in without the total, Omar adds it: now neither may approve, and the
    page says so instead of sending each to the other."""
    ui = Page(browser, site)
    page = ui.page
    page.get_by_role("button", name="Skip to free use").click()
    page.locator(".add-row[data-add='08']").click()
    page.locator(".decision [data-type]").click()
    page.locator("[data-fill]").click()
    page.locator("[data-tf='total']").fill("")
    page.locator("[data-savetype]").click()
    page.locator(".lockbox", has_text="Fix the typed values first").wait_for()
    page.locator(".who-row [data-reviewer='omar']").click()
    page.locator(".decision [data-type]").click()
    page.locator("[data-tf='total']").fill("540.00")
    page.locator("[data-savetype]").click()
    note = page.locator(".four-eyes")
    note.wait_for()
    assert "Sara and Omar both changed values, so neither can approve; reject it." in note.inner_text()
    assert page.locator(".four-eyes [data-reviewer]").count() == 0 and page.locator("[data-approve]").is_disabled()
    page.locator(".who-row [data-reviewer='sara']").click()
    assert "neither can approve" in page.locator(".four-eyes").inner_text() and page.locator("[data-approve]").is_disabled()
    ui.close()


def test_on_a_phone_the_toast_stays_clear_and_goes_with_its_session(browser, site):
    ui = Page(browser, site, 390, 664)
    page = ui.page
    page.get_by_role("button", name="Skip to free use").tap()
    page.locator(".add-row[data-add='01']").tap()
    page.locator("[data-approve]").tap()
    page.locator(".decided").wait_for()
    page.locator(".switch-row [data-pane='inbox']").tap()
    page.locator(".books-link").tap()
    page.route("**/api/step", lambda route: route.fulfill(status=500, json={"error": "server", "message": "Try again."}))
    page.locator("[data-export]").tap()  # fails: the page says so in a toast
    toast = page.locator("#toast")
    toast.wait_for(state="visible")
    page.unroute("**/api/step")
    ui.errors = [e for e in ui.errors if "status of 500" not in e]  # the failure this test made on purpose
    back, box = page.locator(".back-link").bounding_box(), toast.bounding_box()
    assert back and box and (box["y"] >= back["y"] + back["height"] or box["y"] + box["height"] <= back["y"])
    story = page.locator("#story").bounding_box()
    assert story and box["y"] + box["height"] <= story["y"]  # above the story sheet, not over it
    page.locator("#replay").tap()
    page.locator(".story-title", has_text="An invoice arrives.").wait_for()
    assert toast.is_hidden()
    ui.close()
