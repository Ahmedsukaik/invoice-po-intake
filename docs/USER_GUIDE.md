# User guide for AP clerks and reviewers

This guide is for the people who add supplier invoices and purchase orders, and for the
people who check and approve them. You do not need to know how the system is built.

Words used here: a **document** is one PDF, an invoice or a purchase order (PO). The
**queue** is the list of documents waiting for a decision. A **finding** is something the
checks noticed, for example "The total does not add up". The **vendor master** is the list
of suppliers you pay, with their bank details. The **PO register** is the list of purchase
orders that invoices are matched against. Both come from your ERP; nobody changes them in
this tool.

## What happens to a PDF

1. A clerk uploads it, or drops it into the inbox folder.
2. The system reads it: who sent it, the numbers, the dates, the lines, the totals. Next to
   each value it shows where on the PDF it was read.
3. It checks it: is this one of our suppliers, are the bank details the ones we have, do
   the lines and totals add up, is the tax rate normal, does it match the PO, have we seen
   it before.
4. It waits in the queue for a reviewer. **Nothing is passed to accounting until a reviewer
   approves it.**
5. An administrator exports approved documents to files for your accounting system, as
   often as your team decides (for example once a day). Each document is exported only
   once.

If the PDF is a scan (a picture with no text in it), the system does not try to read it.
It goes to the queue as **Needs manual entry**, and a reviewer types the values in.

## Before you start

Your administrator gives you an access token (a long password-like code) and the address
of the page, for example `https://intake.yourcompany.example`. Open the address, paste the
token and choose **Sign in**. You stay signed in for eight hours.

The page also works on a phone. There, the findings and the decision come first, then the
values, and each document in the queue is a card. Typing in a whole scanned invoice is
easier on a computer.

What you can do depends on your role:

| Role | Can |
|---|---|
| Clerk | add documents, look at the queue and every document |
| Reviewer | look, correct values, approve, reject |
| Administrator | look, export approved documents, read the audit log |

Clerks see bank details only as their last four characters: in the vendor master and on
each document. The "Text read from the PDF" box is shown to reviewers only, for the same
reason.

## For clerks: adding documents

**Upload** one PDF at a time: choose **Upload** at the top, pick the file, choose
**Upload**. You see the document straight away, with what was read and what the checks
found.

Or **drop PDFs into the inbox folder** your administrator set up. They are picked up
within about ten seconds. Read files move to `inbox/processed`. A file that is not a PDF,
or is too large, moves to `inbox/failed`, with a short text file next to it that says why.
If the reader is busy for a moment (with other files, or the AI reader is not available),
the file stays in the inbox and is tried again on the next round.

If you add the same file twice, nothing new is created; the page tells you and takes you
to the existing document. That is on purpose.

## For reviewers: the queue

**Queue** shows what is waiting (the **To review** tab). Each line shows the vendor, the
number, the total, the status and a short summary of the checks:

- `no issues` (green): nothing found.
- `warning` (yellow): read it; it does not stop you approving.
- `blocking` (orange): you can approve only if you tick it off and say why.
- `error` (red): it cannot be approved as it is.

The newest documents come first, 100 to a page; use **Older** and **Newer** to move
between pages. Open a document by its file name.

## For reviewers: checking a document

The page has four parts.

**Fields.** Each value, and on the right where it was read, for example
`p.1 "Total due 1,800.00"`. That is the line of text in the PDF the value came from. A
coloured bar on the left of a row means a finding is about that value. **Open original
PDF** shows the file itself. Compare with it when anything looks unusual.

**Line items.** Each line, and in the last column quantity times unit price. A green tick
means the line adds up; a red cross means it does not.

**Findings.** Every finding, with a short title and a sentence that says what is wrong in
numbers. When a finding is about another document (a duplicate, for example), it links to
it. The checks run again each time you open the page (or `show` the document on the
command line), so what you see is current.

**Decision.** What you can do now.

### Approve

