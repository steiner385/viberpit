"""Fleet endpoints expose the registry and let an operator tune it."""
from __future__ import annotations


def test_host_to_wire_shape():
    from datetime import datetime

    from server.api.fleet import host_to_wire
    from server.db.models import Host

    h = Host(name="rocky", endpoint="https://rocky:9443", enabled=True, weight=100,
             max_sessions=4, healthy=True, load_avg=1.25, mem_available_mb=2048,
             active_sessions=2, sha="abc1234", protocol=1,
             last_probe_at=datetime(2026, 8, 21, 12, 0, 0), last_error=None)
    w = host_to_wire(h)
    assert w["name"] == "rocky"
    assert w["maxSessions"] == 4
    assert w["memAvailableMb"] == 2048
    assert w["healthy"] is True
    assert isinstance(w["lastProbeAt"], str), "datetime must serialize for JSON"


def test_unprobed_host_reports_null_not_false():
    """healthy=None means 'never probed'. Collapsing it to false in the wire
    format would tell the operator a host is DOWN when it is merely unknown."""
    from server.api.fleet import host_to_wire
    from server.db.models import Host

    w = host_to_wire(Host(name="new", endpoint="unix:///a"))
    assert w["healthy"] is None
    assert w["lastProbeAt"] is None


def test_patch_rejects_unknown_fields():
    """Only operator-tunable fields may be written; probe-owned state must never
    be settable through the API, or the UI could lie to the ranker."""
    from server.api.fleet import sanitize_patch

    assert sanitize_patch({"enabled": False, "weight": 50, "maxSessions": 3}) == {
        "enabled": False, "weight": 50, "max_sessions": 3}
    assert sanitize_patch({"healthy": True, "loadAvg": 0.0, "sha": "x"}) == {}


def test_weight_is_clamped_to_a_sane_range():
    from server.api.fleet import sanitize_patch

    assert sanitize_patch({"weight": -5})["weight"] == 1
    assert sanitize_patch({"weight": 10_000})["weight"] == 1000


def test_none_values_are_ignored_not_written():
    """A PATCH omitting a field must leave it alone, not null it out."""
    from server.api.fleet import sanitize_patch

    assert sanitize_patch({"enabled": None, "weight": None, "maxSessions": None}) == {}
