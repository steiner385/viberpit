"""Unit tests for server.sessions.recovery.RecoveryState (quarantine + cap)."""
from server.sessions.recovery import RecoveryState


def test_quarantined_account_excluded_until_cooldown_expires():
    s = RecoveryState()
    s.quarantine("kindash", until=100.0)
    assert s.quarantined(now=50.0) == frozenset({"kindash"})
    # at/after expiry it is no longer quarantined
    assert s.quarantined(now=100.0) == frozenset()
    assert s.quarantined(now=101.0) == frozenset()


def test_multiple_quarantines_and_pruning():
    s = RecoveryState()
    s.quarantine("a", until=10.0)
    s.quarantine("b", until=30.0)
    assert s.quarantined(now=5.0) == frozenset({"a", "b"})
    assert s.quarantined(now=20.0) == frozenset({"b"})


def test_requarantine_extends_window():
    s = RecoveryState()
    s.quarantine("a", until=10.0)
    s.quarantine("a", until=50.0)
    assert s.quarantined(now=30.0) == frozenset({"a"})


def test_rotation_cap_reached_after_max_within_window():
    s = RecoveryState(max_rotations=3, window=300.0)
    for t in (0.0, 10.0, 20.0):
        assert not s.rotation_cap_reached("sess", now=t)
        s.record_rotation("sess", now=t)
    # 4th within window trips the cap
    assert s.rotation_cap_reached("sess", now=30.0)


def test_rotation_cap_resets_outside_window():
    s = RecoveryState(max_rotations=2, window=100.0)
    s.record_rotation("sess", now=0.0)
    s.record_rotation("sess", now=10.0)
    assert s.rotation_cap_reached("sess", now=50.0)
    # old rotations age out of the window
    assert not s.rotation_cap_reached("sess", now=200.0)


def test_rotation_cap_is_per_session():
    s = RecoveryState(max_rotations=1, window=100.0)
    s.record_rotation("a", now=0.0)
    assert s.rotation_cap_reached("a", now=10.0)
    assert not s.rotation_cap_reached("b", now=10.0)


def test_crash_cap_reached_after_max_within_window():
    s = RecoveryState()
    for t in (0.0, 10.0, 20.0):
        assert not s.crash_cap_reached("sess", now=t)
        s.record_crash("sess", now=t)
    assert s.crash_cap_reached("sess", now=30.0)


def test_crash_cap_resets_outside_window():
    s = RecoveryState()
    s.record_crash("sess", now=0.0)
    s.record_crash("sess", now=10.0)
    s.record_crash("sess", now=20.0)
    assert s.crash_cap_reached("sess", now=30.0)
    assert not s.crash_cap_reached("sess", now=200.0)


def test_stall_cap_reached_after_max_within_window():
    s = RecoveryState()
    for t in (0.0, 10.0, 20.0):
        assert not s.stall_cap_reached("sess", now=t)
        s.record_stall("sess", now=t)
    assert s.stall_cap_reached("sess", now=30.0)


def test_stall_and_crash_caps_are_independent():
    s = RecoveryState()
    for _ in range(3):
        s.record_stall("sess", now=1000.0)
    assert s.stall_cap_reached("sess", now=1000.0)
    assert not s.crash_cap_reached("sess", now=1000.0)   # stalls don't debit crash budget
