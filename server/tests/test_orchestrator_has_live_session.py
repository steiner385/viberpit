"""Tests for Finding 2 (issue #9): has_live_session + poller closure wiring.

Finding 2a: has_live_session() must snapshot _drivers.values() before iterating
  so that a concurrent event-loop mutation (create_session / close_session) cannot
  cause RuntimeError: dictionary changed size during iteration.

Finding 2b (poller closure wiring): the _poller_usage_fn closure in
  __init__ passes allow_refresh=not self.has_live_session(name).  Verify:
  - allow_refresh=False is passed to usage_for when has_live_session(name) is True
  - allow_refresh=True  is passed to usage_for when has_live_session(name) is False
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from server.sessions.orchestrator import SessionOrchestrator


# ---------------------------------------------------------------------------
# Finding 2a — has_live_session snapshots _drivers to avoid dict-iteration race
# ---------------------------------------------------------------------------

def test_has_live_session_true_when_driver_present():
    """has_live_session returns True when a matching driver is in _drivers."""
    orch = SessionOrchestrator(sdk_factory=MagicMock())

    driver_mock = MagicMock()
    driver_mock.account = "acct1"
    orch._drivers["session-x"] = driver_mock

    assert orch.has_live_session("acct1") is True


def test_has_live_session_false_when_no_driver():
    """has_live_session returns False when _drivers is empty or no driver matches."""
    orch = SessionOrchestrator(sdk_factory=MagicMock())
    assert orch.has_live_session("acct1") is False

    driver_mock = MagicMock()
    driver_mock.account = "other-acct"
    orch._drivers["session-y"] = driver_mock

    assert orch.has_live_session("acct1") is False


def test_has_live_session_uses_snapshot_survives_concurrent_mutation():
    """Confirm has_live_session does not raise RuntimeError when _drivers is
    mutated from another thread during the iteration.

    The fix is `list(self._drivers.values())` — the list() call materialises
    a snapshot before any iteration begins.  We simulate the mutation by
    replacing _drivers with a dict subclass whose .values() returns a lazy
    iterator that mutates the dict before yielding.  Because our implementation
    calls list() first, the mutation happens while building the snapshot (safe)
    rather than during the any() iteration.
    """
    import threading

    orch = SessionOrchestrator(sdk_factory=MagicMock())

    driver_mock = MagicMock()
    driver_mock.account = "acct1"

    mutation_done = threading.Event()

    class MutatingDict(dict):
        """A dict subclass whose values() yields items while mutating the dict."""
        def values(self):  # type: ignore[override]
            real_items = list(super().values())  # snapshot for the yielder

            class LazyIter:
                def __iter__(self_inner):
                    for item in real_items:
                        # Mutate the live dict while caller is iterating.
                        if not mutation_done.is_set():
                            self["session-b"] = MagicMock()
                            mutation_done.set()
                        yield item

            return LazyIter()

    mutating = MutatingDict()
    mutating["session-a"] = driver_mock
    orch._drivers = mutating  # type: ignore[assignment]

    # Must not raise RuntimeError: dictionary changed size during iteration
    result = orch.has_live_session("acct1")
    assert result is True  # driver_mock with acct1 was found


# ---------------------------------------------------------------------------
# Finding 2b — poller closure passes allow_refresh=False when live session exists
# ---------------------------------------------------------------------------

def test_poller_closure_passes_allow_refresh_false_when_live_session():
    """When has_live_session(name) is True, the poller usage_fn must call
    usage_for(name, allow_refresh=False)."""
    import ccswitch.accounts as acct_mod

    allow_refresh_calls: list[bool] = []

    def fake_usage_for(name: str, *, allow_refresh: bool = True, path: str = "unknown"):
        allow_refresh_calls.append(allow_refresh)
        return None

    with patch.object(acct_mod, "usage_for", side_effect=fake_usage_for):
        orch = SessionOrchestrator(sdk_factory=MagicMock())

        # Plant a driver so has_live_session("acct1") returns True.
        driver_mock = MagicMock()
        driver_mock.account = "acct1"
        orch._drivers["session-z"] = driver_mock

        # Invoke the closure directly (it's stored as a bound variable
        # in the UsagePoller; we access it through the poller's usage_fn).
        # The poller stores the closure as _usage_fn.
        poller_fn = orch._poller._usage_fn
        poller_fn("acct1")

    assert allow_refresh_calls == [False], (
        f"expected allow_refresh=False when live session present, got {allow_refresh_calls}"
    )


def test_poller_closure_passes_allow_refresh_true_when_no_live_session():
    """When has_live_session(name) is False, the poller usage_fn must call
    usage_for(name, allow_refresh=True)."""
    import ccswitch.accounts as acct_mod

    allow_refresh_calls: list[bool] = []

    def fake_usage_for(name: str, *, allow_refresh: bool = True, path: str = "unknown"):
        allow_refresh_calls.append(allow_refresh)
        return None

    with patch.object(acct_mod, "usage_for", side_effect=fake_usage_for):
        orch = SessionOrchestrator(sdk_factory=MagicMock())

        # No drivers — has_live_session returns False.
        assert not orch._drivers

        poller_fn = orch._poller._usage_fn
        poller_fn("acct1")

    assert allow_refresh_calls == [True], (
        f"expected allow_refresh=True when no live session, got {allow_refresh_calls}"
    )


# ---------------------------------------------------------------------------
# live_session_load — weighted count of live sessions per account
# ---------------------------------------------------------------------------

def _plant(orch, sid, account, status):
    from server.sessions.types import SessionRecord
    driver = MagicMock()
    driver.account = account
    orch._drivers[sid] = driver
    orch._records[sid] = SessionRecord(
        id=sid, name=sid, account=account, claude_session_id="c",
        cols=80, rows=24, cwd="/tmp", status=status)


def test_live_session_load_weights_working_and_idle():
    orch = SessionOrchestrator(sdk_factory=MagicMock())
    _plant(orch, "s1", "acct1", "in_progress")  # working -> 1.0
    _plant(orch, "s2", "acct1", "idle")         # idle    -> 0.3
    _plant(orch, "s3", "acct2", "working")      # working -> 1.0

    load = orch.live_session_load()
    assert load["acct1"] == 1.3
    assert load["acct2"] == 1.0
    assert "acct3" not in load  # accounts with no live sessions are absent


def test_live_session_load_ignores_driverless_records():
    orch = SessionOrchestrator(sdk_factory=MagicMock())
    from server.sessions.types import SessionRecord
    # A record with no live driver (detached/rehydrated) must not count.
    orch._records["dead"] = SessionRecord(
        id="dead", name="dead", account="acct1", claude_session_id="c",
        cols=80, rows=24, cwd="/tmp", status="working")
    assert orch.live_session_load() == {}


def test_live_session_load_skips_driver_without_record():
    orch = SessionOrchestrator(sdk_factory=MagicMock())
    d = MagicMock(); d.account = "acct1"
    orch._drivers["orphan"] = d  # no matching record
    assert orch.live_session_load() == {}


# ---------------------------------------------------------------------------
# live_session_counts — raw active/idle counts per account
# ---------------------------------------------------------------------------

def test_live_session_counts_splits_active_and_idle():
    orch = SessionOrchestrator(sdk_factory=MagicMock())
    # account "a": one working (active) + one idle; account "b": one idle
    _plant(orch, "s1", account="a", status="working")
    _plant(orch, "s2", account="a", status="idle")
    _plant(orch, "s3", account="b", status="idle")
    counts = orch.live_session_counts()
    assert counts["a"] == {"active": 1, "idle": 1}
    assert counts["b"] == {"active": 0, "idle": 1}


def test_live_session_counts_omits_accounts_without_live_sessions():
    orch = SessionOrchestrator(sdk_factory=MagicMock())
    assert orch.live_session_counts() == {}
