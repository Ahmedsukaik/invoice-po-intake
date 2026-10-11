"""Read PDFs dropped into the inbox folder.

    python -m intake.inbox            process what is there now, then stop
    python -m intake.inbox --watch    keep looking every 10 seconds (Ctrl+C to stop)

It uses the same database and storage as the server (settings from .env), and acts as the
user "inbox-watcher" with the clerk role: it can add documents, nothing else.
"""
from __future__ import annotations

import argparse
import time

from .config import Settings
from .deps import Deps
from .pipeline import scan_inbox


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read PDFs from the inbox folder into the review queue")
    parser.add_argument("--watch", action="store_true", help="keep watching the folder")
    parser.add_argument("--interval", type=float, default=10.0, help="seconds between scans with --watch")
    args = parser.parse_args(argv)
    deps = Deps.from_settings(Settings.from_env())
    print(f"Inbox: {deps.inbox_dir.resolve()}")
    while True:
        # With --watch, leave files that changed in the last 2 seconds: they may still be copying.
        for item in scan_inbox(deps, min_age_seconds=2.0 if args.watch else 0.0):
            if item.error:
                print(f"  {item.file}: refused ({item.error}), moved to inbox/failed")
            elif not item.created:
                print(f"  {item.file}: same file as {item.document_id}, not added again")
            else:
                print(f"  {item.file}: {item.document_id} [{item.status}]")
        if not args.watch:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
