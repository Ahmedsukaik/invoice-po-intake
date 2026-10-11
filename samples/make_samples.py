"""Generates the demo PDFs in samples/pdfs/.  Run:  python samples/make_samples.py

Every company here is fictional ("Demo" in each name), every IBAN uses the made-up bank
code DEMO, every address uses the non-existent postcode area ZZ, and every e-mail domain
ends in .example (reserved for examples). The generated files are committed, so the demo
runs without this script; the tests also use it to build variants.
"""
from __future__ import annotations

import io
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fpdf import FPDF

OUT = Path(__file__).resolve().parent / "pdfs"
FIXED_DATE = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)  # same bytes on every run
BUYER = "Harbor Demo Manufacturing Ltd"
BUYER_ADDRESS = "1 Harbour Road, Sampletown ZZ2 2BB, United Kingdom"
FOOTER = "Demo document generated for a reference build. Fictional company; not a real invoice or order."
INK, GREY, ACCENT = (17, 24, 39), (100, 110, 125), (15, 118, 110)

VENDORS = {
    "acme": {"name": "Acme Demo Components Ltd", "address": "Unit 4, Demo Business Park, Sampletown ZZ1 1AA",
             "email": "accounts@acme-components.example", "iban": "GB77 DEMO 4000 0110 0000 01"},
    "brightline": {"name": "Brightline Demo Packaging Ltd", "address": "22 Carton Lane, Boxford ZZ3 4CD",
                   "email": "billing@brightline-packaging.example", "iban": "GB79 DEMO 4000 0220 0000 02"},
    "cobalt": {"name": "Cobalt Demo Logistics Ltd", "address": "Bay 9, Freight Way, Portley ZZ5 6EF",
               "email": "finance@cobalt-logistics.example", "iban": "GB81 DEMO 4000 0330 0000 03"},
    "zenith": {"name": "Zenith Demo Trading LLC", "address": "300 Example Avenue, Suite 12, Faketown ZZ9 9ZZ",
               "email": "invoices@zenith-trading.example", "iban": "GB37 DEMO 4000 7777 7777 77"},
}

Line = tuple[str, str, str, str, str]  # sku, description, qty, unit price, amount (as printed)


def _pdf() -> FPDF:
    pdf = FPDF(format="A4")
    pdf.set_creation_date(FIXED_DATE)
    pdf.set_margins(18, 16, 18)
    pdf.set_auto_page_break(True, 18)
    pdf.add_page()
    return pdf


def _header(pdf: FPDF, issuer: str, title: str, address: str, email: str | None) -> None:
    pdf.set_text_color(*INK)
    pdf.set_font("Helvetica", "B", 17)
    pdf.cell(110, 10, issuer)
    pdf.set_text_color(*ACCENT)
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 10, title, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*GREY)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, address, new_x="LMARGIN", new_y="NEXT")
    if email:
        pdf.cell(0, 5, email, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.6)
    pdf.line(18, pdf.get_y(), 192, pdf.get_y())
    pdf.ln(5)


def _info(pdf: FPDF, rows: list[tuple[str, str]]) -> None:
    pdf.set_fill_color(243, 246, 249)
    top = pdf.get_y()
    pdf.rect(18, top, 90, 6.5 * len(rows) + 4, style="F")
    pdf.set_y(top + 2)
    for label, value in rows:
        pdf.set_x(22)
        pdf.set_text_color(*GREY)
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(32, 6.5, f"{label}:")
        pdf.set_text_color(*INK)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(50, 6.5, value, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)


def _party(pdf: FPDF, label: str, name: str, address: str) -> None:
    pdf.set_text_color(*INK)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"{label}: {name}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*GREY)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, address, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)


def _table(pdf: FPDF, lines: list[Line], extra_rows: list[str] | None = None) -> None:
    widths = [24, 80, 18, 26, 26]
    pdf.set_text_color(*INK)
    pdf.set_draw_color(*INK)
    pdf.set_line_width(0.3)
    pdf.set_font("Helvetica", "B", 10)
    for width, head in zip(widths, ["Item", "Description", "Qty", "Unit price", "Amount"]):
        pdf.cell(width, 8, head, border="B", align="L" if head in ("Item", "Description") else "R")
    pdf.ln()
    pdf.set_font("Helvetica", "", 10)
    pdf.set_draw_color(220, 225, 230)
    for row in lines:
        for i, (width, value) in enumerate(zip(widths, row)):
            pdf.cell(width, 8, value, border="B", align="L" if i < 2 else "R")
        pdf.ln()
    for text in extra_rows or []:
        pdf.cell(sum(widths), 8, text, border="B", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)


