"""The checks. Each one produces findings a person can read, with a severity:

error     must be fixed (by correcting a field) before the document can be approved;
          some cannot be fixed here at all (unknown vendor, changed bank details, a copy of
          an invoice that is already approved) and the document must be rejected
blocking  stops approval unless the reviewer acknowledges that exact finding, with a note
warning   shown to the reviewer; does not stop approval
info      context, for example how much of the PO remains

Every finding has a fingerprint (its code and message). An acknowledgement names the
fingerprint, so if the finding changes (another copy of the invoice was approved, the PO
was used up) the old acknowledgement no longer counts and the reviewer looks again.

The checks only read. They run when a document arrives, after every correction, when a
document page is opened, when a related document is decided, and again at approval.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from .config import Policy
from .values import (
    BANK_FIELDS,
    CENT,
    FIELDS_BY_TYPE,
    HEADER_FIELDS,
    REQUIRED_BY_TYPE,
    dec,
    exact_number_key,
    iban_is_valid,
    money,
    name_key,
    parse_date,
    uk_details_from_iban,
)

SEVERITIES = ("error", "blocking", "warning", "info")
COMMITTED = ("approved", "exporting", "exported")

# Plain words for each finding, for the tick boxes and the user guide.
CODE_LABELS = {
    "missing_field": "A required value is empty", "no_lines": "No line items", "line_incomplete": "A line is incomplete",
    "doc_type_unknown": "Document type not set", "vendor_unknown": "Not a supplier in the vendor master",
    "bank_changed": "Bank details differ from the vendor master", "bank_invalid": "The IBAN is not valid",
    "bank_missing": "No bank details could be read", "bank_unverified": "Bank details cannot be compared",
    "bank_typed": "Bank details were typed in by a person", "value_corrected": "A value read from the PDF was changed",
    "currency_not_allowed": "Currency not accepted", "currency_unusual": "Unusual currency for this supplier",
    "date_in_future": "Dated in the future", "due_before_invoice": "Due before it was issued",
    "due_far_out": "Very long payment term", "due_missing": "No due date", "date_ambiguous": "A date could be read two ways",
    "line_math": "A line does not add up", "subtotal_mismatch": "The lines do not add up to the subtotal",
    "tax_mismatch": "The tax does not match the rate", "tax_rate_unusual": "Unusual tax rate",
    "total_mismatch": "The total does not add up",
    "duplicate_invoice": "Same invoice already in the system", "possible_duplicate": "Possibly the same invoice",
    "duplicate_po": "Same purchase order already in the system",
    "po_missing": "No purchase order number", "po_not_found": "PO not in the PO register", "po_closed": "PO is closed",
    "po_vendor_mismatch": "PO is for another supplier", "po_currency_mismatch": "PO is in another currency",
    "po_line_not_found": "A line is not on the PO", "po_price_mismatch": "Price differs from the PO",
    "po_qty_exceeded": "Quantity above what the PO has left", "po_amount_exceeded": "Amount above what the PO has left",
    "po_not_in_register": "PO not in the PO register", "po_differs": "PO differs from the PO register",
    "po_matched": "Matched to the PO", "po_in_register": "Vendor, net amount and line count match the PO register",
    "manual_entry": "Needs typing in",
    "evidence_not_found": "Source of a value not found in the PDF", "evidence_missing": "No source given for a value",
    "model_concern": "The AI reader noted something", "reader_conflict": "A value is printed twice, differently",
    "reader_unreadable_line": "A table line could not be read", "reader_invalid_value": "A value could not be read",
}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    field: str | None = None
    related: str | None = None  # another document this finding is about

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(f"{self.code}|{self.message}".encode()).hexdigest()[:10]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.fingerprint, "code": self.code, "severity": self.severity, "message": self.message,
                "field": self.field, "related": self.related, "label": CODE_LABELS.get(self.code, self.code)}


class Lookups(Protocol):
    """What the checks need from storage (the Store implements it)."""

    def vendors(self) -> list[dict]: ...
    def po(self, number: str | None) -> dict | None: ...
    def same_number(self, doc_type: str, number: str | None, exclude_id: str) -> list[dict]: ...
    def invoices_near_total(self, total: str | None, tolerance: Decimal, exclude_id: str) -> list[dict]: ...
    def invoices_for_po(self, po_number: str, exclude_id: str) -> list[dict]: ...


def match_vendor(name: str | None, vendors: list[dict]) -> dict | None:
    """Exact match on the vendor's name or one of its aliases, ignoring case and punctuation.
    No fuzzy matching: 'Acme Demo Componets' is a different (unknown) vendor."""
    key = name_key(name)
    if not key:
        return None
    for vendor in vendors:
        if vendor.get("active", True) and key in {name_key(vendor["name"]), *(name_key(a) for a in vendor["aliases"])}:
            return vendor
    return None


def vendor_identity(name: str | None, vendors: list[dict]) -> str:
    """The vendor id when the name is in the vendor master (aliases count), else the printed name."""
    vendor = match_vendor(name, vendors)
    return vendor["id"] if vendor else f"name:{name_key(name)}"


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, ROUND_HALF_UP)


@dataclass
class _Out:
    findings: list[Finding] = field(default_factory=list)

    def add(self, code: str, severity: str, message: str, field_name: str | None = None,
            related: str | None = None) -> None:
        self.findings.append(Finding(code, severity, message, field_name, related))


def _note_still_applies(note: dict, doc: dict, corrected: set[str]) -> bool:
    """A reader note stops applying once a person corrected what it is about. A note about one
    line stays until that line changes; other line notes go once the lines are corrected."""
    name = note.get("field")
    if name != "lines":
        return name not in corrected
    if "lines" not in corrected:
        return True
    line = note.get("line")
    if not line:
        return False
    original, current = doc.get("original_lines") or [], doc.get("lines") or []
    idx = line - 1
    return idx < len(original) and idx < len(current) and original[idx] == current[idx]


def run_checks(doc: dict, lookups: Lookups, policy: Policy, today: date) -> list[Finding]:
    out = _Out()
    add = out.add
    fields: dict[str, Any] = doc["fields"]
    lines: list[dict] = doc["lines"]
    corrected = set(doc.get("corrected") or [])
    original = doc.get("original_fields") or {}

    for note in doc.get("reader_notes") or []:
        if _note_still_applies(note, doc, corrected):
            add(note["code"], note.get("severity", "warning"), note["message"], note.get("field"))
    if doc.get("manual_reason") and not corrected:
        add("manual_entry", "info", f"{doc['manual_reason']} Type the fields in from the PDF.")

    # A value read from the PDF and then changed by a person: the approver confirms it explicitly,
    # so a correction cannot quietly make another finding go away.
    for name in sorted(corrected - {"lines"} - set(BANK_FIELDS)):
        old, new = original.get(name), fields.get(name)
        if old is not None and old != new and name in HEADER_FIELDS:
            add("value_corrected", "blocking",
                f"{HEADER_FIELDS[name].label} was read from the PDF as '{old}' and changed to '{new}'. "
                "Approve only if the PDF really says that.", name)
    if "lines" in corrected and doc.get("original_lines") and lines != doc["original_lines"]:
        add("value_corrected", "blocking",
            "The line items were changed from what was read from the PDF. Approve only if the PDF really shows them "
            "as they are now.", "lines")

    doc_type = fields.get("doc_type")
    if doc_type not in FIELDS_BY_TYPE:
        if not any(f.field == "doc_type" and f.severity == "error" for f in out.findings):
            add("doc_type_unknown", "error", "Set the document type (invoice or purchase order) first.", "doc_type")
        return sort(out.findings)

    # ---- required fields -------------------------------------------------------------
    for name in REQUIRED_BY_TYPE[doc_type]:
        if not fields.get(name):
            add("missing_field", "error", f"{HEADER_FIELDS[name].label} is empty. Read it from the PDF and enter it.", name)
    if not lines:
        add("no_lines", "error", "There are no line items. Enter them from the PDF.", "lines")

    vendors = lookups.vendors()
    vendor = match_vendor(fields.get("vendor_name"), vendors)
    if fields.get("vendor_name") and vendor is None:
        add("vendor_unknown", "error",
            f"'{fields['vendor_name']}' is not in the vendor master. A new vendor must be set up through the normal "
            "vendor process (with its own checks) before anything can be paid. Reject this document, or correct the "
            "name if it was misread.", "vendor_name")

    # ---- currency ----------------------------------------------------------------------
    currency = fields.get("currency")
    if currency and currency not in policy.allowed_currencies:
        add("currency_not_allowed", "error",
            f"Currency {currency} is not accepted (allowed: {', '.join(policy.allowed_currencies)}).", "currency")
    elif currency and vendor and currency != vendor["currency"]:
        add("currency_unusual", "warning",
            f"{vendor['name']} normally bills in {vendor['currency']}; this document is in {currency}.", "currency")

    # ---- dates -------------------------------------------------------------------------
    main_date_field = "invoice_date" if doc_type == "invoice" else "order_date"
    main_date = parse_date(fields.get(main_date_field))
    label = HEADER_FIELDS[main_date_field].label
    if main_date and main_date > today + timedelta(days=policy.max_future_days):
        add("date_in_future", "blocking",
            f"{label} {main_date.isoformat()} is {(main_date - today).days} days in the future.", main_date_field)
    if doc_type == "invoice":
        due = parse_date(fields.get("due_date"))
        if main_date and due:
            if due < main_date:
                add("due_before_invoice", "blocking",
                    f"Due date {due.isoformat()} is before the invoice date {main_date.isoformat()}.", "due_date")
            elif due > main_date + timedelta(days=policy.max_payment_days):
                add("due_far_out", "warning",
                    f"Due date is {(due - main_date).days} days after the invoice date.", "due_date")
        elif main_date and not due:
            add("due_missing", "warning", "No due date was read. Your standard payment terms will apply.", "due_date")

    # ---- line arithmetic ---------------------------------------------------------------
    line_sum = computed_sum = Decimal("0")
    lines_complete = bool(lines)
    line_errors = False
    for i, line in enumerate(lines, 1):
        qty, price, amount = dec(line.get("quantity")), dec(line.get("unit_price")), dec(line.get("line_total"))
        missing = [n for n, v in (("quantity", qty), ("unit price", price), ("amount", amount)) if v is None]
        if missing:
            lines_complete = False
            add("line_incomplete", "error", f"Line {i} has no {' or '.join(missing)}.", "lines")
            continue
        assert qty is not None and price is not None and amount is not None
        line_sum += amount
        expected = q2(qty * price)
        computed_sum += expected
        if abs(expected - amount) > policy.line_tolerance:
            line_errors = True
            add("line_math", "blocking",
                f"Line {i}: {qty} x {price} = {money(expected)}, but the document says {money(amount)}.", "lines")

    # ---- totals and tax ----------------------------------------------------------------
    subtotal, tax, total = dec(fields.get("subtotal")), dec(fields.get("tax_amount")), dec(fields.get("total"))
    rate = dec(fields.get("tax_rate"))
    tol = policy.amount_tolerance
    rates = ", ".join(f"{r.normalize():f}" for r in policy.tax_rates)
    if subtotal is not None and lines_complete:
        if abs(line_sum - subtotal) > tol:
            add("subtotal_mismatch", "blocking",
                f"The lines add up to {money(line_sum)}, but the subtotal says {money(subtotal)}.", "subtotal")
        elif not line_errors and abs(computed_sum - subtotal) > tol:  # each line within a penny, but they add up
            add("subtotal_mismatch", "blocking",
                f"Quantity x price over all lines comes to {money(computed_sum)}, but the subtotal says "
                f"{money(subtotal)}. Small differences on each line add up.", "subtotal")
    if rate is None:
        if tax is not None and subtotal is not None and tax > 0:
            implied = [r for r in policy.tax_rates if abs(q2(subtotal * r / 100) - tax) <= tol]
            if not implied:
                share = (tax / subtotal * 100).quantize(Decimal("0.1")) if subtotal else Decimal("0")
                add("tax_rate_unusual", "blocking",
                    f"No tax rate is printed, and tax {money(tax)} on {money(subtotal)} is {share}%, which is not one "
                    f"of the expected rates ({rates}).", "tax_amount")
    else:
        if rate not in policy.tax_rates:
            add("tax_rate_unusual", "blocking", f"Tax rate {rate.normalize():f}% is not one of the expected rates ({rates}).",
                "tax_rate")
        if subtotal is not None and tax is not None:
            expected_tax = q2(subtotal * rate / 100)
            if abs(expected_tax - tax) > tol:
                add("tax_mismatch", "blocking",
                    f"{rate.normalize():f}% of {money(subtotal)} is {money(expected_tax)}, but the tax says {money(tax)}.",
                    "tax_amount")
    if subtotal is not None and tax is not None and total is not None and abs(subtotal + tax - total) > tol:
        add("total_mismatch", "blocking",
            f"Subtotal {money(subtotal)} + tax {money(tax)} = {money(subtotal + tax)}, but the total says {money(total)}.",
            "total")

    if doc_type == "invoice":
        _bank_checks(fields, corrected, original, vendor, add)
        _duplicate_checks(doc, fields, vendors, lookups, policy, add)
        _po_match(doc, fields, lines, vendor, lookups, policy, add)
    else:
        _purchase_order_checks(doc, fields, lines, vendor, lookups, add)
    return sort(out.findings)


def _last4(value: str | None) -> str:
    """Only the end of a vendor master value goes into a finding, so the message can be shown to
    anyone who may see the document (clerks too) without giving away the master's bank details."""
    return "".join(ch for ch in (value or "") if ch.isalnum())[-4:]


