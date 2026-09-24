"""Pure overlay tests for capacity_apply.py — the plan-to-policy/config seam the
Backlog Runner's read paths call once the governor's plan is confirmed live.
No I/O, no DB: every case is table-driven over plain dicts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from server.automations.capacity_apply import (
    CLOCK_SKEW_TOLERANCE_S,
    PLAN_MAX_AGE_S,
    apply_fleet_plan,
    apply_product_plan,
    plan_is_live,
)

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)


def _plan(**overrides) -> dict:
    base = {
        "computedAt": NOW.isoformat(),
        "pushLevel": "steady",
        "applied": True,
        "fleetConcurrency": 6,
        "perProduct": {"viberpit": 3},
        "pending": {},
        "budget": {},
        "binding": None,
        "reasons": [],
        "defects": [],
    }
    base.update(overrides)
    return base


# --- plan_is_live -----------------------------------------------------------


def test_plan_is_live_true_when_fresh_applied_and_enabled():
    assert plan_is_live(_plan(), now=NOW, enabled=True) is True


def test_plan_is_live_false_when_not_applied():
    assert plan_is_live(_plan(applied=False), now=NOW, enabled=True) is False


def test_plan_is_live_false_when_stale_11_minutes():
    computed = NOW - timedelta(minutes=11)
    plan = _plan(computedAt=computed.isoformat())
    assert plan_is_live(plan, now=NOW, enabled=True) is False


def test_plan_is_live_true_just_under_max_age():
    computed = NOW - timedelta(seconds=PLAN_MAX_AGE_S - 1)
    plan = _plan(computedAt=computed.isoformat())
    assert plan_is_live(plan, now=NOW, enabled=True) is True


def test_plan_is_live_false_when_kill_switch_off():
    assert plan_is_live(_plan(), now=NOW, enabled=False) is False


def test_plan_is_live_false_when_computed_at_missing():
    assert plan_is_live(_plan(computedAt=None), now=NOW, enabled=True) is False


def test_plan_is_live_false_when_plan_is_none():
    assert plan_is_live(None, now=NOW, enabled=True) is False


def test_plan_is_live_false_when_plan_is_empty_dict():
    assert plan_is_live({}, now=NOW, enabled=True) is False


def test_plan_is_live_naive_computed_at_assumed_utc():
    computed_naive = (NOW - timedelta(minutes=1)).replace(tzinfo=None)
    plan = _plan(computedAt=computed_naive.isoformat())
    assert plan_is_live(plan, now=NOW, enabled=True) is True


def test_plan_is_live_false_when_computed_at_unparseable():
    assert plan_is_live(_plan(computedAt="not-a-timestamp"), now=NOW, enabled=True) is False


def test_plan_is_live_false_when_computed_11_minutes_in_the_future():
    """Freshness is a window, not a distance from `now`: `abs()` used to treat
    a plan from 11 minutes in the future as equally live as one 11 minutes
    stale. A plan that far in the future was never computed against the
    world this reader sees."""
    computed = NOW + timedelta(minutes=11)
    plan = _plan(computedAt=computed.isoformat())
    assert plan_is_live(plan, now=NOW, enabled=True) is False


def test_plan_is_live_true_when_computed_30_seconds_in_the_future():
    """A small future skew (ordinary clock drift between the writer and this
    reader) is tolerated."""
    computed = NOW + timedelta(seconds=30)
    plan = _plan(computedAt=computed.isoformat())
    assert plan_is_live(plan, now=NOW, enabled=True) is True


def test_plan_is_live_skew_tolerance_boundary_is_the_named_constant():
    """The tolerance is `CLOCK_SKEW_TOLERANCE_S`, not an inline literal: exactly at
    it the plan is live, one second past it the plan is not."""
    at_edge = _plan(computedAt=(NOW + timedelta(seconds=CLOCK_SKEW_TOLERANCE_S)).isoformat())
    past_edge = _plan(computedAt=(NOW + timedelta(seconds=CLOCK_SKEW_TOLERANCE_S + 1)).isoformat())
    assert plan_is_live(at_edge, now=NOW, enabled=True) is True
    assert plan_is_live(past_edge, now=NOW, enabled=True) is False


def test_plan_is_live_logs_when_skew_beyond_tolerance_disables_the_plan(caplog):
    """A plan from the future silently reading as not-live left an operator
    with a governor in `act` that changed nothing and said nothing (#2040)."""
    plan = _plan(computedAt=(NOW + timedelta(seconds=CLOCK_SKEW_TOLERANCE_S + 30)).isoformat())
    with caplog.at_level("INFO", logger="server.automations.capacity_apply"):
        assert plan_is_live(plan, now=NOW, enabled=True) is False
    assert any("plan_from_the_future" in r.getMessage() for r in caplog.records)


def test_plan_is_live_does_not_log_for_an_ordinary_stale_or_fresh_plan(caplog):
    with caplog.at_level("INFO", logger="server.automations.capacity_apply"):
        assert plan_is_live(_plan(), now=NOW, enabled=True) is True
        stale = _plan(computedAt=(NOW - timedelta(seconds=PLAN_MAX_AGE_S + 1)).isoformat())
        assert plan_is_live(stale, now=NOW, enabled=True) is False
    assert not caplog.records


# --- apply_product_plan -------------------------------------------------


def test_apply_product_plan_overlays_auto_product():
    policies = {"viberpit": {"dispatch": "auto", "capacity_mode": "auto", "max_in_flight": 1}}
    plan = _plan(perProduct={"viberpit": 3})
    out = apply_product_plan(policies, plan)
    assert out["viberpit"]["max_in_flight"] == 3
    assert out["viberpit"]["dispatch"] == "auto"


def test_apply_product_plan_leaves_pinned_untouched():
    raw = {"dispatch": "auto", "capacity_mode": "pinned", "max_in_flight": 1}
    policies = {"viberpit": raw}
    plan = _plan(perProduct={"viberpit": 3})
    out = apply_product_plan(policies, plan)
    assert out["viberpit"] == raw
    assert out["viberpit"] is raw  # a pinned product's entry passes through unchanged, same object


def test_apply_product_plan_leaves_absent_slug_untouched():
    raw = {"dispatch": "auto", "capacity_mode": "auto", "max_in_flight": 1}
    policies = {"kindash": raw}
    plan = _plan(perProduct={"viberpit": 3})
    out = apply_product_plan(policies, plan)
    assert out["kindash"] == raw


def test_apply_product_plan_none_raw_stays_none():
    policies = {"viberpit": None}
    plan = _plan(perProduct={"viberpit": 3})
    out = apply_product_plan(policies, plan)
    assert out["viberpit"] is None


def test_apply_product_plan_none_plan_is_no_change():
    policies = {"viberpit": {"dispatch": "auto", "capacity_mode": "auto", "max_in_flight": 1}}
    out = apply_product_plan(policies, None)
    assert out == policies


def test_apply_product_plan_does_not_mutate_input_dicts():
    raw = {"dispatch": "auto", "capacity_mode": "auto", "max_in_flight": 1}
    policies = {"viberpit": raw}
    plan = _plan(perProduct={"viberpit": 3})
    apply_product_plan(policies, plan)
    assert raw == {"dispatch": "auto", "capacity_mode": "auto", "max_in_flight": 1}
    assert policies["viberpit"] is raw


def test_apply_product_plan_malformed_plan_is_unchanged():
    policies = {"viberpit": {"dispatch": "auto", "capacity_mode": "auto", "max_in_flight": 1}}
    out = apply_product_plan(policies, {"perProduct": "not-a-mapping"})
    assert out == policies


def test_apply_product_plan_non_numeric_per_product_value_is_unchanged():
    raw = {"dispatch": "auto", "capacity_mode": "auto", "max_in_flight": 1}
    policies = {"viberpit": raw}
    plan = _plan(perProduct={"viberpit": "not-a-number"})
    out = apply_product_plan(policies, plan)
    assert out["viberpit"] == raw


def test_apply_product_plan_non_dict_truthy_raw_passes_through_unchanged():
    # A corrupt stored policy row (wrong type, not just wrong shape) must never
    # raise: it passes through unchanged, like a None raw, while other slugs in
    # the same call are still overlaid normally.
    kindash_raw = {"dispatch": "auto", "capacity_mode": "auto", "max_in_flight": 1}
    plan = _plan(perProduct={"viberpit": 3, "kindash": 5})
    for bad_raw in ("not-a-dict", 123, ["a"], True):
        policies = {"viberpit": bad_raw, "kindash": kindash_raw}
        out = apply_product_plan(policies, plan)
        assert out["viberpit"] == bad_raw
        assert out["kindash"]["max_in_flight"] == 5


# --- apply_fleet_plan -----------------------------------------------------


def test_apply_fleet_plan_takes_the_lower_of_config_and_plan():
    config = {"max_concurrent": 10}
    out = apply_fleet_plan(config, _plan(fleetConcurrency=4))
    assert out["max_concurrent"] == 4


def test_apply_fleet_plan_config_lower_than_plan_stays():
    config = {"max_concurrent": 3}
    out = apply_fleet_plan(config, _plan(fleetConcurrency=10))
    assert out["max_concurrent"] == 3


def test_apply_fleet_plan_none_plan_is_no_change():
    config = {"max_concurrent": 10}
    out = apply_fleet_plan(config, None)
    assert out == config
    assert out is not config


def test_apply_fleet_plan_missing_fleet_concurrency_is_no_change():
    config = {"max_concurrent": 10}
    out = apply_fleet_plan(config, _plan(fleetConcurrency=None))
    assert out == config


def test_apply_fleet_plan_malformed_plan_is_unchanged():
    config = {"max_concurrent": 10}
    out = apply_fleet_plan(config, {"fleetConcurrency": "not-a-number"})
    assert out == config


def test_apply_fleet_plan_does_not_mutate_input():
    config = {"max_concurrent": 10, "other": "x"}
    apply_fleet_plan(config, _plan(fleetConcurrency=4))
    assert config == {"max_concurrent": 10, "other": "x"}
