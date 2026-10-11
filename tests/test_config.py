"""Settings, tokens, and the guard against exposing a try-out setup."""
import json
import os
import shutil
import time
from decimal import Decimal

import pytest
from conftest import SAMPLES

from intake.__main__ import main as run_server
from intake.config import Policy, Settings, TokenTable
from intake.pipeline import scan_inbox
from intake.server import build_app

GOOD = {"clerk-token-0000000001": {"name": "tom", "role": "clerk"}}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no stray .env
    for key in list(os.environ):
        if key.startswith(("INTAKE_", "ALLOWED_", "TAX_", "PO_", "MAX_", "AMOUNT_", "LINE_")):
            monkeypatch.delenv(key)
    return monkeypatch


def test_tokens_must_be_long_named_and_have_a_known_role():
    for bad in ({"short": {"name": "x", "role": "clerk"}}, {"t" * 20: {"name": "x", "role": "superuser"}},
                {"t" * 20: {"name": " ", "role": "admin"}}):
        with pytest.raises(ValueError):
            TokenTable(bad)
    table = TokenTable(GOOD)
    assert table.identify("Bearer clerk-token-0000000001").name == "tom"
    assert table.identify("bearer  clerk-token-0000000001 ") is not None
    assert table.identify("Bearer clerk-token-0000000002") is None


def test_settings_refuse_missing_or_broken_tokens(env):
    with pytest.raises(SystemExit, match="INTAKE_TOKENS"):
        Settings.from_env()
    env.setenv("INTAKE_TOKENS", "{not json")
    with pytest.raises(SystemExit, match="not valid"):
        Settings.from_env()
    env.setenv("INTAKE_TOKENS", json.dumps(GOOD))
    env.setenv("INTAKE_EXTRACTOR", "magic")
    with pytest.raises(SystemExit, match="rules"):
        Settings.from_env()


def test_policy_comes_from_the_environment(env):
    env.setenv("INTAKE_TOKENS", json.dumps(GOOD))
    env.setenv("ALLOWED_CURRENCIES", "eur, chf")
    env.setenv("TAX_RATES", "0,7.7,8.1")
    env.setenv("PO_PRICE_TOLERANCE_PCT", "5")
    policy = Settings.from_env().policy
    assert policy.allowed_currencies == ("EUR", "CHF") and Decimal("7.7") in policy.tax_rates
    assert policy.po_price_tolerance_pct == Decimal("5") and policy.max_future_days == Policy().max_future_days


def test_the_server_will_not_listen_publicly_with_placeholder_tokens(env):
    env.setenv("INTAKE_TOKENS", json.dumps({"change-me-clerk-token-000000": {"name": "tom", "role": "clerk"}}))
    with pytest.raises(SystemExit, match="change-me"):
        run_server(["--host", "0.0.0.0", "--port", "1"])
    env.setenv("INTAKE_TOKENS", json.dumps(GOOD))
    with pytest.raises(SystemExit, match="allowed-host"):
        run_server(["--host", "0.0.0.0", "--port", "1"])


def test_watch_mode_leaves_files_that_are_still_being_copied(deps):
    target = deps.inbox_dir / "arriving.pdf"
    shutil.copy(SAMPLES / "01_clean_invoice.pdf", target)
    os.utime(target, (time.time(), time.time()))
    assert scan_inbox(deps, min_age_seconds=60) == [] and target.exists()
    assert scan_inbox(deps)[0].status == "needs_review" and not target.exists()


def test_a_public_server_answers_only_to_its_own_hostname(deps):
    from fastapi.testclient import TestClient

    app = build_app(deps, allowed_hosts=["intake.example.com", "127.0.0.1"])
    with TestClient(app, base_url="http://intake.example.com") as ok:
        assert ok.get("/healthz").status_code == 200
    with TestClient(app, base_url="http://evil.example.net") as other:
        assert other.get("/healthz").status_code == 400 and other.get("/login").status_code == 400


def test_a_bad_value_in_env_gives_a_clear_message(env):
    env.setenv("INTAKE_TOKENS", json.dumps(GOOD))
    env.setenv("AMOUNT_TOLERANCE", "two pence")
    with pytest.raises(SystemExit, match="AMOUNT_TOLERANCE in .env must be a number"):
        Settings.from_env()
    env.setenv("AMOUNT_TOLERANCE", "0.02")
    env.setenv("REQUIRE_PO", "maybe")
    with pytest.raises(SystemExit, match="REQUIRE_PO"):
        Settings.from_env()
