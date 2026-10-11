"""Everything the app needs, in one object, so tests and the demo can build it with throwaway paths."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from .config import Policy, Settings, TokenTable
from .export import CsvJsonExporter, Exporter
from .extract import Extractor, make_extractor
from .store import DatabaseVersionError, Store


@dataclass
class Deps:
    store: Store
    tokens: TokenTable
    extractor: Extractor
    storage_dir: Path
    inbox_dir: Path
    outbox_dir: Path
    policy: Policy = field(default_factory=Policy)
    exporter: Exporter = field(default_factory=CsvJsonExporter)
    today: Callable[[], date] = date.today
    max_upload_bytes: int = 10 * 1024 * 1024
    max_pages: int = 50
    pdf_timeout: float = 20.0  # seconds a PDF may take to parse before it goes to a person
    pdf_workers: int = 3       # PDFs parsed at the same time, each in its own worker process
    pdf_wait: float = 30.0     # seconds a PDF may wait for a free worker before it goes to a person

    def __post_init__(self) -> None:
        for folder in (self.storage_dir, self.inbox_dir, self.outbox_dir):
            Path(folder).mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_settings(cls, s: Settings) -> "Deps":
        try:
            store = Store(s.db_path)
        except DatabaseVersionError as exc:
            raise SystemExit(str(exc)) from None
        if store.upgraded_from is not None:
            print(f"Upgraded the database {s.db_path} from version {store.upgraded_from}.", flush=True)
        store.load_seed(s.seed_dir)
        extractor = make_extractor(s.extractor)
        check = getattr(extractor, "check_ready", None)
        if check:
            check()  # Claude reader: wrong key or model name stops the start-up with a clear message
        return cls(
            store=store, tokens=s.tokens, extractor=extractor, storage_dir=s.storage_dir,
            inbox_dir=s.inbox_dir, outbox_dir=s.outbox_dir, policy=s.policy, max_upload_bytes=s.max_upload_bytes,
            max_pages=s.max_pages, pdf_timeout=s.pdf_timeout, pdf_workers=s.pdf_workers, pdf_wait=s.pdf_wait,
        )

    def pdf_path(self, sha256: str) -> Path:
        return Path(self.storage_dir) / f"{sha256}.pdf"
