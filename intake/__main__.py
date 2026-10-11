"""Run the server:  python -m intake [--host 127.0.0.1] [--port 8200] [--allowed-host intake.example.com]"""
from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .config import Settings
from .deps import Deps
from .server import build_app

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Invoice and PO intake: API and reviewer web page")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--allowed-host", action="append", default=[],
                        help="hostname clients use (repeatable); required when --host is not a loopback address")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    public = args.host not in LOOPBACK
    if public and settings.uses_placeholders:
        raise SystemExit("Refusing to listen on a public address with the change-me tokens from .env.example.")
    if public and not args.allowed_host:
        raise SystemExit("Pass --allowed-host with the hostname clients use when listening on a public address.")
    if settings.uses_placeholders:
        print("WARNING: running with the change-me tokens from .env.example. Fine for a local try-out only.",
              file=sys.stderr)
    deps = Deps.from_settings(settings)
    app = build_app(deps, allowed_hosts=[*args.allowed_host, *LOOPBACK] if args.allowed_host else None,
                    secure_cookies=public)  # behind a TLS proxy the server itself sees plain http
    print(f"Reviewer page: http://{args.host}:{args.port}/   (reader: {settings.extractor})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
