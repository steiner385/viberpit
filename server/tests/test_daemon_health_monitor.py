"""Issue #434: DaemonHealthMonitor tracks daemon restarts as observed from the long-lived
edge process, so a wedge→watchdog-restart is self-evident in the dashboard instead of
requiring `journalctl` archaeology.

Key facts under test:
- The daemon epoch (`/info`) is `<start_ms_hex>-<pid>-<uuid8>`; the first hex segment is the
  process start time in ms, so uptime is derivable from the epoch alone.
- A *change* in epoch between polls is a restart; the first epoch we ever see is NOT a
  restart (the edge just connected).
- Restart timestamps are kept in a bounded window (default 24h) and pruned.
"""
import asyncio

import pytest

from server.sessions.daemon_health import DaemonHealthMonitor, epoch_started_at_ms


@pytest.fixture(autouse=True)
def _isolate_watchdog_file(tmp_path, monkeypatch):
    # Point the watchdog last-restart lookup at an empty dir so a real
    # /var/lib/ccswitch/health/last-restart on the host can't leak into snapshots.
    monkeypatch.setenv("CCSWITCH_HEALTH_STATE_DIR", str(tmp_path))


def _epoch(start_ms: int, pid: int = 1234, suffix: str = "abcd1234") -> str:
    return f"{start_ms:x}-{pid}-{suffix}"


def test_epoch_started_at_ms_parses_hex_prefix():
    assert epoch_started_at_ms(_epoch(1_700_000_000_000)) == 1_700_000_000_000
    # Robust to a plain PROC_EPOCH (`<hex>-<pid>`) without the daemon's uuid suffix.
    assert epoch_started_at_ms("18c0-99") == 0x18C0


def test_epoch_started_at_ms_returns_none_for_garbage():
    assert epoch_started_at_ms("") is None
    assert epoch_started_at_ms(None) is None
    assert epoch_started_at_ms("not-hex-value") is None


def test_first_epoch_is_not_a_restart():
    mon = DaemonHealthMonitor(info_fn=None)
    mon.record(_epoch(1_000_000), now=1000.0)
    snap = mon.snapshot(now=1000.0)
    assert snap["available"] is True
    assert snap["restartCount24h"] == 0
    assert snap["lastRestartAt"] is None
    assert snap["epoch"] == _epoch(1_000_000)


def test_epoch_change_counts_as_restart():
    mon = DaemonHealthMonitor(info_fn=None)
    mon.record(_epoch(1_000_000), now=1000.0)
    # Same epoch again → no new restart.
    mon.record(_epoch(1_000_000), now=1005.0)
    assert mon.snapshot(now=1005.0)["restartCount24h"] == 0
    # New epoch → one restart.
    mon.record(_epoch(2_000_000), now=1010.0)
    snap = mon.snapshot(now=1010.0)
    assert snap["restartCount24h"] == 1
    assert snap["lastRestartAt"] is not None


def test_restarts_outside_window_are_pruned():
    mon = DaemonHealthMonitor(info_fn=None, window_s=100.0)
    mon.record(_epoch(1), now=0.0)
    mon.record(_epoch(2), now=10.0)    # restart at t=10
    mon.record(_epoch(3), now=20.0)    # restart at t=20
    assert mon.snapshot(now=25.0)["restartCount24h"] == 2
    # Advance past the window from the first restart: only t=20 remains within 100s of t=115.
    assert mon.snapshot(now=115.0)["restartCount24h"] == 1


def test_snapshot_reports_uptime_from_epoch():
    started_ms = 1_700_000_000_000
    mon = DaemonHealthMonitor(info_fn=None)
    mon.record(_epoch(started_ms), now=started_ms / 1000 + 42.0)
    snap = mon.snapshot(now=started_ms / 1000 + 42.0)
    assert snap["uptimeSec"] == pytest.approx(42.0, abs=1.0)
    assert snap["startedAt"] is not None


