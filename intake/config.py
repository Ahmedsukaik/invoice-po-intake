"""Settings, roles and the token -> identity table.

Roles
-----
clerk     upload documents, view the queue and documents
reviewer  view, correct fields, re-run checks, approve and reject
admin     view, export approved documents, read the audit log

Nobody has every permission: the person who uploads cannot approve, and the person who
approves cannot export.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from .envfile import load_env_file

Role = Literal["clerk", "reviewer", "admin"]
Permission = Literal["view", "upload", "review", "export", "audit"]

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "clerk": frozenset({"view", "upload"}),
    "reviewer": frozenset({"view", "review"}),
    "admin": frozenset({"view", "export", "audit"}),
}


@dataclass(frozen=True)
class Identity:
    name: str
    role: Role

    def can(self, permission: Permission) -> bool:
        return permission in ROLE_PERMISSIONS[self.role]

    @property
    def label(self) -> str:
        return f"{self.name} ({self.role})"


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenTable:
    """Maps bearer tokens to identities. Only SHA-256 digests are kept in memory."""

    def __init__(self, tokens: dict[str, dict[str, str]]):
        self._by_digest: dict[str, Identity] = {}
        for token, info in tokens.items():
            if len(token) < 16:
                raise ValueError("tokens must be at least 16 characters")
            role = info.get("role")
            if role not in ROLE_PERMISSIONS:
                raise ValueError(f"unknown role {role!r} for token of {info.get('name')!r}")
            name = str(info.get("name") or "").strip()
            if not name:
                raise ValueError("every token needs a name")
            self._by_digest[digest(token)] = Identity(name=name[:60], role=role)  # type: ignore[arg-type]

    def by_digest(self, token_digest: str) -> Identity | None:
        return self._by_digest.get(token_digest)

    def identify_token(self, token: str | None) -> Identity | None:
        if not token or len(token) > 512:
            return None
        return self._by_digest.get(digest(token.strip()))

    def identify(self, authorization_header: str | None) -> Identity | None:
        if not authorization_header:
            return None
        scheme, _, token = authorization_header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return self.identify_token(token)


def _env_number(name: str, default: object, cast: type) -> Any:
    """A number from .env, or a clear message instead of a traceback."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = cast(raw.strip())
    except (InvalidOperation, ValueError):
        raise SystemExit(f"{name} in .env must be a number (it is {raw.strip()[:20]!r}).") from None
    if value < 0:
        raise SystemExit(f"{name} in .env must not be negative.")
    return value


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    if raw.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if raw.strip().lower() in ("0", "false", "no", "off"):
        return False
    raise SystemExit(f"{name} in .env must be true or false.")


