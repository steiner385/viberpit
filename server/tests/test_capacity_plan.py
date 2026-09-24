from server.automations.capacity_plan import (
    AccountHeadroom,
    CapacityInputs,
    HostCapacity,
    ProductCapacity,
    _split,
    plan_capacity,
)


def _acct(name, available=True, reason=None, next_reset_at=None):
    return AccountHeadroom(name=name, engine="claude", metered=True, available=available,
                           reason=reason, next_reset_at=next_reset_at)


def _host(name, active, max):
    return HostCapacity(name=name, enabled=True, healthy=True, active=active, max=max)


def _prod(slug, rank, eligible, in_flight=0, mode="auto", pin=1, dispatch="auto",
          pipeline=0, pipeline_cap=None):
    return ProductCapacity(slug=slug, rank=rank, created_at="2026-01-01T00:00:00+00:00",
                           dispatch=dispatch, capacity_mode=mode, pin=pin,
                           in_flight=in_flight, eligible=eligible,
                           pipeline=pipeline, pipeline_cap=pipeline_cap)


def _inputs(**overrides):
    defaults = dict(
        global_cap=10,
        global_active=0,
        hosts=(_host("d", 0, 6),),
        accounts=(_acct("a1"), _acct("a2"), _acct("a3"), _acct("a4")),
        runner_ceiling=6,
        runner_in_flight=0,
        products=(_prod("kindash", 1, 50), _prod("viberpit", 2, 30), _prod("ghostdriver", 3, 5)),
    )
    defaults.update(overrides)
    return CapacityInputs(**defaults)


def test_reserve_follows_push_level():
    i = _inputs()
    assert plan_capacity(i, push_level="max").budget["reservePct"] == 5


def test_fleet_is_min_of_four_ceilings():
    i = _inputs()
    plan = plan_capacity(i, push_level="steady")
    assert plan.fleet_concurrency == 6
    assert plan.limiting == "runner"

    i2 = _inputs(runner_ceiling=20, hosts=(_host("d", 4, 6),))
    plan2 = plan_capacity(i2, push_level="steady")
    assert plan2.fleet_concurrency == 2
    assert plan2.limiting == "host"


def test_split_is_rank_ordered_round_robin():
    products = [_prod("kindash", 1, 50), _prod("viberpit", 2, 30), _prod("ghostdriver", 3, 5)]
    assert _split(6, products) == {"kindash": 2, "viberpit": 2, "ghostdriver": 2}

    products2 = [_prod("kindash", 1, 50), _prod("viberpit", 2, 30), _prod("ghostdriver", 3, 1)]
    assert _split(6, products2) == {"kindash": 3, "viberpit": 2, "ghostdriver": 1}


def test_pins_come_first():
    i = _inputs(products=(
        _prod("kindash", 1, 50),
        _prod("viberpit", 2, 30, mode="pinned", pin=1),
        _prod("ghostdriver", 3, 5),
    ))
    plan = plan_capacity(i, push_level="steady")
    assert plan.per_product["viberpit"] == 1
    assert "viberpit" not in plan.pending
    assert plan.pending == {"kindash": 3, "ghostdriver": 2}


def test_never_below_in_flight_or_above_eligible():
    products = [_prod("kindash", 1, 50), _prod("viberpit", 2, 30), _prod("ghostdriver", 3, 1)]
    caps = _split(6, products)
    assert caps["ghostdriver"] <= 1

    caps2 = _split(3, [_prod("kindash", 1, 50, in_flight=4)])
    assert caps2["kindash"] >= 4


def test_hysteresis_carry_is_floored_at_in_flight():
    """The pending/carry branch used to carry forward the PREVIOUS tick's cap
    verbatim, which could be lower than what the product already holds — a
    plan-in-flux moment where the plan itself would (transiently) show a cap
    the runner's own in-flight count already exceeds. The carried value must
    never drop below `in_flight`, the same floor `_split` and `_fleet` apply
    elsewhere."""
    from server.automations.capacity_plan import CapacityPlan

    previous = CapacityPlan(push_level="steady", fleet_concurrency=1,
                            per_product={"kindash": 1}, pending={}, budget={})
    i = _inputs(products=(_prod("kindash", 1, eligible=50, in_flight=3),),
               runner_ceiling=4, hosts=(_host("d", 3, 8),), runner_in_flight=3)
    plan = plan_capacity(i, push_level="steady", previous=previous)
    assert plan.per_product["kindash"] == 3   # carried, floored at in_flight — not the stale 1
    assert plan.pending["kindash"] == 4       # the newly proposed value, unchanged


def test_hysteresis_applies_on_second_tick():
    i = _inputs()
    first = plan_capacity(i, push_level="steady")
    assert first.per_product == {"kindash": 1, "viberpit": 1, "ghostdriver": 1}
    assert first.pending == {"kindash": 2, "viberpit": 2, "ghostdriver": 2}

    second = plan_capacity(i, push_level="steady", previous=first)
    assert second.per_product == first.pending