def _bank_checks(fields: dict, corrected: set[str], original: dict, vendor: dict | None, add: Any) -> None:
    iban = fields.get("vendor_iban")
    sort_code, account = fields.get("vendor_sort_code"), fields.get("vendor_account")
    typed = [f for f in BANK_FIELDS if fields.get(f) and f in corrected and not original.get(f)]
    if typed:
        add("bank_typed", "blocking",
            "Bank details were typed in by a person, not read by the system. Compare them once more with the PDF and "
            "with the vendor master before approving.", typed[0])
    if iban and not iban_is_valid(iban):
        add("bank_invalid", "error", "The IBAN on the invoice fails its check digits, so it is not a real account. "
            "Do not pay to it.", "vendor_iban")
    if not vendor:
        return
    if not (iban or sort_code or account):
        add("bank_missing", "blocking",
            "No bank details could be read from the invoice, so they were not compared with the vendor master. "
            "Look at the PDF: if it shows bank details (in any format), they must match the vendor master. "
            "Payment uses the vendor master.", "vendor_iban")
        return
    if not iban and bool(sort_code) != bool(account):
        missing = "account number" if sort_code else "sort code"
        add("bank_missing", "blocking",
            f"Only part of the UK bank details could be read: the {missing} is missing, so the bank details were not "
            "fully compared with the vendor master. Look at the PDF: the sort code and the account number must both "
            "match the vendor master. Payment uses the vendor master.",
            "vendor_account" if sort_code else "vendor_sort_code")
    master_iban = vendor.get("iban")
    from_iban = uk_details_from_iban(master_iban)
    master_sort = vendor.get("sort_code") or (from_iban[0] if from_iban else None)
    master_account = vendor.get("account_number") or (from_iban[1] if from_iban else None)
    changed = []
    if iban and iban_is_valid(iban):
        if not master_iban:
            add("bank_unverified", "blocking",
                f"The vendor master has no IBAN for {vendor['name']}, so the IBAN on the invoice cannot be compared. "
                "Confirm it with the vendor through a known contact.", "vendor_iban")
        elif iban != master_iban:
            changed.append(f"the IBAN (the vendor master's ends {_last4(master_iban)})")
    if sort_code or account:
        if not (master_sort and master_account):
            add("bank_unverified", "blocking",
                f"The vendor master has no UK sort code and account number for {vendor['name']}, so those on the "
                "invoice cannot be compared. Confirm them with the vendor through a known contact.", "vendor_sort_code")
        else:
            if sort_code and sort_code != master_sort:
                changed.append("the sort code")
            if account and account != master_account:
                changed.append(f"the account number (the vendor master's ends {_last4(master_account)})")
    if changed:
        add("bank_changed", "error",
            f"Bank details differ from the vendor master: {' and '.join(changed)}. Changed bank details are a common "
            "invoice fraud pattern. Do not approve. Call the vendor on a number from the vendor master, not one from "
            "this invoice. If the change is real, it is made in the vendor master through its own controlled process, "
            "then this document is re-checked.", "vendor_iban" if iban else "vendor_sort_code")


