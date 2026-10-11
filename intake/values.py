"""Field definitions and value parsing, shared by the readers, the corrections and the checks.

Every value is stored as a plain, normalised string (amounts as "1500.00", dates as
"2026-10-05"), or None when it is empty. Anything that does not parse is refused here,
so the checks never see a half-valid value.

normalize() is strict: it is used for what people type and for the demo layout.
parse_printed_amount() and parse_printed_date() read values the way suppliers print them
("GBP 1.500,00", "05/10/2026"); the Claude reader uses them, so the model copies text and
code does the interpreting.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

DOC_TYPES = ("invoice", "purchase_order")
CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("9999999999.99")


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    kind: str  # doc_type text ref date currency iban sortcode account amount price quantity percent


HEADER_FIELDS: dict[str, FieldSpec] = {
    spec.name: spec
    for spec in (
        FieldSpec("doc_type", "Document type", "doc_type"),
        FieldSpec("vendor_name", "Vendor", "text"),
        FieldSpec("invoice_number", "Invoice number", "ref"),
        FieldSpec("invoice_date", "Invoice date", "date"),
        FieldSpec("due_date", "Due date", "date"),
        FieldSpec("po_number", "PO number", "ref"),
        FieldSpec("order_date", "Order date", "date"),
        FieldSpec("currency", "Currency", "currency"),
        FieldSpec("vendor_iban", "Bank account (IBAN)", "iban"),
        FieldSpec("vendor_sort_code", "Sort code", "sortcode"),
        FieldSpec("vendor_account", "Account number", "account"),
        FieldSpec("subtotal", "Subtotal", "amount"),
        FieldSpec("tax_rate", "Tax rate %", "percent"),
        FieldSpec("tax_amount", "Tax", "amount"),
        FieldSpec("total", "Total", "amount"),
    )
}
BANK_FIELDS = ("vendor_iban", "vendor_sort_code", "vendor_account")

FIELDS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "invoice": ("vendor_name", "invoice_number", "invoice_date", "due_date", "po_number", "currency",
                "vendor_iban", "vendor_sort_code", "vendor_account", "subtotal", "tax_rate", "tax_amount", "total"),
    "purchase_order": ("vendor_name", "po_number", "order_date", "currency", "subtotal", "tax_rate",
                       "tax_amount", "total"),
}

REQUIRED_BY_TYPE: dict[str, tuple[str, ...]] = {
    "invoice": ("vendor_name", "invoice_number", "invoice_date", "currency", "subtotal", "tax_amount", "total"),
    "purchase_order": ("vendor_name", "po_number", "order_date", "currency", "subtotal", "tax_amount", "total"),
}

LINE_FIELDS: dict[str, FieldSpec] = {
    spec.name: spec
    for spec in (
        FieldSpec("sku", "Item code", "ref"),
        FieldSpec("description", "Description", "text"),
        FieldSpec("quantity", "Qty", "quantity"),
        FieldSpec("unit_price", "Unit price", "price"),
        FieldSpec("line_total", "Amount", "amount"),
    )
}
MAX_LINES = 200

_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9/_.#-]{0,39}", re.ASCII)
_GROUPED = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", re.ASCII)
_PLAIN = re.compile(r"\d+(?:\.\d+)?", re.ASCII)
_IBAN = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", re.ASCII)
_CONFUSABLE = str.maketrans({"O": "0", "I": "1", "L": "1"})


def clean_text(value: object, limit: int = 200) -> str:
    """One line of printable text: control and format characters removed, spaces collapsed."""
    text = "" if value is None else str(value)
    text = "".join(" " if unicodedata.category(ch)[0] in "CZ" else ch for ch in text)
    return " ".join(text.split())[:limit]


def name_key(name: str | None) -> str:
    """Comparable form of a company name: case, punctuation and spacing ignored."""
    text = unicodedata.normalize("NFKC", name or "").casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def number_key(number: str | None) -> str:
    """Loose comparable form of a document number, used to find possible duplicates. The number
    is split at spaces and punctuation; in each part the letters O, I and L count as the digits
    0, 1 and 1, and each run of digits loses its leading zeros; then the parts are joined.
    'INV-2026-0001', 'inv 2026/001' and 'INV-2026-OOO1' give the same key, and so do 'INV-11'
    and 'INV-L1'. 'INV-10-01' and 'INV-1001' do not ('10' and '01' are separate parts)."""
    parts = re.split(r"[^A-Z0-9]+", (number or "").upper().translate(_CONFUSABLE))
    return "".join(re.sub(r"\d+", lambda m: m.group(0).lstrip("0") or "0", part) for part in parts)


def exact_number_key(number: str | None) -> str:
    """Strict comparable form: case ignored and any punctuation counts as one separator, nothing
    else. 'INV-A-1001' and 'inv/a/1001' are printed the same way; 'INV-11' and 'INV-L1',
    'INV-0042' and 'INV-42', or 'INV-10-01' and 'INV-1001' are not."""
    return "-".join(part for part in re.split(r"[^A-Z0-9]+", (number or "").upper()) if part)


def to_decimal(text: str, max_places: int, what: str) -> Decimal:
    raw = (text or "").strip().replace(" ", "")
    if raw.startswith("-"):
        raise ValueError(f"{what} must be zero or more (credit notes are not handled here).")
    if not (_PLAIN.fullmatch(raw) or _GROUPED.fullmatch(raw)):
        raise ValueError(f"{what} must be a number such as 1250.00 (no currency signs or letters).")
    value = Decimal(raw.replace(",", ""))
    exponent = int(value.as_tuple().exponent)
    if max(0, -exponent) > max_places:
        raise ValueError(f"{what} may have at most {max_places} decimal places.")
    if value > MAX_AMOUNT:
        raise ValueError(f"{what} is too large.")
    return value


def money(value: Decimal) -> str:
    """Decimal -> '1,500.00' for people."""
    return f"{value.quantize(CENT, ROUND_HALF_UP):,.2f}"


def money_text(value: object) -> str:
    """Stored amount string -> '1,500.00', or '' (never via float)."""
    amount = dec(value)
    return money(amount) if amount is not None else ""


def iban_is_valid(iban: str) -> bool:
    """ISO 13616 check digits (mod 97)."""
    if not _IBAN.fullmatch(iban or ""):
        return False
    moved = iban[4:] + iban[:4]
    digits = "".join(str(int(ch, 36)) for ch in moved)
    return int(digits) % 97 == 1


def iban_display(iban: str | None) -> str:
    if not iban:
        return ""
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


def uk_details_from_iban(iban: str | None) -> tuple[str, str] | None:
    """A UK IBAN holds the sort code and account number: GBkk BANK SSSSSS AAAAAAAA."""
    if iban and len(iban) == 22 and iban.startswith("GB") and iban[8:].isdigit():
        return f"{iban[8:10]}-{iban[10:12]}-{iban[12:14]}", iban[14:22]
    return None


def normalize(kind: str, raw: object, what: str = "value") -> str | None:
    """Parse one value. Returns the stored form, None for empty, or raises ValueError(message)."""
    if raw is None:
        return None
    if not isinstance(raw, (str, int, Decimal)) or isinstance(raw, bool):
        raise ValueError(f"{what} must be text.")
    text = clean_text(raw, 300)
    if not text:
        return None
    if kind == "doc_type":
        value = text.lower().replace(" ", "_")
        if value not in DOC_TYPES:
            raise ValueError(f"{what} must be 'invoice' or 'purchase_order'.")
        return value
    if kind == "text":
        if len(text) > 200:
            raise ValueError(f"{what} must be 200 characters or fewer.")
        return text
    if kind == "ref":
        if not _REF.fullmatch(text):
            raise ValueError(f"{what} may only contain letters, digits and / _ . # - (max 40, no spaces).")
        return text
    if kind == "date":
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text, re.ASCII):
                raise ValueError
            parsed = date.fromisoformat(text)
        except ValueError:
            raise ValueError(f"{what} must be a real date written as YYYY-MM-DD.") from None
        if not 2000 <= parsed.year <= 2099:
            raise ValueError(f"{what} must be between the years 2000 and 2099.")
        return parsed.isoformat()
    if kind == "currency":
        value = text.upper()
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError(f"{what} must be a three-letter code such as GBP.")
        return value
    if kind == "iban":
        value = re.sub(r"\s+", "", text).upper()
        if not _IBAN.fullmatch(value):
            raise ValueError(f"{what} does not look like an IBAN.")
        return value
    if kind == "sortcode":
        digits = re.sub(r"[\s-]", "", text)
        if not re.fullmatch(r"\d{6}", digits, re.ASCII):
            raise ValueError(f"{what} must be six digits, such as 40-00-01.")
        return f"{digits[0:2]}-{digits[2:4]}-{digits[4:6]}"
    if kind == "account":
        digits = re.sub(r"[\s-]", "", text)
        if not re.fullmatch(r"\d{6,10}", digits, re.ASCII):
            raise ValueError(f"{what} must be 6 to 10 digits.")
        return digits
    if kind == "amount":
        return str(to_decimal(text, 2, what).quantize(CENT))
    if kind == "price":
        value = to_decimal(text, 4, what)
        return str(value.quantize(CENT) if value == value.quantize(CENT) else value)
    if kind == "quantity":
        value = to_decimal(text, 3, what)
        if value == 0:
            raise ValueError(f"{what} must be above zero.")
        return format(value.normalize(), "f")
    if kind == "percent":
        value = to_decimal(text.rstrip("%").strip(), 2, what)
        if value > 100:
            raise ValueError(f"{what} must be 100 or less.")
        return format(value.normalize(), "f")
    raise ValueError(f"unknown field kind {kind!r}")


def normalize_line(raw: object) -> dict[str, str | None]:
    """One line item, every cell parsed. Raises ValueError naming the bad cell."""
    if not isinstance(raw, dict):
        raise ValueError("each line must be an object")
    unknown = set(raw) - set(LINE_FIELDS)
    if unknown:
        raise ValueError(f"unknown line field(s): {', '.join(sorted(map(str, unknown)))[:100]}")
    return {name: normalize(spec.kind, raw.get(name), spec.label) for name, spec in LINE_FIELDS.items()}


# --------------------------------------------------------------------------- printed formats
_CURRENCY_MARKS = re.compile(r"\b(?:GBP|EUR|USD|CHF)\b|[£€$]", re.IGNORECASE)


def parse_printed_amount(text: object, kind: str, what: str) -> str | None:
    """'£1,500.00', '1.500,00 EUR', '12,50' -> the stored form for this kind.

    Read UK style where a number could go either way: a comma followed by exactly three digits
    after a group of one to three digits is a thousands separator ('1,500' is fifteen hundred).
    A dot followed by exactly three digits after a group of one to three digits ('1.500') could
    be 1.5 or 1500 and is refused. A leading '0' cannot start a thousands group, so '0,125' and
    '0.125' are decimals. Negative amounts are refused (credit notes are not handled here)."""
    raw = _CURRENCY_MARKS.sub("", clean_text(text, 100)).replace(" ", "").replace("'", "")
    if not raw:
        return None
    if raw.startswith(("-", "(")) or raw.endswith("-"):
        raise ValueError(f"{what} is negative (credit notes are not handled here).")
    unclear = ValueError(f"{what} '{clean_text(text, 30)}' is not a clear number.")
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
    elif "," in raw or "." in raw:
        sep = "," if "," in raw else "."
        head, _, tail = raw.rpartition(sep)
        grouped = re.fullmatch(rf"[1-9]\d{{0,2}}(?:\{sep}\d{{3}})+", raw, re.ASCII)
        if raw.count(sep) == 1 and (len(tail) != 3 or head == "0" or len(head) > 3):
            raw = f"{head}.{tail}"  # a decimal mark: 12,50  0,125  1234,567  12.5
        elif grouped and (sep == "," or raw.count(sep) > 1):
            raw = raw.replace(sep, "")  # thousands: 1,500  1,500,000  1.500.000
        elif grouped:
            raise ValueError(f"{what} '{clean_text(text, 30)}' could be read two ways (a decimal point or "
                             "thousands).")
        else:
            raise unclear
    return normalize(kind, raw, what)


_NUMBER_IN_TEXT = re.compile(r"\d(?:[\d.,']*\d)?", re.ASCII)


def amount_in_text(value: object, text: object, kind: str) -> bool:
    """Is the stored number `value` one of the numbers printed in `text`? Each number in the
    text is parsed like a printed value and compared as a number, so 'Total due 1,800.00'
    matches 1800.00, and 'Total 18' or '180.00' does not."""
    target = dec(value)
    if target is None:
        return False
    for match in _NUMBER_IN_TEXT.finditer(clean_text(text, 400)):
        try:
            parsed = dec(parse_printed_amount(match.group(0), kind, "number"))
        except ValueError:
            continue
        if parsed is not None and parsed == target:
            return True
    return False


_MONTHS = {name: number for number, names in enumerate(
    [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
     ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
     ("dec", "december")], 1) for name in names}


def parse_printed_date(text: object, order: str = "dmy") -> tuple[str | None, bool]:
    """A printed date -> (YYYY-MM-DD, ambiguous). 05/10/2026 could be 5 October or 10 May:
    it is read in the configured order (dmy or mdy) and reported as ambiguous. Raises
    ValueError when there is no real date."""
    t = clean_text(text, 60).lower().replace(",", " ").rstrip(".")
    if not t:
        return None, False
    ambiguous = False
    if m := re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t):
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    elif m := re.fullmatch(r"(\d{1,2})([./-])(\d{1,2})\2(\d{2}|\d{4})", t):
        a, b, y = int(m[1]), int(m[3]), int(m[4])
        y = y + 2000 if y < 100 else y
        if m[2] == ".":  # dotted dates are day first everywhere they are used
            d, mo = a, b
        elif a > 12 >= b:
            d, mo = a, b
        elif b > 12 >= a:
            d, mo = b, a
        else:
            ambiguous = a != b
            d, mo = (a, b) if order == "dmy" else (b, a)
    elif m := re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?[\s-]+([a-z]+)[\s-]+(\d{4})", t):
        d, mo, y = int(m[1]), _MONTHS.get(m[2], 0), int(m[3])
    elif m := re.fullmatch(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s+(\d{4})", t):
        mo, d, y = _MONTHS.get(m[1], 0), int(m[2]), int(m[3])
    else:
        raise ValueError(f"'{clean_text(text, 30)}' is not a date this reader understands.")
    try:
        parsed = date(y, mo, d)
    except ValueError:
        raise ValueError(f"'{clean_text(text, 30)}' is not a real date.") from None
    return normalize("date", parsed.isoformat()), ambiguous


# --------------------------------------------------------------------------- helpers
def dec(value: object) -> Decimal | None:
    """Stored string -> Decimal, or None. Never raises."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def q2_or_none(value: Decimal | None) -> Decimal | None:
    return value.quantize(CENT, ROUND_HALF_UP) if value is not None else None


def parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
