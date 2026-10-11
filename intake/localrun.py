"""Running the app locally in one process: used by demo.py and the tests."""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn

from .config import TokenTable
from .deps import Deps
from .extract import Extractor, RuleBasedExtractor
from .store import Store

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app: Any, port: int) -> uvicorn.Server:
    """Start the app on 127.0.0.1:port in a background thread and wait until it listens."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("server did not start")


def local_deps(workdir: Path, tokens: dict[str, dict[str, str]], extractor: Extractor | None = None) -> Deps:
    """Deps with a fresh database and folders under workdir, and the seed reference data."""
    store = Store(str(workdir / "intake.db"))
    store.load_seed(ROOT / "seed")
    return Deps(store=store, tokens=TokenTable(tokens), extractor=extractor or RuleBasedExtractor(),
                storage_dir=workdir / "storage", inbox_dir=workdir / "inbox", outbox_dir=workdir / "outbox")
