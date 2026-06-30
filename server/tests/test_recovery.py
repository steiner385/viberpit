"""Tests for recovery state tracking and dropped-turn resend guarding."""
from server.sessions.recovery import RecoveryState, RESEND_CAP, RESEND_WINDOW


def test_resend_count_within_and_outside_window():
    rs = RecoveryState()
    assert rs.resend_count("s1", now=1000.0) == 0
    rs.record_resend("s1", now=1000.0)
    rs.record_resend("s1", now=1001.0)
    assert rs.resend_count("s1", now=1002.0) == 2
    # Entries older than RESEND_WINDOW (120s) are pruned (exclusive boundary).
    # At now=1121.0, cutoff=1001.0, so entries at 1000.0 and exactly 1001.0 are pruned.
    assert rs.resend_count("s1", now=1121.0) == 0
    # Test that an entry slightly newer than the window edge survives.
    rs2 = RecoveryState()
    rs2.record_resend("s1", now=1001.1)
    assert rs2.resend_count("s1", now=1121.0) == 1


def test_resend_cap_reached_uses_count():
    rs = RecoveryState()
    for t in range(RESEND_CAP):
        rs.record_resend("s1", now=1000.0 + t)
    assert rs.resend_count("s1", now=1000.0 + RESEND_CAP) == RESEND_CAP
    assert rs.resend_cap_reached("s1", now=1000.0 + RESEND_CAP) is True


def test_resend_count_boundary_is_exclusive():
    rs = RecoveryState()
    rs.record_resend("s1", now=1000.0)
    # An entry exactly RESEND_WINDOW old is at the cutoff and is pruned (exclusive).
    assert rs.resend_count("s1", now=1000.0 + RESEND_WINDOW) == 0