def test_snapshot_unavailable_before_first_poll():
    mon = DaemonHealthMonitor(info_fn=None)
    snap = mon.snapshot(now=1000.0)
    assert snap["available"] is False
    assert snap["epoch"] is None
    assert snap["restartCount24h"] == 0


def test_poll_once_records_epoch_from_info_fn():
    calls = {"n": 0}

    async def info_fn():
        calls["n"] += 1
        return {"epoch": _epoch(5_000_000), "sha": "deadbeef"}

    mon = DaemonHealthMonitor(info_fn=info_fn, clock=lambda: 5000.0)
    asyncio.run(mon.poll_once())
    assert calls["n"] == 1
    assert mon.snapshot()["epoch"] == _epoch(5_000_000)


def test_poll_once_swallows_info_errors():
    async def info_fn():
        raise RuntimeError("daemon mid-restart")

    mon = DaemonHealthMonitor(info_fn=info_fn, clock=lambda: 1.0)
    # Must not raise — a momentarily-unreachable daemon is expected during a restart.
    asyncio.run(mon.poll_once())
    assert mon.snapshot()["available"] is False


# ---------------------------------------------------------------------------
# Issue #766: available must reflect a live probe, not the last cached epoch.
# ---------------------------------------------------------------------------

def test_last_seen_ago_sec_is_none_before_first_poll():
    mon = DaemonHealthMonitor(info_fn=None)
    snap = mon.snapshot(now=1000.0)
    assert snap["lastSeenAgoSec"] is None


def test_last_seen_ago_sec_is_zero_immediately_after_record():
    mon = DaemonHealthMonitor(info_fn=None)
    mon.record(_epoch(1_000_000), now=500.0)
    snap = mon.snapshot(now=500.0)
    assert snap["lastSeenAgoSec"] == pytest.approx(0.0)
    assert snap["available"] is True


def test_available_becomes_false_after_staleness_window():
    """Once no successful probe arrives within stale_after_s, available must flip False.

    Invariant from issue #766: if a daemon RPC would raise DaemonUnavailable,
    /api/daemon/health must not report available=True.
    """
    mon = DaemonHealthMonitor(info_fn=None, poll_interval_s=15.0, stale_after_s=45.0)
    mon.record(_epoch(1_000_000), now=0.0)
    assert mon.snapshot(now=44.9)["available"] is True   # within window
    snap = mon.snapshot(now=45.1)
    assert snap["available"] is False                    # stale — past window
    assert snap["lastSeenAgoSec"] == pytest.approx(45.1, abs=0.01)
    # Epoch is preserved for diagnostic purposes even when stale.
    assert snap["epoch"] == _epoch(1_000_000)


def test_available_recovers_after_fresh_probe():
    """A successful record() after a stale period restores available=True."""
    mon = DaemonHealthMonitor(info_fn=None, stale_after_s=30.0)
    mon.record(_epoch(1_000_000), now=0.0)
    assert mon.snapshot(now=60.0)["available"] is False  # gone stale
    mon.record(_epoch(1_000_000), now=60.0)              # fresh probe arrived
    assert mon.snapshot(now=60.0)["available"] is True


def test_stale_invariant_probe_errors_never_set_last_seen():
    """poll_once() errors must not advance _last_seen_at (daemon is unreachable)."""
    async def always_fail():
        raise RuntimeError("DaemonUnavailable")

    mon = DaemonHealthMonitor(
        info_fn=always_fail, clock=lambda: 100.0,
        poll_interval_s=15.0, stale_after_s=45.0,
    )
    # Seed an initial successful epoch so available starts True.
    mon.record(_epoch(1_000_000), now=0.0)

    # Run two failed polls (clock stays at 100.0, which is past stale_after_s=45).
    asyncio.run(mon.poll_once())
    asyncio.run(mon.poll_once())

    snap = mon.snapshot(now=100.0)
    # The failed polls must not have reset _last_seen_at, so the staleness is intact.
    assert snap["available"] is False
    assert snap["lastSeenAgoSec"] == pytest.approx(100.0, abs=0.01)