def _totals(pdf: FPDF, rows: list[tuple[str, str]], bold_last: bool = True) -> None:
    for i, (label, value) in enumerate(rows):
        last = bold_last and i == len(rows) - 1
        pdf.set_x(118)
        pdf.set_font("Helvetica", "B" if last else "", 11 if last else 10)
        pdf.cell(30, 7, label)
        pdf.cell(26, 7, value, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)


def _notes(pdf: FPDF, notes: list[str]) -> None:
    pdf.set_text_color(*INK)
    pdf.set_font("Helvetica", "B", 10)
    for note in notes:
        pdf.multi_cell(0, 6, note, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)


def _footer(pdf: FPDF) -> None:
    pdf.set_y(-26)
    pdf.set_text_color(*GREY)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, FOOTER, align="C")


def _bytes(pdf: FPDF) -> bytes:
    return bytes(pdf.output())


def invoice_pdf(vendor: str | dict[str, str], number: str, date: str, due: str | None, po: str | None,
                lines: list[Line], subtotal: str, tax_rate: str, tax: str, total: str, currency: str = "GBP",
                iban: str | None = "", notes: list[str] | None = None, banner: str | None = None,
                extra_table_rows: list[str] | None = None, extra_info: list[tuple[str, str]] | None = None,
                sort_code: str | None = None, account: str | None = None) -> bytes:
    v = VENDORS[vendor] if isinstance(vendor, str) else vendor
    pdf = _pdf()
    _header(pdf, v["name"], "INVOICE", v["address"], v.get("email"))
    if banner:
        pdf.set_text_color(185, 28, 28)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, banner, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
    info = [("Invoice number", number), ("Invoice date", date)]
    if due:
        info.append(("Due date", due))
    if po:
        info.append(("PO number", po))
    info.append(("Currency", currency))
    _info(pdf, info + list(extra_info or []))
    _party(pdf, "Bill to", BUYER, BUYER_ADDRESS)
    _table(pdf, lines, extra_table_rows)
    _totals(pdf, [("Subtotal", subtotal), (f"VAT {tax_rate}%", tax), ("Total due", total)])
    if notes:
        _notes(pdf, notes)
    pdf.set_text_color(*INK)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "Payment details", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, "Bank: Demo Bank plc", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Account name: {v['name']}", new_x="LMARGIN", new_y="NEXT")
    if sort_code:
        pdf.cell(0, 6, f"Sort code: {sort_code}", new_x="LMARGIN", new_y="NEXT")
    if account:
        pdf.cell(0, 6, f"Account number: {account}", new_x="LMARGIN", new_y="NEXT")
    if iban is not None:
        pdf.cell(0, 6, f"IBAN: {iban or v['iban']}", new_x="LMARGIN", new_y="NEXT")
    _footer(pdf)
    return _bytes(pdf)


def purchase_order_pdf(number: str, date: str, supplier: str, lines: list[Line], subtotal: str, tax_rate: str,
                       tax: str, total: str, currency: str = "GBP") -> bytes:
    pdf = _pdf()
    _header(pdf, BUYER, "PURCHASE ORDER", BUYER_ADDRESS, "purchasing@harbor-manufacturing.example")
    _info(pdf, [("PO number", number), ("Order date", date), ("Supplier", VENDORS[supplier]["name"]),
                ("Currency", currency)])
    _party(pdf, "Deliver to", BUYER, "Goods-in, Gate 2, " + BUYER_ADDRESS)
    _table(pdf, lines)
    _totals(pdf, [("Subtotal", subtotal), (f"VAT {tax_rate}%", tax), ("Total", total)])
    _notes(pdf, ["Please quote the PO number on every invoice."])
    _footer(pdf)
    return _bytes(pdf)