@dataclass(frozen=True)
class Policy:
    """The rules the checks apply. Every value can be set in .env."""

    allowed_currencies: tuple[str, ...] = ("GBP", "EUR", "USD")
    tax_rates: tuple[Decimal, ...] = (Decimal("0"), Decimal("5"), Decimal("20"))
    amount_tolerance: Decimal = Decimal("0.02")  # subtotal, tax and total may be off by this much
    line_tolerance: Decimal = Decimal("0.01")    # qty x unit price vs line amount
    po_price_tolerance_pct: Decimal = Decimal("2")
    po_qty_tolerance_pct: Decimal = Decimal("0")
    max_future_days: int = 7      # an invoice dated further ahead than this is suspicious
    max_payment_days: int = 180   # a due date further out than this is unusual
    require_po: bool = True       # an invoice without a PO number is a blocking finding
    duplicate_window_days: int = 10  # same vendor and amount within this many days: possible duplicate
    date_order: str = "dmy"       # how 05/10/2026 is read when a reader meets it: dmy or mdy
    four_eyes: bool = True        # who typed or changed values may not approve that document

    @classmethod
    def from_env(cls) -> "Policy":
        env = os.environ
        d = cls()
        currencies = env.get("ALLOWED_CURRENCIES", "")
        rates = env.get("TAX_RATES", "")
        try:
            tax_rates = tuple(Decimal(p.strip()) for p in rates.split(",") if p.strip()) if rates.strip() else d.tax_rates
        except InvalidOperation:
            raise SystemExit("TAX_RATES in .env must be numbers separated by commas, such as 0,5,20.") from None
        order = env.get("DATE_ORDER", d.date_order).strip().lower()
        if order not in ("dmy", "mdy"):
            raise SystemExit("DATE_ORDER in .env must be dmy or mdy.")
        return cls(
            allowed_currencies=tuple(c.strip().upper() for c in currencies.split(",") if c.strip())
            or d.allowed_currencies,
            tax_rates=tax_rates,
            amount_tolerance=_env_number("AMOUNT_TOLERANCE", d.amount_tolerance, Decimal),
            line_tolerance=_env_number("LINE_TOLERANCE", d.line_tolerance, Decimal),
            po_price_tolerance_pct=_env_number("PO_PRICE_TOLERANCE_PCT", d.po_price_tolerance_pct, Decimal),
            po_qty_tolerance_pct=_env_number("PO_QTY_TOLERANCE_PCT", d.po_qty_tolerance_pct, Decimal),
            max_future_days=_env_number("MAX_FUTURE_DAYS", d.max_future_days, int),
            max_payment_days=_env_number("MAX_PAYMENT_DAYS", d.max_payment_days, int),
            require_po=_env_flag("REQUIRE_PO", d.require_po),
            duplicate_window_days=_env_number("DUPLICATE_WINDOW_DAYS", d.duplicate_window_days, int),
            date_order=order,
            four_eyes=_env_flag("FOUR_EYES", d.four_eyes),
        )


@dataclass(frozen=True)
class Settings:
    db_path: str
    storage_dir: Path
    inbox_dir: Path
    outbox_dir: Path
    seed_dir: Path
    tokens: TokenTable
    extractor: str = "rules"
    max_upload_bytes: int = 10 * 1024 * 1024
    max_pages: int = 50
    pdf_timeout: float = 20.0
    pdf_workers: int = 3
    pdf_wait: float = 30.0
    policy: Policy = field(default_factory=Policy)
    uses_placeholders: bool = False  # still running on the change-me tokens from .env.example

    @classmethod
    def from_env(cls) -> "Settings":
        load_env_file()
        if not os.environ.get("INTAKE_TOKENS"):
            raise SystemExit("Missing setting INTAKE_TOKENS. Copy .env.example to .env and edit it.")
        try:
            tokens = TokenTable(json.loads(os.environ["INTAKE_TOKENS"]))
        except (ValueError, TypeError, AttributeError) as exc:
            raise SystemExit(f"INTAKE_TOKENS is not valid: {exc}") from None
        extractor = os.environ.get("INTAKE_EXTRACTOR", "rules").strip().lower()
        if extractor not in ("rules", "claude"):
            raise SystemExit("INTAKE_EXTRACTOR must be 'rules' or 'claude'.")
        root = Path(os.environ.get("INTAKE_HOME", "."))
        return cls(
            db_path=os.environ.get("INTAKE_DB", str(root / "intake.db")),
            storage_dir=Path(os.environ.get("INTAKE_STORAGE", root / "storage")),
            inbox_dir=Path(os.environ.get("INTAKE_INBOX", root / "inbox")),
            outbox_dir=Path(os.environ.get("INTAKE_OUTBOX", root / "outbox")),
            seed_dir=Path(os.environ.get("INTAKE_SEED", Path(__file__).resolve().parent.parent / "seed")),
            tokens=tokens,
            extractor=extractor,
            max_upload_bytes=_env_number("MAX_UPLOAD_MB", 10, int) * 1024 * 1024,
            max_pages=_env_number("MAX_PAGES", 50, int),
            pdf_timeout=_env_number("PDF_TIMEOUT_SECONDS", 20.0, float),
            pdf_workers=max(1, min(_env_number("PDF_WORKERS", 3, int), 16)),
            pdf_wait=_env_number("PDF_WAIT_SECONDS", 30.0, float),
            policy=Policy.from_env(),
            uses_placeholders="change-me" in os.environ.get("INTAKE_TOKENS", ""),
        )
