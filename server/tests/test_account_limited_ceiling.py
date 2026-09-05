"""``_account_limited`` reads the SHARED "no headroom" definition, not session-only.

It used to be ``usage.session_pct >= _ROTATION_CEILING``. That was the outlier:
``selector.at_ceiling`` is the repo's single definition of "this account has no
headroom" and counts EVERY window, because a turn on a weekly-exhausted account
429s exactly like one on a session-exhausted account. The rotation monitor and
crash recovery already used it.

The divergence had teeth: ``_account_limited`` gates
``_backlog_healthy_account``, so an account at weekly 100% / session 20% read as
healthy and the backlog kept dispatching sessions whose first turn would
rate-limit. These tests pin the widened behaviour so it stays deliberate.
"""
import asyncio

import pytest

from ccswitch.quota import anthropic_snapshot, copilot_snapshot, unknown_snapshot
from server.sessions import orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator, _ROTATION_CEILING
from server.sessions.selection import AllAccountsBusy, Picked


@pytest.fixture
def orch():
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 1000.0
    return o


def _cache(orch, **usages):
    orch._poller.cached = lambda account: usages.get(account)


def test_weekly_exhausted_session_fine_is_limited(orch):
    """The exact case the old session-only check called healthy."""
    _cache(orch, work=anthropic_snapshot(20.0, None, _ROTATION_CEILING, None))
    assert orch._account_limited("work") is True


def test_session_exhausted_is_still_limited(orch):
    """The case the old check already caught — unchanged."""
    _cache(orch, work=anthropic_snapshot(_ROTATION_CEILING, None, 20.0, None))
    assert orch._account_limited("work") is True


def test_account_with_headroom_on_every_window_is_not_limited(orch):
    _cache(orch, work=anthropic_snapshot(20.0, None, 30.0, None))
    assert orch._account_limited("work") is False


def test_single_window_provider_is_limited_on_its_one_window(orch):
    """Copilot reports ONE window; "no headroom" must not need a second one."""
    _cache(orch, cp=copilot_snapshot(_ROTATION_CEILING))
    assert orch._account_limited("cp") is True
    _cache(orch, cp=copilot_snapshot(10.0))
    assert orch._account_limited("cp") is False


def test_unknown_usage_is_not_limited(orch):
    """Best-effort: no cached usage, or a snapshot that says it does not know,
    both degrade to "not limited" — a doomed title query degrades silently anyway."""
    _cache(orch)  # nothing cached at all
    assert orch._account_limited("work") is False
    _cache(orch, work=unknown_snapshot("anthropic"))
    assert orch._account_limited("work") is False


def test_backlog_pauses_on_a_weekly_exhausted_account(orch, monkeypatch):
    """The consequence the widening fixes: the backlog must stop dispatching onto
    an account whose next turn would rate-limit."""
    monkeypatch.setattr(orch_mod, "select_account",
                        lambda **kw: Picked(name="work", optimistic=False))
    orch.live_session_load = lambda: {}
    orch._recovery.quarantined = lambda now=None: set()

    _cache(orch, work=anthropic_snapshot(20.0, None, _ROTATION_CEILING, None))
    assert asyncio.run(orch._backlog_healthy_account()) is None

    _cache(orch, work=anthropic_snapshot(20.0, None, 30.0, None))
    assert asyncio.run(orch._backlog_healthy_account()) == "work"


def test_backlog_returns_none_when_selection_fails(orch, monkeypatch):
    def _busy(**kw):
        raise AllAccountsBusy(None)

    monkeypatch.setattr(orch_mod, "select_account", _busy)
    orch.live_session_load = lambda: {}
    orch._recovery.quarantined = lambda now=None: set()
    assert asyncio.run(orch._backlog_healthy_account()) is None
