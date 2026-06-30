"""Tests for recovery state tracking and dropped-turn resend guarding."""
from server.sessions.recovery import RecoveryState, RESEND_CAP


def test_resend_count_within_and_outside_window():
    rs = RecoveryState()
    assert rs.resend_count("s1", now=1000.0) == 0
    rs.record_resend("s1", now=1000.0)
    rs.record_resend("s1", now=1001.0)
    assert rs.resend_count("s1", now=1002.0) == 2
    # An entry older than RESEND_WINDOW (120s) is pruned.
    assert rs.resend_count("s1", now=1000.0 + 121.0) == 1


def test_resend_cap_reached_uses_count():
    rs = RecoveryState()
    for t in range(RESEND_CAP):
        rs.record_resend("s1", now=1000.0 + t)
    assert rs.resend_count("s1", now=1000.0 + RESEND_CAP) == RESEND_CAP
    assert rs.resend_cap_reached("s1", now=1000.0 + RESEND_CAP) is True
