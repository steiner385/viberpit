"""Finished sessions must not consume runtime capacity (RC3).

Seven finished-but-unarchived sessions occupied a cap of seven on the live
fleet while zero sessions were mid-turn, so 15 queued jobs could never start.
Six of the seven were permanently exempt from auto-archive, so the deadlock
could not self-heal.
"""
from server.db.models import (
    CAPACITY_EXEMPT_STATUSES,
    INACTIVE_SESSION_STATUSES,
    NON_ACTIVE_SESSION_STATUSES,
)


def test_finished_statuses_do_not_consume_capacity():
    # Work is over: the completion marker was emitted. No runtime slot.
    assert "complete" in CAPACITY_EXEMPT_STATUSES
    assert "done" in CAPACITY_EXEMPT_STATUSES


def test_queued_and_terminal_statuses_still_exempt():
    # Everything that was already exempt stays exempt.
    for s in NON_ACTIVE_SESSION_STATUSES:
        assert s in CAPACITY_EXEMPT_STATUSES, f"{s} lost its exemption"


def test_idle_still_consumes_capacity():
    """An idle session is alive and awaiting input — it holds a real slot."""
    assert "idle" not in CAPACITY_EXEMPT_STATUSES


def test_non_active_set_is_unchanged_for_other_consumers():
    """NON_ACTIVE_SESSION_STATUSES has other callers; do not repurpose it."""
    assert NON_ACTIVE_SESSION_STATUSES == INACTIVE_SESSION_STATUSES | {"queued"}


def test_seven_finished_sessions_do_not_block_a_queue(monkeypatch):
    """The live deadlock, reproduced: 3 idle + 3 complete + 1 done vs a cap of 7."""
    from server.sessions.orchestrator import SessionOrchestrator
    from unittest import mock

    orch = SessionOrchestrator(sdk_factory=mock.MagicMock())
    orch._records = {
        f"s{i}": mock.Mock(status=st)
        for i, st in enumerate(["idle", "idle", "idle", "complete", "complete", "complete", "done"])
    }
    # Before the fix this was 7 and the drain loop broke immediately.
    assert orch._active_session_count() == 3
