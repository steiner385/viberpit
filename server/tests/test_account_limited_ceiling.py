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

from ccswitch.quota import (QuotaWindow, SCOPE_MODEL, anthropic_snapshot,
                            copilot_snapshot, unknown_snapshot)
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


def test_backlog_probe_picks_for_the_inherited_fleet_model(orch, monkeypatch):
    """The probe has no per-dispatch model, so it must ask for the model a Default
    launch would actually inherit — not for "every window", which would pause the
    backlog on an account exhausted only for a tier nothing is launching."""
    from server.sessions.engine import AgentEngine
    from server.sessions.model_policy import quota_model
    seen = {}

    def _select(**kw):
        seen.update(kw)
        return Picked(name="work", optimistic=False)

    monkeypatch.setattr(orch_mod, "select_account", _select)
    orch.live_session_load = lambda: {}
    orch._recovery.quarantined = lambda now=None: set()
    _cache(orch, work=anthropic_snapshot(20.0, None, 30.0, None))

    assert asyncio.run(orch._backlog_healthy_account(engine=AgentEngine.CLAUDE_CODE)) == "work"
    assert seen["model"] == quota_model(None, engine=AgentEngine.CLAUDE_CODE)
    assert seen["reserve"] is False  # still a read-only probe


def test_backlog_gate_reads_the_same_fleet_model_the_pick_used(orch, monkeypatch):
    """The pick and the ceiling gate must judge the SAME model.

    The probe picked for ``quota_model(None)`` and then gated on every window, so on
    the live 2026-09-14 shape (Fable weekly 100%, weekly 91%) it selected an account
    the selector had just called usable and immediately threw it away — pausing the
    backlog for a tier nothing was launching. ``ANTHROPIC_MODEL`` is pinned here
    because it is the first source ``inherited_default_model`` reads, which makes the
    fleet default deterministic instead of "whatever /model last wrote".
    """
    from server.sessions.engine import AgentEngine
    seen = {}

    def _select(**kw):
        seen.update(kw)
        return Picked(name="work", optimistic=False)

    monkeypatch.setattr(orch_mod, "select_account", _select)
    orch.live_session_load = lambda: {}
    orch._recovery.quarantined = lambda now=None: set()
    fable = QuotaWindow(key="weekly:fable", label="Fable weekly", used_pct=100.0,
                        scope=SCOPE_MODEL, applies_to_models=("fable",))
    _cache(orch, work=anthropic_snapshot(3.0, None, 91.0, None, model_windows=[fable]))

    monkeypatch.setenv("ANTHROPIC_MODEL", "sonnet")
    assert asyncio.run(orch._backlog_healthy_account(engine=AgentEngine.CLAUDE_CODE)) == "work"
    assert seen["model"] == "sonnet"

    # Same account, same snapshot — the fleet default is what changed.
    monkeypatch.setenv("ANTHROPIC_MODEL", "fable")
    assert asyncio.run(orch._backlog_healthy_account(engine=AgentEngine.CLAUDE_CODE)) is None
    assert seen["model"] == "fable"


def test_backlog_returns_none_when_selection_fails(orch, monkeypatch):
    def _busy(**kw):
        raise AllAccountsBusy(None)

    monkeypatch.setattr(orch_mod, "select_account", _busy)
    orch.live_session_load = lambda: {}
    orch._recovery.quarantined = lambda now=None: set()
    assert asyncio.run(orch._backlog_healthy_account()) is None


def test_backlog_healthy_account_selects_off_event_loop(orch, monkeypatch):
    """select_account does blocking oauth usage I/O; it must never run on the
    daemon's event loop thread. Confirmed live on dobby (#2223): the backlog
    tick's synchronous select_account call starved the FastAPI /info handler
    (the fleet health prober polls it every 30s) for 12-38s at a time."""
    import threading
    loop_thread = threading.get_ident()
    seen_threads = []

    def _select(**kw):
        seen_threads.append(threading.get_ident())
        return Picked(name="work", optimistic=False)

    monkeypatch.setattr(orch_mod, "select_account", _select)
    orch.live_session_load = lambda: {}
    orch._recovery.quarantined = lambda now=None: set()
    _cache(orch, work=anthropic_snapshot(20.0, None, 30.0, None))
    assert asyncio.run(orch._backlog_healthy_account()) == "work"
    assert seen_threads and seen_threads[0] != loop_thread