def test_hysteresis_ticks_is_a_config_knob_not_a_hardcoded_two():
    """#2032 deferred-minor: `hysteresis_ticks` generalises the streak length —
    default 2 (unchanged), but 1 applies immediately and 3 needs a second
    confirming repeat before the value the default would already have applied."""
    i = _inputs()

    # hysteresis_ticks=1: nothing to confirm, the first proposal applies straight away.
    immediate = plan_capacity(i, push_level="steady", hysteresis_ticks=1)
    assert immediate.per_product == {"kindash": 2, "viberpit": 2, "ghostdriver": 2}
    assert immediate.pending == {}

    # hysteresis_ticks=3: the default's second (confirming) tick is not enough yet.
    first = plan_capacity(i, push_level="steady", hysteresis_ticks=3)
    second = plan_capacity(i, push_level="steady", hysteresis_ticks=3, previous=first)
    assert second.per_product == first.per_product   # still carried, not yet confirmed
    assert second.pending == first.pending
    third = plan_capacity(i, push_level="steady", hysteresis_ticks=3, previous=second)
    assert third.per_product == first.pending         # confirmed on the third matching tick
    assert third.pending == {}


def test_hysteresis_streak_survives_the_store_round_trip():
    """The streak has no column: the governor loop writes it as `budget.pendingStreak`
    and `plan_from_dict` restores it, so a real (persisted) plan confirms on the
    same tick an in-memory one would."""
    from server.automations.capacity_plan import plan_from_dict

    i = _inputs()
    first = plan_capacity(i, push_level="steady", hysteresis_ticks=3)
    stored = {"pushLevel": first.push_level, "fleetConcurrency": first.fleet_concurrency,
              "perProduct": first.per_product, "pending": first.pending,
              "budget": {**first.budget, "limiting": first.limiting,
                         "pendingStreak": first.pending_streak}}
    restored = plan_from_dict(stored)
    assert restored.pending_streak == {"kindash": 1, "viberpit": 1, "ghostdriver": 1}
    second = plan_capacity(i, push_level="steady", hysteresis_ticks=3, previous=restored)
    assert second.pending_streak == {"kindash": 2, "viberpit": 2, "ghostdriver": 2}


def test_a_changed_proposal_restarts_the_hysteresis_streak():
    """Confirmation is for the SAME value repeated: a source that flaps 2 → 3 → 2 must
    not accumulate a streak across the different values."""
    first = plan_capacity(_inputs(), push_level="steady", hysteresis_ticks=3)
    flapped = plan_capacity(_inputs(accounts=(_acct("a1"), _acct("a2"))), push_level="steady",
                            hysteresis_ticks=3, previous=first)
    assert flapped.pending != first.pending
    assert all(streak == 1 for slug, streak in flapped.pending_streak.items()
               if flapped.pending[slug] != first.pending.get(slug))


def test_zero_available_accounts_idles_with_reset():
    i = _inputs(accounts=(
        _acct("a1", available=False, next_reset_at="2026-09-23T08:00:00+00:00"),
        _acct("a2", available=False, next_reset_at="2026-09-23T08:00:00+00:00"),
        _acct("a3", available=False, next_reset_at="2026-09-23T08:00:00+00:00"),
        _acct("a4", available=False, next_reset_at="2026-09-23T08:00:00+00:00"),
    ))
    plan = plan_capacity(i, push_level="steady")
    assert plan.fleet_concurrency == 0
    assert plan.limiting == "account"
    assert any("08:00" in r for r in plan.reasons)


def test_fleet_never_drops_below_the_runners_own_in_flight():
    """N2, fix round 2: zero available accounts would otherwise compute fleet
    0 even with sessions already running — the floor keeps the plan from
    refusing capacity the runner already holds (decide_implementation would
    short-circuit on cap == 0 before its `occupied` adjustment, and a refusal
    on an already-claimed target raises StaleOwnership)."""
    i = _inputs(accounts=(
        _acct("a1", available=False), _acct("a2", available=False),
        _acct("a3", available=False), _acct("a4", available=False),
    ), runner_in_flight=2)
    plan = plan_capacity(i, push_level="steady")
    assert plan.fleet_concurrency == 2
    assert plan.limiting == "account"
    assert any("floored" in r for r in plan.reasons)


def test_budget_floor_is_emitted_only_when_it_changed_the_number():
    """#2040: `budget.floor` used to be present on every plan, so a plan whose
    `rawFleet` already cleared the runner's in-flight count still read as if a
    floor were binding. It is now emitted only when `rawFleet < floor`."""
    unfloored = plan_capacity(_inputs(runner_in_flight=1), push_level="steady")
    assert unfloored.budget["rawFleet"] >= 1
    assert "floor" not in unfloored.budget

    floored = plan_capacity(_inputs(accounts=(
        _acct("a1", available=False), _acct("a2", available=False),
    ), runner_in_flight=2), push_level="steady")
    assert floored.budget["rawFleet"] < 2
    assert floored.budget["floor"] == 2


