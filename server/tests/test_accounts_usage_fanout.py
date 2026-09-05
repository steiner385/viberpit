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


def test_gather_usages_isolates_hang_and_error_and_is_concurrent():
    orch = _FakeOrch({"ok": "ok", "slow": "hang", "bad": "raise"})

    async def go():
        start = time.monotonic()
        result = await accounts_api._gather_usages(
            orch, ["ok", "slow", "bad"], timeout=0.2)
        return result, time.monotonic() - start

    result, elapsed = asyncio.run(go())

    # Healthy account resolves; hung and raising accounts degrade to None.
    assert isinstance(result["ok"], QuotaSnapshot)
    assert result["slow"] is None
    assert result["bad"] is None
    # Concurrent: total ~= the single timeout (0.2s), not 3x. Generous ceiling
    # to stay non-flaky on a loaded CI box, but well under a serial 3x0.2.
    assert elapsed < 0.5


def test_safe_usage_returns_value_on_success():
    orch = _FakeOrch({"ok": "ok"})
    got = asyncio.run(accounts_api._safe_usage(orch, "ok", timeout=1.0))
    assert isinstance(got, QuotaSnapshot)


def test_safe_usage_timeout_returns_none():
    orch = _FakeOrch({"slow": "hang"})
    got = asyncio.run(accounts_api._safe_usage(orch, "slow", timeout=0.05))
    assert got is None
