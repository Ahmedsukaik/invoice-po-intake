"""Record the web demo's story and take the README's screenshots of it.

    python docs/make_media.py                      (starts the demo on a free local port, rules reader only)
    python docs/make_media.py https://...          (records a running copy, for example the deployed demo)

Needs Playwright with an installed Chrome or Edge, and ffmpeg on the PATH for the GIF and MP4.
Writes docs/img/demo-tour.gif, docs/img/demo-tour.mp4 and docs/img/demo-*.png:

    demo-0-intro.png        the first screen (1440x900)
    demo-1-read.png         step 1: invoice 01 read, each value marked on the page
    demo-3-locked.png       step 3: new bank details, Approve locked
    demo-5-layout.png       step 5: a layout the rules do not know
    demo-6-export.png       step 6: the export, the audit trail and the cost
    demo-3-phone.png        step 3 on a phone (390x664)
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "img"
SIZE = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 664}


def click(page: Page, name: str) -> None:
    page.get_by_role("button", name=name, exact=True).click()


def title(page: Page, text: str) -> None:
    page.locator(".story-title", has_text=text).wait_for(timeout=90000)


def story(page: Page, url: str, shots: bool, pause: float = 1.0) -> None:
    """The six steps, with pauses a viewer can follow."""
    wait = lambda ms: page.wait_for_timeout(int(ms * pause))  # noqa: E731
    shot = lambda name: page.screenshot(path=str(OUT / name)) if shots else None  # noqa: E731
    page.goto(url + "/")
    page.get_by_role("button", name="Start the demo").wait_for()
    page.evaluate("sessionStorage.clear()")
    page.reload()
    page.get_by_role("button", name="Start the demo").wait_for()
    wait(2600)
    shot("demo-0-intro.png")
    click(page, "Start the demo")
    wait(1600)
    click(page, "Bring in invoice 01")
    title(page, "Read, checked and matched")
    wait(1200)
    page.locator(".f-row[data-key='invoice_number']").hover()
    wait(1600)
    shot("demo-1-read.png")
    page.locator(".f-row[data-key='total']").hover()
    wait(1400)
    page.locator(".f-row[data-key='po_number']").hover()
    wait(1400)
    page.mouse.move(700, 160)
    click(page, "Next: your turn")
    wait(1400)
    page.locator("[data-approve]").click()
    title(page, "Approved, stamped")
    wait(2600)
    click(page, "Next: new bank details")
    wait(1200)
    click(page, "Bring in invoice 04")
    title(page, "Approve is locked")
    wait(2800)
    shot("demo-3-locked.png")
    page.locator("[data-reject]").click()
    wait(1400)
    page.get_by_role("button", name="Send rejection").click()
    title(page, "Rejected")
    wait(1600)
    click(page, "Next: an invoice over its order")
    wait(1000)
    click(page, "Bring in invoice 06")
    title(page, "Tick each finding")
    wait(2200)
    for _ in range(2):
        page.locator(".tick-row.ring").click()
        wait(800)
    page.locator("[data-suggest]").click()
    wait(1200)
    page.locator("[data-approve]").click()
    title(page, "Approved, with two")
    wait(2400)
    click(page, "Next: a layout it has never seen")
    wait(1000)
    click(page, "Bring in invoice 10")
    page.get_by_role("button", name="Next: export and audit trail").wait_for(timeout=90000)
    wait(2800)
    shot("demo-5-layout.png")
    click(page, "Next: export and audit trail")
    wait(1600)
    page.locator("[data-export]").click()
    title(page, "What you just did")
    wait(3600)
    shot("demo-6-export.png")


def phone_step_3(page: Page, url: str) -> None:
    page.goto(url + "/")
    page.get_by_role("button", name="Start the demo").wait_for()
    page.evaluate("sessionStorage.clear()")
    page.reload()
    click(page, "Start the demo")
    click(page, "Bring in invoice 01")
    page.get_by_role("button", name="Next: your turn").click()
    page.locator("[data-approve]").click()
    page.get_by_role("button", name="Next: new bank details").click()
    click(page, "Bring in invoice 04")
    title(page, "Approve is locked")
    page.wait_for_timeout(1500)
    page.screenshot(path=str(OUT / "demo-3-phone.png"))


def record(url: str) -> None:
    OUT.mkdir(exist_ok=True)
    tmp = tempfile.mkdtemp()  # left for the system to clean: Windows can keep the video file open for a moment
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        shots = browser.new_context(viewport=SIZE, device_scale_factor=1, color_scheme="light")
        story(shots.new_page(), url, shots=True, pause=0.4)
        shots.close()
        phone = browser.new_context(viewport=PHONE, device_scale_factor=2, is_mobile=True, has_touch=True, color_scheme="light")
        phone_step_3(phone.new_page(), url)
        phone.close()
        video = browser.new_context(viewport=SIZE, device_scale_factor=1, color_scheme="light", record_video_dir=tmp,
                                    record_video_size=SIZE)
        page = video.new_page()
        story(page, url, shots=False)
        path = page.video.path() if page.video else None
        video.close()
        browser.close()
    if path and shutil.which("ffmpeg"):
        webm = Path(tmp) / "tour.webm"
        shutil.copy(path, webm)
        mp4 = OUT / "demo-tour.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-crf", "26", "-movflags", "+faststart", str(mp4)], check=True)
        palette = Path(tmp) / "palette.png"
        scale = "fps=6,scale=960:-1:flags=lanczos"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4), "-vf", f"{scale},palettegen=max_colors=96",
                        str(palette)], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4), "-i", str(palette), "-lavfi",
                        f"{scale} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=4", str(OUT / "demo-tour.gif")],
                       check=True)
    print("written:", ", ".join(sorted(f.name for f in OUT.iterdir() if f.name.startswith("demo-"))))


def main() -> None:
    if len(sys.argv) > 1:
        record(sys.argv[1].rstrip("/"))
        return
    from intake.demo_web import create_app
    from intake.localrun import free_port, serve

    port = free_port()
    server = serve(create_app(reader=None), port)
    try:
        record(f"http://127.0.0.1:{port}")
    finally:
        server.should_exit = True


if __name__ == "__main__":
    main()