- **No findings, or only warnings or info:** read them, then choose **Approve**.
- **Blocking findings:** check each one against the PDF and with the people involved. If
  you still want to approve, tick the box for each one (it reads: I have checked "..." and
  approve anyway), write in **Why** what you checked (for example "Buyer agreed the extra
  40 units by email on 9 October"), and choose **Approve**. Your note and the ticked findings are saved with
  the approval and go into the export.
- **Errors:** the **Approve** button is greyed out, and the page says why. See "Findings
  you will see often" below.

The checks run again at the moment you approve, on the values as they are then. If
something changed in the meantime (another reviewer corrected a value, another invoice
used up the PO, or the vendor master changed), approval is refused and the page shows the
new findings. A tick counts only for the finding as you saw it: if its numbers changed,
tick it again. Look again and decide again.

**Four eyes.** If you typed in or changed any value on a document (including a value the
system left empty, such as a PO number, or a line), you cannot approve that document
yourself. The **Approve** button is greyed out for you and the page says why. Another
reviewer must approve it. You can still reject it.

### Reject

Write a reason in **Reason for rejecting** and choose **Reject**. Examples: "Duplicate of
INV-A-1001", "Not one of our suppliers", "Totals wrong, asked supplier for a corrected
invoice". A rejected document is never exported. If the supplier sends a corrected
invoice, it is added as a new document.

### Correct a value

If the system read a value wrongly, or could not read it, choose **Correct fields**, type
what the PDF says and choose **Save corrections**. Dates are written `2026-10-05`, amounts
`1500.00` (a comma for thousands is fine: `1,500.00`), tax rates as a number (`20`).

Every change is saved with the old value, the new value, your name and the time, and the
checks run again. To remove a line, clear all its boxes. Use the empty rows to add lines;
if you need more, choose **More empty rows**: it keeps what you typed and saves nothing
yet. If something you
typed is refused, nothing is saved, and what you typed stays in the form so you can fix it.

A value the system read from the PDF can be changed, but not deleted. And when you change
one, the document gets the finding "A value read from the PDF was changed": whoever
approves it ticks it off, so a correction is always confirmed on purpose. That way a
correction can never make another problem disappear without anyone noticing.

Only correct a value to what the PDF really says. If the PDF itself is wrong (for example
the total does not add up), do not "fix" it: reject it and ask the supplier for a corrected
invoice.

**Bank details read from the PDF cannot be changed.** That is on purpose; see "Bank
details differ from the vendor master" below.

### Type in a scanned document

A document marked **Needs manual entry** has empty fields. Open the PDF, choose **Correct
fields**, set **Document type** first, then type every value and every line from the PDF,
including the bank details if they are printed (an IBAN, or a UK sort code and account
number). Save. The checks then run as usual. Typed bank details show "Bank details were
typed in by a person". The page then says "Typed in by" and your name: because you typed
it, a colleague approves it (you can still reject it).

### Read the PDF again

If a document went to **Needs manual entry** because the reader was busy with other files
or the AI reader was not available, choose **Read the PDF again** to try once more. This works only while nobody
has corrected the document, so typed values are never lost.

## Findings you will see often

The page shows the title in the first column. The code in brackets is what the command
line shows.

| Finding | What it means | What you do |
|---|---|---|
| Bank details differ from the vendor master (`bank_changed`) | The IBAN, sort code or account number on the invoice is not the one in the vendor master. The finding shows only the last four characters of the vendor master's details. This is the most common invoice fraud: a fake or altered invoice asking you to pay a new account. It cannot be approved. | Phone the supplier on a number from the vendor master or an earlier contract, never one printed on this invoice. If they did not change banks, reject the document and tell your manager. If they did, the change goes through your normal vendor master process; afterwards choose **Re-run checks**. |
| Not a supplier in the vendor master (`vendor_unknown`) | The sender is not in the vendor master. It cannot be approved, and correcting the name does not change that: the check also runs on the name as first read. | Reject it. A new supplier is set up through your vendor process first; then the invoice is sent again. |
| Same invoice already in the system (`duplicate_invoice`) | The same supplier and the same invoice number, printed the same way (only capitals, spaces and punctuation may differ, as in `INV-A-1001` and `inv a/1001`). The finding links to the other copy. It is also used when the number only looks alike (`INV-A-01001` and `INV-A-1001`) but the other copy is already approved or exported with the same total. | If the other copy is already approved or exported, this one cannot be approved: reject it. If both are waiting, approve at most one. |
| Possibly the same invoice (`possible_duplicate`), must be ticked | The same supplier and a number that is the same once zeros and look-alike letters are set aside (`INV-2026-0001` and `INV-2026-001`, or `INV-11` and `INV-L1`); or the same supplier and total as another invoice within about ten days, under a different number. | Open the other document from the link and compare the two. Approve only if they are really different invoices, and say how you know. |
| Possibly the same invoice (`possible_duplicate`), as a warning | The same supplier and total as an earlier invoice, more than about ten days apart. | Usually a regular invoice for the same amount (rent, a subscription). Check it is not an old invoice sent again. |
| A line does not add up, The lines do not add up to the subtotal, The tax does not match the rate, The total does not add up (`line_math`, `subtotal_mismatch`, `tax_mismatch`, `total_mismatch`) | The numbers on the document do not add up. | If the system misread a number, correct it. If the PDF is wrong, reject and ask for a corrected invoice. |
| Unusual tax rate (`tax_rate_unusual`) | The rate is not one of your usual rates, or no rate is printed and the tax does not fit any of them. | Check the tax with the supplier or your tax adviser before approving. |
| Quantity / Amount above what the PO has left (`po_qty_exceeded`, `po_amount_exceeded`) | The invoice asks for more than the PO has left (earlier approved invoices are counted). | Check with the buyer. Approve only with their agreement, and write it in the note. |
| Price differs from the PO (`po_price_mismatch`) | A unit price differs from the PO by more than 2%. | Check with the buyer. |
| PO not in the PO register, PO is for another supplier (`po_not_found`, `po_vendor_mismatch`) | The PO number is unknown, or the PO belongs to another supplier. | Check the PO number on the PDF; correct it if it was misread. |
| No purchase order number (`po_missing`) | No PO number on the invoice. | Under most policies the budget holder must agree first. Write who agreed in the note. |
| No bank details could be read (`bank_missing`) | No IBAN, sort code or account number could be read from the PDF, or only half of the UK details (a sort code without an account number, or the other way round). | Look at the PDF. If it shows bank details, they must be the ones in the vendor master; if they differ, treat it like "Bank details differ". Then tick it off and say what you checked. Payment always uses the vendor master. |
| Bank details cannot be compared (`bank_unverified`) | The vendor master has no bank details for this supplier. | Confirm the bank details with the supplier through a known contact before approving. |
| Bank details were typed in by a person (`bank_typed`) | Someone typed the bank details in (for example from a scan). | Compare them once more with the PDF and the vendor master, then tick it off. |
| A value read from the PDF was changed (`value_corrected`) | A person changed a value the system read. The finding shows the old and the new value. | Check the PDF really says the new value, then tick it off. |
| A date could be read two ways (`date_ambiguous`) | A date like `05/10/2026` could be 5 October or 10 May. | Check the PDF and the supplier's country; correct the date if needed, then tick it off. |
| Source of a value not found in the PDF, No source given for a value (`evidence_not_found`, `evidence_missing`) | The AI reader (when it is switched on) gave a value it could not point to in the PDF. | Compare that value with the PDF and correct it if needed. |
| PO not in the PO register, PO differs from the PO register (`po_not_in_register`, `po_differs`) | A purchase order document that your ERP does not have, or whose supplier, net amount or number of lines differs from it. | Check with purchasing. Approving the document only records that you checked it; it does not add the PO to the register. |
| Reader notes (`reader_...`) | The reader was not sure about a value. | Compare that value with the PDF and correct it if needed. The note disappears once you correct the field. |
| The AI reader noted something (`model_concern`) | The AI reader (when it is switched on) thought a person should look at something. | Read it and check the PDF. This note stays on the document. |
| Matched to the PO (`po_matched`, info) | How much of the PO was left before and after this invoice. | Nothing. |

Goods received are not checked here (only the invoice against the PO). If your process
needs proof of delivery, check it before approving.

## What the statuses mean

| Status | Meaning | What you do |
|---|---|---|
| Needs review | Read and checked, waiting for you | Approve or reject |
| Needs manual entry | Not read (scan, password, unusual layout, reading service not available) | Type it in, or read it again; then a colleague approves or rejects |
| Approved | Approved; waiting for the next export | Nothing |
| Rejected | Rejected, with the reason shown | Nothing |
| Exporting | Being written to an export. If it stays here, the export was interrupted | Tell your administrator |
| Exported | Written to an export file for accounting | Nothing |

## Using the command line instead

Everything above also works from a terminal in the project folder, with your token in
the `.env` file as `INTAKE_TOKEN`:

```
python -m intake.cli list
python -m intake.cli show doc_1a2b3c4d5e6f
python -m intake.cli correct doc_1a2b3c4d5e6f total=1800.00 --version 1 --note "misread"
python -m intake.cli approve doc_1a2b3c4d5e6f --version 2
python -m intake.cli approve doc_1a2b3c4d5e6f --version 2 --ack 3f9a1c27b0 --note "agreed with the buyer"
python -m intake.cli reject doc_1a2b3c4d5e6f --reason "duplicate of INV-A-1001"
python -m intake.cli read-again doc_1a2b3c4d5e6f
python -m intake.cli upload invoice.pdf
```

`show` prints the document's version and its findings as of now, with an id next to each
blocking finding
(`[ack id 3f9a1c27b0]`). `correct` and `approve` need the version, so nothing can change
between what you read and what you approve. Pass `--ack` once for each blocking finding
you checked. `upload` and `read-again` can take a few minutes with the AI reader; if the
command stops waiting, run `list` a little later to see the document.

Administrators export with `python -m intake.cli export`, or on the **Exports** page with
**Export now**; that page also shows the export folder and lets you download each file.
They read the log with `python -m intake.cli audit`. If an export was interrupted (for
example the accounting system did not answer) and the system cannot tell whether the batch
arrived, its documents wait as **Exporting**. Check the accounting system, then run
`python -m intake.cli resolve-export <batch id> --written yes` (or `no`, to export them
again with the next export).

## Common questions

**I approved something by mistake.**
There is no undo in this version. Tell your administrator straight away. The document goes
out with the next export and is then corrected in the accounting system as you do today
(for example by voiding the bill there).

**"The document changed after you opened it."**
Someone else corrected or decided it while you had it open. Open it again; you see the
current values and findings.

**"Each blocking finding must be checked and ticked before approving: ..."**
Tick the box for every blocking finding named in the message, and write why in **Why**.
If you had ticked them already, a finding changed since: check it again.

**"This document has errors that must be fixed first."**
Correct the value if it was misread. If it cannot be fixed (unknown supplier, changed bank
details, a currency not accepted, a copy of an invoice already approved), reject it. These are
checked on the values as first read from the PDF too, so changing a value does not lift them,
and the document type read from the PDF cannot be changed at all.

**"You typed in or changed values on this document, so another reviewer must approve it."**
That is the four-eyes rule. Ask a colleague to approve it.

**"Bank details read from the PDF cannot be changed here."**
That is on purpose. See "Bank details differ from the vendor master" above.

**The supplier says they sent the invoice twice.**
The second copy shows "Same invoice already in the system", with a link to the first one.
Reject the second.

**I am sent back to the sign-in page.**
Your session ended (after eight hours), or your token was changed. Sign in again, or ask
your administrator for a new token.

**"role ... cannot ..."**
Your role does not allow that action. Ask a colleague with the right role, or your
administrator.

**Who did what?**
Each document shows its **History** at the bottom. Administrators see everything under
**Audit log**. Times are in UTC.
