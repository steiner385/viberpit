"""Finished sessions must not consume runtime capacity (RC3).

Seven finished-but-unarchived sessions occupied a cap of seven on the live
fleet while zero sessions were mid-turn, so 15 queued jobs could never start.
Six of the seven were permanently exempt from auto-archive, so the deadlock
could not self-heal.

Exempt means `complete` ONLY. `done` is not a finished-work signal — see
server/sessions/status.derive_status, which returns it for ANY ended turn — so a
`done` session is a live session between turns that keeps its warm driver and
must still be counted.
"""
from server.db.models import CAPACITY_EXEMPT_STATUSES, INACTIVE_SESSION_STATUSES


def test_complete_does_not_consume_capacity():
    # Work is over: the completion marker was emitted and _persist_status released
    # the claude process. No runtime slot.
    assert "complete" in CAPACITY_EXEMPT_STATUSES


def test_done_still_consumes_capacity():
    """`done` means a TURN ended, not that the session finished.

    It is returned after every ordinary prompt (turns_completed >= 1, no active
    turn), and such a session keeps its driver for the next prompt — a real live
    process holding a real slot. Exempting it made the cap stop being a ceiling.
    """
    assert "done" not in CAPACITY_EXEMPT_STATUSES


def test_queued_and_terminal_statuses_still_exempt():
    # A terminal row holds nothing; a queued row is waiting FOR a slot, not holding one.
    for s in INACTIVE_SESSION_STATUSES | {"queued"}:
        assert s in CAPACITY_EXEMPT_STATUSES, f"{s} lost its exemption"


def test_idle_still_consumes_capacity():
    """An idle session is alive and awaiting input — it holds a real slot."""
    assert "idle" not in CAPACITY_EXEMPT_STATUSES


def test_exempt_set_is_derived_from_the_inactive_set():
    """One derivation, no intermediate set: exempt = inactive + queued + complete."""
    assert CAPACITY_EXEMPT_STATUSES == INACTIVE_SESSION_STATUSES | {"queued", "complete"}


def test_seven_finished_sessions_do_not_block_a_queue(monkeypatch):
    """The live deadlock, reproduced: 3 idle + 3 complete + 1 done vs a cap of 7.

    The count drops 7 → 4 (the 3 complete sessions release their drivers and stop
    occupying slots), which is under the cap, so the queue unblocks. It is 4 and not
    3 because the `done` session STILL COUNTS: it is between turns with a live driver,
    not finished. That one slot is the honest price of a warm session.
    """
    from unittest import mock

    from server.sessions.orchestrator import SessionOrchestrator

    orch = SessionOrchestrator(sdk_factory=mock.MagicMock())
    orch._records = {
        f"s{i}": mock.Mock(status=st)
        for i, st in enumerate(["idle", "idle", "idle", "complete", "complete", "complete", "done"])
    }
    # Before the capacity fix this was 7 and the drain loop broke immediately.
    assert orch._active_session_count() == 4
