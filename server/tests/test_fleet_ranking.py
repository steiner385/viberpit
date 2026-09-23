"""Pure host ranking — no DB, no network, no live hosts.

Locality is deliberately NOT an input (2026-08-21 decision: ignore locality
entirely). ensure_clone is lazy and idempotent, so a cold host clones on demand;
filtering on locality would strand nearly every session on dobby.
"""
from __future__ import annotations

import pytest

from server.sessions.fleet import HostCandidate, rank_hosts


def _c(name, **kw):
    base = dict(name=name, enabled=True, healthy=True, weight=100,
                active_sessions=0, max_sessions=None, load_avg=0.0,
                mem_available_mb=8192)
    base.update(kw)
    return HostCandidate(**base)


def test_unknown_health_is_not_dispatchable():
    """healthy=None means 'never probed', which must not be read as 'up'."""
    assert rank_hosts([_c("rocky", healthy=None)]) == []


def test_unhealthy_and_disabled_hosts_are_excluded():
    assert rank_hosts([_c("a", healthy=False), _c("b", enabled=False)]) == []


def test_host_at_capacity_is_excluded():
    assert rank_hosts([_c("full", max_sessions=2, active_sessions=2)]) == []
    assert rank_hosts([_c("room", max_sessions=2, active_sessions=1)]) == ["room"]


def test_ignore_capacity_admits_a_full_host():
    """#2184: the assistant consumes no capacity slot anywhere else — the daemon's
    global-cap gate and `_consumes_capacity_slot` both exempt it — so refusing its
    launch *for lack of a slot* made the drawer unopenable whenever the primary was
    full (dobby's maxSessions is 4 and it sits at 3-4 routinely)."""
    full = [_c("full", max_sessions=2, active_sessions=2)]
    assert rank_hosts(full) == []
    assert rank_hosts(full, ignore_capacity=True) == ["full"]


def test_ignore_capacity_relaxes_only_the_capacity_leg():
    """A down, disabled or never-probed host must still be refused — an assistant
    launch onto a dead daemon is not a launch, it is a 500 later. Only the slot
    count is exempt."""
    assert rank_hosts([_c("down", healthy=False, max_sessions=2, active_sessions=2),
                       _c("off", enabled=False, max_sessions=2, active_sessions=2),
                       _c("unprobed", healthy=None, max_sessions=2, active_sessions=2)],
                      ignore_capacity=True) == []


def test_more_free_memory_ranks_higher():
    assert rank_hosts([_c("small", mem_available_mb=1024),
                       _c("big", mem_available_mb=16384)])[0] == "big"


def test_lower_load_ranks_higher():
    assert rank_hosts([_c("busy", load_avg=8.0), _c("idle", load_avg=0.1)])[0] == "idle"


def test_weight_overrides_marginal_differences():
    """An operator pinning weight must outrank a small metric edge."""
    assert rank_hosts([_c("preferred", weight=500, load_avg=1.0),
                       _c("default", weight=100, load_avg=0.9)])[0] == "preferred"


def test_ties_break_by_name_for_determinism():
    assert rank_hosts([_c("zeta"), _c("alpha")]) == ["alpha", "zeta"]


def test_empty_fleet_returns_empty():
    assert rank_hosts([]) == []


@pytest.mark.parametrize("missing", ["load_avg", "mem_available_mb"])
def test_missing_metrics_do_not_crash(missing):
    """A probed-but-partial host still ranks; it just scores conservatively."""
    assert rank_hosts([_c("partial", **{missing: None})]) == ["partial"]