def _duplicate_checks(doc: dict, fields: dict, vendors: list[dict], lookups: Lookups, policy: Policy, add: Any) -> None:
    """Same vendor and the same invoice number: an error when the other copy is already approved
    or exported and either the number is printed the same way or the total is the same too;
    otherwise blocking, so a person compares them (a number that only looks alike, such as INV-11
    and INV-L1, may be another invoice).
    Same vendor and total under another number: blocking within the duplicate window; further
    apart, a warning naming the nearest one only, since regular invoices for one amount are normal."""
    number = fields.get("invoice_number")
    me = vendor_identity(fields.get("vendor_name"), vendors)
    flagged: set[str] = set()
    for other in lookups.same_number("invoice", number, doc["id"]):
        if vendor_identity(other["fields"].get("vendor_name"), vendors) != me:
            continue
        flagged.add(other["id"])
        printed = other["fields"].get("invoice_number")
        mine, theirs = dec(fields.get("total")), dec(other["fields"].get("total"))
        same_total = mine is not None and theirs is not None and abs(mine - theirs) <= policy.amount_tolerance
        if exact_number_key(printed) != exact_number_key(number) and other["status"] in COMMITTED and same_total:
            add("duplicate_invoice", "error",
                f"{other['id']} (file {other['filename']}) from this vendor is already {other['status']} with "
                f"invoice number {printed}, which reads the same as {number}, and the same total {money(mine)}. "
                "This copy cannot be approved; reject it.", "invoice_number", other["id"])
        elif exact_number_key(printed) != exact_number_key(number):
            add("possible_duplicate", "blocking",
                f"{other['id']} ({other['status']}, file {other['filename']}) from this vendor has invoice number "
                f"{printed}, which reads the same as {number} once spaces, punctuation, leading zeros and the "
                "look-alike letters O, I and L are set aside. Check they are not the same invoice.",
                "invoice_number", other["id"])
        elif other["status"] in COMMITTED:
            add("duplicate_invoice", "error",
                f"Invoice {printed} from this vendor is already {other['status']} as {other['id']} (file "
                f"{other['filename']}). This copy cannot be approved; reject it.", "invoice_number", other["id"])
        else:
            add("duplicate_invoice", "blocking",
                f"Invoice {printed} from this vendor is also waiting as {other['id']} ({other['status']}, file "
                f"{other['filename']}). Approve at most one of them.", "invoice_number", other["id"])
    this_day = parse_date(fields.get("invoice_date"))
    window = timedelta(days=policy.duplicate_window_days)
    total_text = money(dec(fields.get("total")) or Decimal("0"))
    nearest: tuple[int, dict] | None = None
    for other in lookups.invoices_near_total(fields.get("total"), policy.amount_tolerance, doc["id"]):
        if other["id"] in flagged or vendor_identity(other["fields"].get("vendor_name"), vendors) != me:
            continue
        other_day = parse_date(other["fields"].get("invoice_date"))
        if this_day and other_day and abs(this_day - other_day) > window:
            gap = abs(this_day - other_day).days
            if nearest is None or gap < nearest[0]:
                nearest = (gap, other)
            continue
        add("possible_duplicate", "blocking",
            f"{other['id']} ({other['status']}) has the same vendor and total {total_text} within "
            f"{policy.duplicate_window_days} days, under number {other['fields'].get('invoice_number')}. Check it is "
            "not the same invoice.", "invoice_number", other["id"])
    if nearest:
        gap, other = nearest
        add("possible_duplicate", "warning",
            f"{other['id']} ({other['status']}) has the same vendor and total {total_text}, {gap} days apart, under "
            f"number {other['fields'].get('invoice_number')}. Regular invoices for one amount are normal; check this "
            "is not an old invoice sent again.", "invoice_number", other["id"])


