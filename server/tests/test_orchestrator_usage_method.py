"""Regression for #2033: SessionOrchestrator must expose its own async
``usage(name)``, since ``capacity_wiring.sources_for`` hands the BARE
orchestrator (not a facade) to ``account_headroom`` — it needs private
attributes (``_max_active_sessions``, ``_backlog_ctx``, ...) no facade exposes.
Before the fix, ``orch.usage`` raised ``AttributeError`` on every call, which
``_safe_usage`` silently swallowed into a stale-ledger fallback.
"""
from __future__ import annotations

import asyncio
from unittest import mock

from ccswitch.quota import anthropic_snapshot
from server.api import accounts as accounts_api
from server.sessions.orchestrator import SessionOrchestrator


def _orch(usage_fn):
    return SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=usage_fn)


def test_orchestrator_usage_delegates_to_poller():
    snap = anthropic_snapshot(10.0, None, 20.0, None)
    orch = _orch(lambda name: snap)

    result = asyncio.run(orch.usage("acct"))

    assert result is snap


def test_safe_usage_fresh_path_succeeds_against_a_real_orchestrator(monkeypatch):
    snap = anthropic_snapshot(10.0, None, 20.0, None)
    orch = _orch(lambda name: snap)
    # Isolate from the real on-disk ledger so the fresh (orch.usage) path is
    # actually exercised instead of short-circuiting on a local cache hit.
    monkeypatch.setattr(accounts_api, "_local_cached_usage", lambda name: None)

    usage, stale_as_of = asyncio.run(accounts_api._safe_usage(orch, "acct", timeout=1.0))

    assert usage is snap
    assert stale_as_of is None
