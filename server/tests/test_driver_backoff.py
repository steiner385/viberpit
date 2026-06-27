"""The driver must expose whether the current turn is waiting on API rate-limit/
backoff rather than genuinely wedged. The SDK can go silent during a 429/529 backoff
without emitting a per-wait progress event; the stall watchdog uses this signal to
extend its timeout so a throttled-but-alive turn is not interrupted as if hung.
See issue #185 (turns wedge under daemon/account load → watchdog kills them)."""
from __future__ import annotations

from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry


def _driver() -> SessionDriver:
    return SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                         sdk_factory=lambda *a, **k: None,
                         registry=PermissionRegistry(), identity="me")


def test_fresh_driver_is_not_in_backoff():
    d = _driver()
    assert d.in_api_backoff is False


def test_retrying_flag_marks_backoff():
    d = _driver()
    d._retrying = True
    assert d.in_api_backoff is True


def test_unexpired_rate_reset_marks_backoff():
    d = _driver()
    d._clock = lambda: 1000.0
    d._last_rate_reset = 1200  # resets in the future → still throttled
    assert d.in_api_backoff is True


def test_expired_rate_reset_is_not_backoff():
    d = _driver()
    d._clock = lambda: 1000.0
    d._last_rate_reset = 900  # reset already passed → no longer throttled
    assert d.in_api_backoff is False