def _po_match(doc: dict, fields: dict, lines: list[dict], vendor: dict | None, lookups: Lookups, policy: Policy,
              add: Any) -> None:
    """2-way match: invoice against the PO register (from the ERP). Goods receipts are not checked."""
    po_number = fields.get("po_number")
    if not po_number:
        add("po_missing", "blocking" if policy.require_po else "warning",
            "The invoice quotes no PO number. Under your policy it needs sign-off from the budget holder; record who "
            "agreed in the note.", "po_number")
        return
    po = lookups.po(po_number)
    if po is None:
        add("po_not_found", "blocking", f"{po_number} is not in the PO register.", "po_number")
        return
    if po["status"] != "open":
        add("po_closed", "blocking", f"{po['number']} is {po['status']}, not open.", "po_number")
    if vendor and po["vendor_id"] != vendor["id"]:
        add("po_vendor_mismatch", "blocking",
            f"{po['number']} was raised for vendor {po['vendor_id']}, not for {vendor['name']} ({vendor['id']}).",
            "po_number")
    if fields.get("currency") and fields["currency"] != po["currency"]:
        add("po_currency_mismatch", "blocking",
            f"{po['number']} is in {po['currency']}; the invoice is in {fields['currency']}.", "currency")

    def po_line_for(line: dict) -> tuple[int, dict] | None:
        """The PO line an invoice line belongs to: same item code, or (without a code) same description."""
        sku = (line.get("sku") or "").upper()
        for index, po_line in enumerate(po["lines"]):
            if sku and sku == str(po_line.get("sku") or "").upper():
                return index, po_line
            if not sku and line.get("description") and name_key(line["description"]) == name_key(po_line.get("description")):
                return index, po_line
        return None

    prior = lookups.invoices_for_po(po["number"], doc["id"])
    prior_qty: dict[int, Decimal] = defaultdict(Decimal)
    for other in prior:
        for line in other["lines"]:
            match, qty = po_line_for(line), dec(line.get("quantity"))
            if match and qty is not None:
                prior_qty[match[0]] += qty
    this_qty: dict[int, Decimal] = defaultdict(Decimal)
    for i, line in enumerate(lines, 1):
        match = po_line_for(line)
        label = line.get("sku") or line.get("description") or f"line {i}"
        if match is None:
            add("po_line_not_found", "blocking", f"Line {i} ({label}) is not on {po['number']}.", "lines")
            continue
        key, po_line = match
        qty, price = dec(line.get("quantity")), dec(line.get("unit_price"))
        po_price, po_qty = Decimal(str(po_line["unit_price"])), Decimal(str(po_line["quantity"]))
        if price is not None and po_price == 0 and price > 0:
            add("po_price_mismatch", "blocking",
                f"Line {i} ({label}): unit price {price}, but {po['number']} has this item at 0.00.", "lines")
        elif price is not None and po_price > 0:
            diff_pct = abs(price - po_price) / po_price * 100
            if diff_pct > policy.po_price_tolerance_pct:
                add("po_price_mismatch", "blocking",
                    f"Line {i} ({label}): unit price {price} vs {po_price} on {po['number']} "
                    f"({'+' if price > po_price else '-'}{diff_pct.quantize(Decimal('0.1'))}%, tolerance "
                    f"{policy.po_price_tolerance_pct}%).", "lines")
        if qty is not None:
            this_qty[key] += qty
            already = prior_qty.get(key, Decimal("0"))
            allowed = po_qty * (1 + policy.po_qty_tolerance_pct / 100)
            if already + this_qty[key] > allowed:
                remaining = max(po_qty - already, Decimal("0"))
                add("po_qty_exceeded", "blocking",
                    f"Line {i} ({label}): invoicing {this_qty[key].normalize():f}; {po['number']} ordered "
                    f"{po_qty.normalize():f}, {already.normalize():f} already on approved invoices, so "
                    f"{remaining.normalize():f} remain.", "lines")

    po_net = sum((q2(Decimal(str(ln["quantity"])) * Decimal(str(ln["unit_price"]))) for ln in po["lines"]), Decimal("0"))
    invoiced = Decimal(str(po.get("already_invoiced") or "0")) + sum(
        (dec(o["fields"].get("subtotal")) or Decimal("0") for o in prior), Decimal("0"))
    remaining = po_net - invoiced
    subtotal = dec(fields.get("subtotal"))
    if subtotal is not None and subtotal > remaining + policy.amount_tolerance:
        add("po_amount_exceeded", "blocking",
            f"Subtotal {money(subtotal)} is more than what remains on {po['number']}: {money(max(remaining, Decimal('0')))} "
            f"of {money(po_net)} (already invoiced {money(invoiced)}).", "subtotal")
    elif subtotal is not None:
        add("po_matched", "info",
            f"Matched to {po['number']}: {money(remaining)} of {money(po_net)} remained, {money(remaining - subtotal)} "
            "after this invoice. Goods receipts are not checked (2-way match).", "po_number")


