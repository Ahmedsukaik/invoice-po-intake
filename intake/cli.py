"""Command line for the same actions as the web page (it calls the HTTP API).

    python -m intake.cli list [--status open|needs_review|needs_manual_entry|approved|rejected|exported|all]
    python -m intake.cli show doc_1a2b3c4d5e6f
    python -m intake.cli upload invoice.pdf [more.pdf ...]
    python -m intake.cli correct doc_1a2b3c4d5e6f --version 1 total=1800.00 due_date=2026-11-04 [--lines lines.json] [--note "..."]
    python -m intake.cli recheck doc_1a2b3c4d5e6f
    python -m intake.cli read-again doc_1a2b3c4d5e6f
    python -m intake.cli approve doc_1a2b3c4d5e6f --version 1 [--ack 3f9c2a1b7e ...] [--note "why"]
    python -m intake.cli reject doc_1a2b3c4d5e6f --reason "duplicate of INV-A-1001"
    python -m intake.cli export
    python -m intake.cli resolve-export exp_20261011_101500_1a2b3c --written yes|no
    python -m intake.cli audit [--limit 30]

`show` prints the document's version and, for each blocking finding, its id. `approve` and
`correct` need --version: the version you looked at. If the document changed since, the
action is refused. --ack takes the id of each blocking finding you checked.

Reads INTAKE_URL and INTAKE_TOKEN from the environment or from .env. What you may do
depends on the token's role.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from .envfile import load_env_file
from .values import HEADER_FIELDS, clean_text, iban_display, money_text

SLOW_SECONDS = 300  # upload and read-again; longer than the server can take for one PDF


def show(value: object, limit: int = 2000) -> str:
    """Text from documents is untrusted: print it on one line, with control characters removed,
    so a crafted PDF cannot fake another line of output."""
    return clean_text(value, limit)


def cell(value: object, width: int) -> str:
    text = show(value, 200)
    return (text if len(text) <= width else text[:width - 1] + "~").ljust(width)


def print_summary(d: dict) -> None:
    total = f"{money_text(d.get('total'))} {show(d.get('currency') or '')}".strip()
    print(f"{cell(d['id'], 16)}  {cell(d['status'], 18)}  {cell(d.get('doc_type') or '?', 14)}  "
          f"{cell(d.get('vendor_name') or '-', 30)}  {cell(d.get('number') or '-', 14)}  {total:>16}  {show(d.get('checks', ''))}")


def print_document(doc: dict) -> None:
    f = doc["fields"]
    print(f"{show(doc['id'])}  [{show(doc['status'])}]  {show(doc['filename'])}  "
          f"(version {doc['version']}, read by {show(doc['extractor'])})")
    if doc.get("manual_reason") and doc["status"] in ("needs_review", "needs_manual_entry"):
        print(f"    Needs manual entry: {show(doc['manual_reason'])}")
    print("  Fields:")
    for name, spec in HEADER_FIELDS.items():
        value = f.get(name)
        if value is None and name not in ("doc_type", "vendor_name"):
            continue
        shown = iban_display(value) if spec.kind == "iban" else money_text(value) if spec.kind == "amount" else value
        mark = "  (corrected)" if name in doc.get("corrected", []) else ""
        print(f"    {spec.label:<20} {show(shown if shown is not None else '(empty)')}{mark}")
    if doc["lines"]:
        print("  Lines:")
        for i, line in enumerate(doc["lines"], 1):
            print(f"    {i}. {show(line.get('sku') or '-')}  {show(line.get('description') or '', 60)}  "
                  f"{show(line.get('quantity'))} x {show(line.get('unit_price'))} = {money_text(line.get('line_total'))}")
    print("  Findings:" if doc["findings"] else "  Findings: none")
    for x in doc["findings"]:
        ack = f" [ack id {show(x.get('id'))}]" if x["severity"] == "blocking" else ""
        print(f"    {show(x['severity']).upper():<8} {show(x['code']):<20} {show(x['message'])}{ack}")
    for c in doc.get("corrections", []):
        print(f"  Corrected by {show(c['corrected_by'])}: {show(c['field'])} {show(c['old_value'], 80)!r} -> {show(c['new_value'], 80)!r}")
    if doc.get("decided_by"):
        print(f"  Decision: {show(doc['status'])} by {show(doc['decided_by'])} at {show(doc['decided_at'])}"
              + (f": {show(doc['decision_note'])}" if doc.get("decision_note") else ""))


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description="Invoice and PO intake from the command line")
    parser.add_argument("--url", default=os.environ.get("INTAKE_URL", "http://127.0.0.1:8200"))
    parser.add_argument("--token", default=os.environ.get("INTAKE_TOKEN"))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list", help="documents in the queue")
    p.add_argument("--status", default="open")
    p.add_argument("--limit", type=int, default=100)
    sub.add_parser("show", help="one document with fields, lines and findings").add_argument("doc_id")
    sub.add_parser("upload", help="upload PDF files").add_argument("files", nargs="+")
    p = sub.add_parser("correct", help="correct fields: name=value ...")
    p.add_argument("doc_id")
    p.add_argument("changes", nargs="*", help="field=value (an empty value clears a field the reader left empty)")
    p.add_argument("--version", type=int, required=True, help="the version shown by `show`")
    p.add_argument("--lines", help="a JSON file with the full list of line items")
    p.add_argument("--note", default=None)
    sub.add_parser("recheck", help="run the checks again").add_argument("doc_id")
    sub.add_parser("read-again", help="read the stored PDF again (nothing corrected yet)").add_argument("doc_id")
    p = sub.add_parser("approve", help="approve a document")
    p.add_argument("doc_id")
    p.add_argument("--version", type=int, required=True, help="the version shown by `show`")
    p.add_argument("--ack", action="append", default=[], help="the id of a blocking finding you checked (see `show`)")
    p.add_argument("--note", default=None)
    p = sub.add_parser("reject", help="reject a document")
    p.add_argument("doc_id")
    p.add_argument("--reason", default="")
    sub.add_parser("export", help="export approved documents")
    p = sub.add_parser("resolve-export", help="settle an export batch left in doubt after a crash")
    p.add_argument("batch_id")
    p.add_argument("--written", required=True, choices=["yes", "no"],
                   help="yes if the target system received the batch, no if it did not")
    sub.add_parser("audit", help="the audit log").add_argument("--limit", type=int, default=30)
    args = parser.parse_args(argv)
    if not args.token:
        print("Set INTAKE_TOKEN in .env or pass --token.", file=sys.stderr)
        return 2

    # Reading a PDF can take minutes with the Claude reader (a wait for a free PDF worker, the parse
    # time limit, then up to two API attempts of 60 seconds each), so those commands wait longer.
    slow = args.command in ("upload", "read-again")
    try:
        with httpx.Client(base_url=args.url.rstrip("/"), headers={"Authorization": f"Bearer {args.token}"},
                          timeout=httpx.Timeout(SLOW_SECONDS if slow else 60, connect=10)) as http:
            return run(http, args)
    except httpx.ReadTimeout:
        print("The server did not answer in time. If it was reading a PDF, it may still be working on it: run "
              "`python -m intake.cli list` in a minute to see the result.", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"Could not reach the intake server at {args.url}: {type(exc).__name__}. Is it running?", file=sys.stderr)
        return 1


def run(http: httpx.Client, args: argparse.Namespace) -> int:
    def check(resp: httpx.Response) -> dict | None:
        try:
            data = resp.json()
        except ValueError:
            data = {"error": resp.text[:200]}
        if resp.status_code >= 400:
            print(f"Error {resp.status_code}: {show(data.get('error', data))}", file=sys.stderr)
            for x in data.get("findings") or []:
                print(f"    {show(x['severity']).upper():<8} {show(x['code']):<20} {show(x['message'])} "
                      f"[ack id {show(x.get('id'))}]", file=sys.stderr)
            if resp.status_code == 401:
                print("Check INTAKE_TOKEN in .env: the server does not know this token.", file=sys.stderr)
            return None
        return data

    cmd = args.command
    if cmd == "list":
        data = check(http.get("/api/documents", params={"status": args.status, "limit": args.limit}))
        if data is None:
            return 1
        if not data["documents"]:
            print("Nothing waiting for review." if args.status == "open" else f"No {args.status} documents.")
        else:
            print(f"{'ID':<16}  {'STATUS':<18}  {'TYPE':<14}  {'VENDOR':<30}  {'NUMBER':<14}  {'TOTAL':>16}  CHECKS")
        for d in data["documents"]:
            print_summary(d)
        if data.get("total", 0) > len(data["documents"]):
            print(f"(showing {len(data['documents'])} of {data['total']}; use --limit)")
        return 0
    if cmd == "upload":
        code = 0
        for name in args.files:
            path = Path(name)
            if not path.is_file():
                print(f"{name}: no such file", file=sys.stderr)
                code = 1
                continue
            with path.open("rb") as fh:
                data = check(http.post("/api/documents", files={"file": (path.name, fh, "application/pdf")}))
            if data is None:
                code = 1
                continue
            doc = data["document"]
            note = "added" if data["created"] else "same file already in the system"
            print(f"{path.name}: {show(doc['id'])} [{show(doc['status'])}] {note}")
        return code
    if cmd == "export":
        data = check(http.post("/api/exports"))
        if data is None:
            return 1
        batch = data["batch"]
        if not batch:
            print(data["message"])
            return 0
        print(f"Batch {show(batch['batch_id'])}: {batch['count']} document(s) -> {show(batch['folder'])}")
        print("Files: " + ", ".join(show(f) for f in batch["files"]))
        return 0
    if cmd == "resolve-export":
        data = check(http.post(f"/api/exports/{args.batch_id}/resolve", json={"written": args.written == "yes"}))
        if data is None:
            return 1
        print(f"Batch {show(data['batch_id'])}: {show(data['resolved'])}")
        return 0
    if cmd == "audit":
        data = check(http.get("/api/audit", params={"limit": args.limit}))
        if data is None:
            return 1
        for e in reversed(data["entries"]):
            print(f"{show(e['ts']).replace('T', ' ').replace('+00:00', '')}  {cell(e['actor'], 24)} "
                  f"{cell(e['action'], 12)} {cell(e['document_id'] or '-', 17)} {cell(e['outcome'], 14)} "
                  f"{show(e['detail'] or '', 120)}")
        return 0

    doc_id = args.doc_id
    if cmd == "show":
        data = check(http.get(f"/api/documents/{doc_id}"))
    elif cmd == "recheck":
        data = check(http.post(f"/api/documents/{doc_id}/recheck"))
    elif cmd == "read-again":
        data = check(http.post(f"/api/documents/{doc_id}/read-again"))
    elif cmd == "correct":
        fields = {}
        for change in args.changes:
            name, sep, value = change.partition("=")
            if not sep:
                print(f"'{change}' is not field=value", file=sys.stderr)
                return 2
            fields[name.strip()] = value
        body: dict[str, Any] = {"fields": fields, "note": args.note, "version": args.version}
        if args.lines:
            body["lines"] = json.loads(Path(args.lines).read_text(encoding="utf-8"))
        data = check(http.post(f"/api/documents/{doc_id}/corrections", json=body))
    elif cmd == "approve":
        data = check(http.post(f"/api/documents/{doc_id}/approve",
                               json={"acknowledge": args.ack, "note": args.note, "version": args.version}))
    else:
        data = check(http.post(f"/api/documents/{doc_id}/reject", json={"reason": args.reason}))
    if data is None:
        return 1
    print_document(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
