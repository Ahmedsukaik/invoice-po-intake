"""Renders each demo PDF to a PNG for the web demo, and finds where each line of its text
layer sits on the page.   Run:  python samples/make_previews.py   (needs pypdfium2, a
development dependency; the demo itself never renders a PDF).

Writes public/samples/<name>.png and public/samples/previews.json:

    {"01_clean_invoice.pdf": {"png": "samples/01_clean_invoice.png", "width": 1240, "height": 1754,
                              "lines": [{"text": "Invoice number: INV-A-1001", "box": [x0, y0, x1, y1],
                                         "cells": [[x0, x1], ...]}]}}

The lines are the text layer as pypdf reads it, which is what the rules reader quotes as the
source of each value, so the page can draw a box around the line a value came from. Boxes are
fractions of the page, from the top left corner. Every demo PDF has one page; only page 1 is
rendered. A scan has no text layer, so it has no lines. Each page is cropped to what is
printed on it, without the fine-print footer, so the text shows larger in the page.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intake.pdftext import read_text  # noqa: E402

PDFS = ROOT / "samples" / "pdfs"
OUT = ROOT / "public" / "samples"
WIDTH = 1240  # pixels: A4 at 150 dpi, sharp on a high density screen at the size the page shows it
FOOTER = "Demo document generated for a reference build."


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def line_boxes(path: Path, page_width: float, page_height: float) -> list[dict]:
    """[{"text", "box"}] for each line of page 1 as pypdf reads it, located with pdfium's
    character boxes: the line's characters (spaces aside) are found in order on the page, and
    the box is the union of theirs."""
    pages = read_text(path.read_bytes()).pages
    if not pages or not pages[0].strip():
        return []
    doc = pdfium.PdfDocument(str(path))
    try:
        textpage = doc[0].get_textpage()
        chars: list[tuple[str, tuple[float, float, float, float]]] = []
        for index in range(textpage.count_chars()):
            ch = textpage.get_text_range(index, 1)
            if ch.strip():
                chars.append((ch.casefold(), textpage.get_charbox(index)))
    finally:
        doc.close()
    flat = "".join(c for c, _ in chars)
    out, start = [], 0
    for text in (t.strip() for t in pages[0].split("\n")):
        key = squash(text)
        if not key:
            continue
        at = flat.find(key, start)
        if at < 0:
            at = flat.find(key)  # out of reading order: search from the top
        if at < 0:
            print(f"  {path.name}: no position found for {text!r}")
            continue
        start = at + len(key)
        boxes = [box for _, box in chars[at:at + len(key)]]
        left, bottom = min(b[0] for b in boxes), min(b[1] for b in boxes)
        right, top = max(b[2] for b in boxes), max(b[3] for b in boxes)
        # The line's cells: runs of characters with no gap wider than a word space between them
        # (a table row has several). The page cuts a long line into pieces at these gaps.
        cells, gap = [[boxes[0][0], boxes[0][2]]], (top - bottom) * 0.9
        for box in boxes[1:]:
            if box[0] - cells[-1][1] > gap:
                cells.append([box[0], box[2]])
            else:
                cells[-1][1] = max(cells[-1][1], box[2])
        out.append({"text": text, "box": [round(left / page_width, 4), round(1 - top / page_height, 4),
                                          round(right / page_width, 4), round(1 - bottom / page_height, 4)],
                    "cells": [[round(a / page_width, 4), round(b / page_width, 4)] for a, b in cells]})
    return out


def render(path: Path) -> dict:
    """The page as a PNG, cropped to its content, and the box of each text line on it."""
    doc = pdfium.PdfDocument(str(path))
    try:
        page = doc[0]
        page_width, page_height = page.get_size()
        image = page.render(scale=WIDTH / page_width).to_pil().convert("RGB")
    finally:
        doc.close()
    lines = line_boxes(path, page_width, page_height)
    # The footer is a fine-print note (the page says itself that the documents are fictional); it is
    # left out, and the page is cropped to what is printed on it, so the text shows larger on screen.
    footer = [line for line in lines if line["text"].startswith(FOOTER)]
    lines = [line for line in lines if not line["text"].startswith(FOOTER)]
    draw = ImageDraw.Draw(image)
    for line in footer:
        x0, y0, x1, y1 = line["box"]
        draw.rectangle((x0 * image.width - 4, y0 * image.height - 4, x1 * image.width + 4, y1 * image.height + 4),
                       fill="white")
    inked = image.convert("L").point(lambda v: 255 if v < 215 else 0).getbbox() or (0, 0, image.width, image.height)
    margin = int(image.width * 0.035)
    left, top = max(0, inked[0] - margin), max(0, inked[1] - margin)
    right, bottom = min(image.width, inked[2] + margin), min(image.height, inked[3] + margin)
    old_width, old_height = image.width, image.height
    image = image.crop((left, top, right, bottom))
    for line in lines:
        x0, y0, x1, y1 = line["box"]
        line["box"] = [round((x0 * old_width - left) / image.width, 4), round((y0 * old_height - top) / image.height, 4),
                       round((x1 * old_width - left) / image.width, 4), round((y1 * old_height - top) / image.height, 4)]
        line["cells"] = [[round((a * old_width - left) / image.width, 4), round((b * old_width - left) / image.width, 4)]
                         for a, b in line["cells"]]
    png = OUT / (path.stem + ".png")
    # A small palette keeps each file small; text and a scan both survive it.
    image.quantize(colors=48, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(png, optimize=True)
    return {"png": f"samples/{png.name}", "width": image.width, "height": image.height, "lines": lines}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    previews = {}
    for path in sorted(PDFS.glob("*.pdf")):
        previews[path.name] = render(path)
        size = (OUT / (path.stem + ".png")).stat().st_size // 1024
        print(f"wrote public/samples/{path.stem}.png ({size} KB, {len(previews[path.name]['lines'])} lines)")
    # one file per line, so a change to one preview shows as one changed line
    text = "{\n" + ",\n".join(f"{json.dumps(name)}: {json.dumps(item, separators=(',', ':'))}"
                              for name, item in previews.items()) + "\n}\n"
    (OUT / "previews.json").write_text(text, encoding="utf-8", newline="\n")
    print("wrote public/samples/previews.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