def scanned_pdf(rows: list[list[tuple[int, str]]]) -> bytes:
    """An image-only PDF: the page is a picture of text, with no text layer (like a scan).
    Each row is a list of (x position in pixels, text); the first row is the title."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    width, height = 1240, 1754  # A4 at 150 dpi
    img = Image.new("L", (width, height), 246)
    draw = ImageDraw.Draw(img)
    big, small, tiny = (ImageFont.load_default(size=s) for s in (46, 27, 20))
    y = 130
    for i, row in enumerate(rows):
        font = big if i == 0 else tiny if row and row[0][1].startswith("(") else small
        for x, text in row:
            draw.text((x, y), text, fill=35, font=font)
        y += 80 if i == 0 else 46 if row else 26
    img = img.rotate(0.7, fillcolor=246, resample=Image.BICUBIC).filter(ImageFilter.GaussianBlur(0.6))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=70)
    pdf = FPDF(format="A4")
    pdf.set_creation_date(FIXED_DATE)
    pdf.add_page()
    pdf.image(io.BytesIO(buf.getvalue()), x=0, y=0, w=210, h=297)
    return _bytes(pdf)


ACME_0101 = [("AC-100", "Steel bracket, 40 mm", "200", "4.50", "900.00"),
             ("AC-220", "Hinge set, zinc", "50", "12.00", "600.00")]

SAMPLES: dict[str, Any] = {
    "01_clean_invoice.pdf": lambda: invoice_pdf(
        "acme", "INV-A-1001", "2026-10-05", "2026-11-04", "PO-2026-0101", ACME_0101,
        "1,500.00", "20", "300.00", "1,800.00"),
    "02_totals_do_not_add_up.pdf": lambda: invoice_pdf(
        "brightline", "INV-B-2001", "2026-10-06", "2026-11-05", "PO-2026-0102",
        [("BP-310", "Carton, double wall, 400 x 300 mm", "500", "0.85", "425.00"),
         ("BP-420", "Stretch film roll, 500 mm", "120", "3.25", "395.00")],
        "820.00", "20", "164.00", "974.00"),
    "03_unknown_vendor.pdf": lambda: invoice_pdf(
        "zenith", "ZT-0042", "2026-10-07", "2026-10-21", None,
        [("ZT-900", "Consulting day, supplier onboarding", "2", "650.00", "1,300.00")],
        "1,300.00", "20", "260.00", "1,560.00"),
    "04_changed_bank_details.pdf": lambda: invoice_pdf(
        "cobalt", "INV-C-3005", "2026-10-08", "2026-11-07", "PO-2026-0103",
        [("CL-01", "Pallet delivery, zone 2", "6", "85.00", "510.00")],
        "510.00", "20", "102.00", "612.00", iban="GB40 DEMO 4000 9999 9999 99",
        notes=["IMPORTANT: we have changed banks. Please pay this and all future invoices to the new account below."]),
    "05_duplicate_invoice.pdf": lambda: invoice_pdf(
        "acme", "INV-A-1001", "2026-10-05", "2026-11-04", "PO-2026-0101", ACME_0101,
        "1,500.00", "20", "300.00", "1,800.00", banner="COPY - PAYMENT REMINDER"),
    "06_exceeds_po.pdf": lambda: invoice_pdf(
        "acme", "INV-A-1002", "2026-10-09", "2026-11-08", "PO-2026-0104",
        [("AC-310", "Mounting plate, steel", "140", "8.00", "1,120.00")],
        "1,120.00", "20", "224.00", "1,344.00"),
    "07_purchase_order.pdf": lambda: purchase_order_pdf(
        "PO-2026-0105", "2026-10-02", "brightline",
        [("BP-310", "Carton, double wall, 400 x 300 mm", "300", "0.85", "255.00"),
         ("BP-505", "Edge protector, 1 m", "200", "0.40", "80.00")],
        "335.00", "20", "67.00", "402.00"),
    "09_price_above_po.pdf": lambda: invoice_pdf(
        "brightline", "INV-B-2007", "2026-10-09", "2026-11-08", "PO-2026-0102",
        [("BP-310", "Carton, double wall, 400 x 300 mm", "500", "0.89", "445.00"),
         ("BP-420", "Stretch film roll, 500 mm", "120", "3.25", "390.00")],
        "835.00", "20", "167.00", "1,002.00"),
    "08_scanned_invoice.pdf": lambda: scanned_pdf([
        [(110, "Acme Demo Components Ltd"), (900, "INVOICE")],
        [(110, "Unit 4, Demo Business Park, Sampletown ZZ1 1AA")],
        [],
        [(110, "Invoice number:"), (380, "INV-A-1003")],
        [(110, "Invoice date:"), (380, "2026-10-09")],
        [(110, "Due date:"), (380, "2026-11-08")],
        [(110, "PO number:"), (380, "PO-2026-0106")],
        [(110, "Currency:"), (380, "GBP")],
        [],
        [(110, "Bill to: Harbor Demo Manufacturing Ltd")],
        [],
        [(110, "Item"), (270, "Description"), (720, "Qty"), (850, "Unit price"), (1030, "Amount")],
        [(110, "AC-100"), (270, "Steel bracket, 40 mm"), (720, "100"), (870, "4.50"), (1030, "450.00")],
        [],
        [(720, "Subtotal"), (1030, "450.00")],
        [(720, "VAT 20%"), (1045, "90.00")],
        [(720, "Total due"), (1030, "540.00")],
        [],
        [(110, "IBAN: GB77 DEMO 4000 0110 0000 01")],
        [],
        [(110, "(scanned copy) " + FOOTER)],
    ]),
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in SAMPLES.items():
        (OUT / name).write_bytes(build())
        print(f"wrote samples/pdfs/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