def _purchase_order_checks(doc: dict, fields: dict, lines: list[dict], vendor: dict | None, lookups: Lookups,
                           add: Any) -> None:
    """A PO document is compared with the PO register from the ERP. Approving it never adds it
    to the register: nobody in this tool can create the PO an invoice is matched against."""
    number = fields.get("po_number")
    if not number:
        return
    for other in lookups.same_number("purchase_order", number, doc["id"]):
        printed = other["fields"].get("po_number")
        if exact_number_key(printed) != exact_number_key(number):
            add("duplicate_po", "blocking", f"{other['id']} ({other['status']}) is purchase order {printed}, which "
                f"reads the same as {number} once punctuation, leading zeros and look-alike letters are set aside. "
                "Check they are not the same order.", "po_number", other["id"])
        else:
            severity = "error" if other["status"] in COMMITTED else "blocking"
            add("duplicate_po", severity, f"{other['id']} ({other['status']}) is also purchase order {number}.",
                "po_number", other["id"])
    po = lookups.po(number)
    if po is None:
        add("po_not_in_register", "blocking",
            f"{number} is not in the PO register from the ERP. Approving this document exports it for purchasing; "
            "it does not add it to the register, so invoices cannot be matched against it until it is entered in "
            "the ERP.", "po_number")
        return
    differences = []
    if vendor and po["vendor_id"] != vendor["id"]:
        differences.append(f"vendor {vendor['id']} (register: {po['vendor_id']})")
    po_net = sum((q2(Decimal(str(ln["quantity"])) * Decimal(str(ln["unit_price"]))) for ln in po["lines"]), Decimal("0"))
    subtotal = dec(fields.get("subtotal"))
    if subtotal is not None and subtotal != po_net:
        differences.append(f"net amount {money(subtotal)} (register: {money(po_net)})")
    if len(lines) != len(po["lines"]):
        differences.append(f"{len(lines)} lines (register: {len(po['lines'])})")
    if differences:
        add("po_differs", "blocking", f"This PDF differs from {po['number']} in the PO register: "
            f"{'; '.join(differences)}. The register (from the ERP) is what invoices are matched against.", "po_number")
    else:
        add("po_in_register", "info", f"Vendor, net amount and number of lines match {po['number']} in the PO "
            "register. The lines are not compared one by one.", "po_number")


def sort(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: SEVERITIES.index(f.severity) if f.severity in SEVERITIES else 9)


def summary(findings: list[dict]) -> str:
    """'1 error, 2 blocking' style summary for lists."""
    counts = {s: 0 for s in SEVERITIES}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    parts = [f"{counts[s]} {s if s in ('blocking', 'info') else s + ('s' if counts[s] > 1 else '')}"
             for s in ("error", "blocking", "warning") if counts[s]]
    return ", ".join(parts) or "no issues"
