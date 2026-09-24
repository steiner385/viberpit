"""GET /api/accounts usage fan-out isolation (incident 2026-07-02).

A single account whose usage fetch hangs or raises must NOT stall the other
accounts or turn the whole endpoint into a 500. It degrades to usage=None.

Root cause of the incident: get_accounts awaited each account's usage RPC
serially with no timeout budget, so a wedged daemon's ReadTimeout bubbled up as
an unhandled 500 — and, being serial, one slow account blocked all the others.
"""
from __future__ import annotations

import asyncio
import time
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from datetime import datetime, timezone

from ccswitch.quota import QuotaSnapshot, anthropic_snapshot

NOW = datetime(2026, 9, 5, tzinfo=timezone.utc)


from server.api import accounts as accounts_api


def _fake_usage(tag: str) -> QuotaSnapshot:
    """A real snapshot — the fan-out contract is about the TYPE that reaches
    ``_usage_to_dict``, so a stand-in class would let a shape mismatch through."""
    return anthropic_snapshot(1.0, None, 2.0, None, fetched_at=NOW)


class _FakeOrch:
    """Minimal orch whose per-account usage() behaviour is scripted by name."""

    def __init__(self, behaviour: dict) -> None:
        self._behaviour = behaviour

    async def usage(self, name: str):
        b = self._behaviour[name]
        if b == "hang":
            await asyncio.sleep(1000)  # never returns within the timeout
        if b == "raise":
            raise RuntimeError("boom")
        return _fake_usage(name)


def test_gather_usages_isolates_hang_and_error_and_is_concurrent(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path))
    orch = _FakeOrch({"ok": "ok", "slow": "hang", "bad": "raise"})

    async def go():
        start = time.monotonic()
        result = await accounts_api._gather_usages(
            orch, ["ok", "slow", "bad"], timeout=0.2)
        return result, time.monotonic() - start

    result, elapsed = asyncio.run(go())

    # Healthy account resolves; hung and raising accounts degrade to (None,
    # None) — no ledger reading exists for them here, so there is nothing to
    # serve stale either.
    assert isinstance(result["ok"][0], QuotaSnapshot)
    assert result["ok"][1] is None
    assert result["slow"] == (None, None)
    assert result["bad"] == (None, None)
    # Concurrent: total ~= the single timeout (0.2s), not 3x. Generous ceiling
    # to stay non-flaky on a loaded CI box, but well under a serial 3x0.2.
    assert elapsed < 0.5


def test_safe_usage_returns_value_on_success():
    orch = _FakeOrch({"ok": "ok"})
    got, stale_as_of = asyncio.run(accounts_api._safe_usage(orch, "ok", timeout=1.0))
    assert isinstance(got, QuotaSnapshot)
    assert stale_as_of is None


def test_safe_usage_timeout_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path))
    orch = _FakeOrch({"slow": "hang"})
    got, stale_as_of = asyncio.run(accounts_api._safe_usage(orch, "slow", timeout=0.05))
    assert got is None
    assert stale_as_of is None


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["get", "refresh", "update", "enabled"])
async def test_account_routes_read_accounts_off_event_loop(monkeypatch, route):
    loop_thread = threading.get_ident()
    account = {"name": "test", "nickname": "Personal", "enabled": True}

    def accounts():
        assert threading.get_ident() != loop_thread
        return [account]

    monkeypatch.setattr(accounts_api, "list_accounts", accounts)
    monkeypatch.setattr(accounts_api, "_check_name_and_slot", lambda name: None)
    monkeypatch.setattr(accounts_api, "_provider_or_none", lambda name: None)
    monkeypatch.setattr(accounts_api, "set_account_enabled", lambda *args: None)
    monkeypatch.setattr(accounts_api, "record_audit", AsyncMock())
    monkeypatch.setattr(accounts_api, "_codex_details", AsyncMock(return_value={}))
    monkeypatch.setattr(accounts_api, "_safe_usage", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(accounts_api, "_gather_usages", AsyncMock(return_value={"test": (None, None)}))
    monkeypatch.setattr(accounts_api, "load_config", lambda: None)
    monkeypatch.setattr(accounts_api, "rank_accounts", lambda *args, **kwargs: ([], None))
    monkeypatch.setattr(accounts_api, "_account_response_from_info", lambda acct, *args, **kwargs: acct)
    orch = SimpleNamespace(
        live_session_counts=AsyncMock(return_value={}),
        live_session_load=AsyncMock(return_value={}),
    )
    monkeypatch.setattr(accounts_api, "_get_orchestrator", lambda request: orch)
    request = SimpleNamespace()
    if route == "get":
        assert await accounts_api.get_accounts(request) == [account]
    elif route == "refresh":
        assert await accounts_api.refresh_account("test", request) == account
    elif route == "update":
        assert await accounts_api.update_account("test", accounts_api.AccountPatchBody()) == account
    else:
        assert await accounts_api.set_enabled(
            "test", accounts_api.AccountEnabledBody(enabled=True), request,
            identity="tester", db=None,
        ) == account
