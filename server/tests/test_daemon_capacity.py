"""/info carries the capacity signal the fleet ranker consumes.

Extending /info rather than adding an endpoint keeps the probe a single
round-trip, and means any daemon old enough to answer /info at all stays visible
to the fleet — it just reports no metrics.
"""
from __future__ import annotations

from server.ipc.daemon import _capacity_snapshot


def test_capacity_snapshot_shape():
    snap = _capacity_snapshot(active_sessions=3)
    assert snap["activeSessions"] == 3
    assert isinstance(snap["loadAvg"], float)
    assert isinstance(snap["memAvailableMb"], int)
    assert snap["memAvailableMb"] > 0, "a running host has some available memory"
    assert snap["loadAvg"] >= 0.0


def test_capacity_snapshot_survives_unreadable_meminfo(monkeypatch):
    """Degrade to None, never raise — unreadable /proc must not take /info down."""
    import server.ipc.daemon as d

    monkeypatch.setattr(d, "_read_mem_available_mb", lambda: None)
    assert d._capacity_snapshot(active_sessions=0)["memAvailableMb"] is None
