"""A slow account must not file one auto-error issue per account, forever.

The sibling of the ``host_probe`` flood (#1507/#1669/#1724/#1676), found by
sweeping the open ``auto-error`` issues for the same shape. ``GET /api/accounts``
is re-requested by every open dashboard on a refetch interval, so an account
whose usage RPC keeps losing the deadline warns on every request — and because
the account name used to ride the message, ``server.obs.errsink`` fingerprinted
one issue PER ACCOUNT: #2042 (116 occurrences), #2043 (50), #2041 (30), #2044
(28). ``log_event_throttled`` fingerprints on the ``account.usage_fetch_failed``
event name instead, with the account riding ``fields`` (#2126).

Same shape, same source of truth: ``server.obs.logdedupe``, keyed per account
and per reason. The fallback behaviour is untouched — only how often the
degradation announces itself.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from server.api import accounts as accounts_api


class _FailingOrch:
    """Its usage() always fails, the way a wedged daemon's does."""

    def __init__(self, behaviour: str = "hang") -> None:
        self.behaviour = behaviour

    async def usage(self, name: str):
        if self.behaviour == "hang":
            await asyncio.sleep(1000)
        raise RuntimeError("daemon said no")


#: What ``_stale_fallback`` returns here — a sentinel, not ``(None, None)``, so the
#: "throttling changes nothing but the volume" test asserts the real value flowed
#: through rather than a shape the failure path produces anyway.
STALE = ("stale-reading", "2026-09-20T00:00:00Z")


@pytest.fixture(autouse=True)
def _no_ledger_and_fresh_throttle(monkeypatch):
    """Both ledger seams stubbed so the RPC is always attempted and the fallback is
    identifiable; the throttle starts every test with no window open."""
    monkeypatch.setattr(accounts_api, "_local_cached_usage", lambda name: None)
    monkeypatch.setattr(accounts_api, "_stale_fallback", lambda name: STALE)
    accounts_api._USAGE_FALLBACK_LOG.clear()
    yield
    accounts_api._USAGE_FALLBACK_LOG.clear()


def _fallback_records(caplog):
    return [r for r in caplog.records if r.getMessage() == "account.usage_fetch_failed"]


def test_one_accounts_repeated_usage_timeout_warns_once_and_demotes_the_repeats(caplog):
    orch = _FailingOrch("hang")

    async def run():
        for _ in range(4):
            await accounts_api._safe_usage(orch, "tony_cairnea.com", timeout=0.01)

    with caplog.at_level(logging.DEBUG, logger=accounts_api.log.name):
        asyncio.run(run())

    records = _fallback_records(caplog)
    assert len(records) == 4, "every degradation is still written somewhere"
    assert [r.levelno for r in records] == [logging.WARNING] + [logging.DEBUG] * 3


def test_a_second_accounts_failure_is_not_suppressed_by_the_first(caplog):
    """The key is per account: #2041-#2044 are four accounts, not one condition."""
    orch = _FailingOrch("hang")

    async def run():
        await accounts_api._safe_usage(orch, "tony_cairnea.com", timeout=0.01)
        await accounts_api._safe_usage(orch, "tony_kindash.com", timeout=0.01)

    with caplog.at_level(logging.DEBUG, logger=accounts_api.log.name):
        asyncio.run(run())

    warnings = [r for r in _fallback_records(caplog) if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    accounts = [r.fields["account"] for r in warnings]
    assert "tony_cairnea.com" in accounts
    assert "tony_kindash.com" in accounts


def test_a_raising_fetch_reports_even_while_a_timeouts_window_is_open(caplog):
    """A timeout (the daemon is slow) and a raise (the daemon refused) are
    different conditions with different fixes, so they carry different keys."""

    async def run():
        await accounts_api._safe_usage(_FailingOrch("hang"), "acct", timeout=0.01)
        await accounts_api._safe_usage(_FailingOrch("raise"), "acct", timeout=0.01)

    with caplog.at_level(logging.DEBUG, logger=accounts_api.log.name):
        asyncio.run(run())

    warnings = [r for r in _fallback_records(caplog) if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert warnings[0].fields["reason"] == "timeout"
    assert warnings[1].fields["reason"] == "error"


def test_throttling_does_not_change_what_safe_usage_returns(caplog):
    """The degradation path is untouched — only how loudly it announces itself."""
    orch = _FailingOrch("hang")

    async def run():
        return [await accounts_api._safe_usage(orch, "acct", timeout=0.01)
                for _ in range(3)]

    with caplog.at_level(logging.DEBUG, logger=accounts_api.log.name):
        results = asyncio.run(run())

    assert results == [STALE] * 3