def test_off_products_and_empty_backlogs_get_nothing():
    i = _inputs(products=(
        _prod("kindash", 1, 50, dispatch="off"),
        _prod("viberpit", 2, 0),
        _prod("ghostdriver", 3, 5),
    ))
    plan = plan_capacity(i, push_level="steady")
    assert "kindash" not in plan.per_product
    assert "viberpit" not in plan.per_product
    assert "ghostdriver" in plan.per_product


def test_a_product_with_live_claims_stays_in_the_plan_even_with_an_empty_backlog():
    """A product with `eligible == 0` but `in_flight > 0` is not dropped from
    the plan the way a truly idle product is: its cap is its in-flight floor.
    An empty backlog with NO in-flight claim still gets nothing (the case
    above)."""
    caps = _split(6, [_prod("kindash", 1, eligible=0, in_flight=2)])
    assert caps == {"kindash": 2}

    i = _inputs(products=(_prod("kindash", 1, eligible=0, in_flight=2),))
    plan = plan_capacity(i, push_level="steady")
    assert plan.per_product.get("kindash") == 2


def test_floor_never_lifts_a_cap_above_eligible_or_the_fleet_sum():
    from server.automations.capacity_plan import _split
    prods = [_prod("kindash", 1, eligible=2, in_flight=4), _prod("viberpit", 2, eligible=10)]
    caps = _split(5, prods)
    assert caps["kindash"] == 4                      # floor: never below in-flight
    assert caps["viberpit"] == 1                     # 5 - 4 already held = 1 slot left
    assert sum(caps.values()) == 5


def test_pinned_floor_is_deducted_before_the_round_robin():
    from server.automations.capacity_plan import _split
    prods = [_prod("viberpit", 1, eligible=1, in_flight=3, mode="pinned", pin=1),
             _prod("kindash", 2, eligible=10)]
    caps = _split(4, prods)
    assert caps["viberpit"] == 3 and caps["kindash"] == 1 and sum(caps.values()) == 4


def test_push_level_of_normalises_the_governor_config():
    from server.automations.capacity_plan import plan_from_dict, push_level_of
    assert push_level_of({"push_level": "max"}) == "max"
    assert push_level_of({"push_level": "bogus"}) == "steady"
    assert push_level_of({}) == "steady"
    assert plan_from_dict({"pushLevel": "bogus"}).push_level == "steady"


def test_pipeline_full_product_keeps_its_floor_and_gives_slots_to_the_next_rank():
    """Spec 2026-09-20 D13 / §9: a pipeline-full product is planned as eligible 0 —
    its cap is its in-flight count — and the slots it would have taken go to the
    next rank; the budget and reasons say so."""
    i = _inputs(products=(_prod("kindash", 1, 50, in_flight=1, pipeline=6, pipeline_cap=6),
                          _prod("viberpit", 2, 30, pipeline=1, pipeline_cap=8),
                          _prod("ghostdriver", 3, 5)))
    plan = plan_capacity(i, push_level="max")           # fleet 6 (runner ceiling)
    assert plan.per_product["kindash"] == 1             # floor only
    # A fresh plan (no `previous`) parks any auto product's round-robin value
    # that differs from its pin in `pending`, confirmed on the NEXT tick (D8
    # hysteresis, unrelated to this task) — so the redistributed slots show up
    # in `pending`, not yet in `per_product`.
    assert plan.pending["viberpit"] + plan.pending["ghostdriver"] == 5
    assert plan.budget["pipeline"] == {"kindash": {"open": 6, "cap": 6},
                                       "viberpit": {"open": 1, "cap": 8}}
    assert "kindash: pipeline full (6/6 claims and PRs), its slots go to the next rank" in plan.reasons


def test_pipeline_cap_zero_is_full_at_the_planner():
    """A pipeline cap of 0 refuses every claim (blank != 0, D13) — the planner
    treats it the same as any other pipeline-full product: eligible forced to 0.
    With no in-flight claim either, kindash never enters `working`/`proposed`
    (`_split`), so it gets no cap and no reason line (the reasons loop is guarded
    on `p.slug in proposed`) — only `budget.pipeline` records the fact."""
    i = _inputs(products=(_prod("kindash", 1, 50, pipeline=0, pipeline_cap=0),
                          _prod("viberpit", 2, 30), _prod("ghostdriver", 3, 5)))
    plan = plan_capacity(i, push_level="steady")
    assert plan.per_product.get("kindash", 0) == 0
    assert "kindash" not in plan.pending
    assert plan.budget["pipeline"]["kindash"] == {"open": 0, "cap": 0}
    assert not any(r.startswith("kindash: pipeline full") for r in plan.reasons)


def test_blank_pipeline_cap_changes_nothing():
    base = plan_capacity(_inputs(), push_level="max")
    with_zero_count = plan_capacity(_inputs(products=(
        _prod("kindash", 1, 50, pipeline=40), _prod("viberpit", 2, 30), _prod("ghostdriver", 3, 5))),
        push_level="max")
    assert with_zero_count.per_product == base.per_product
    assert with_zero_count.budget["pipeline"] == {}
