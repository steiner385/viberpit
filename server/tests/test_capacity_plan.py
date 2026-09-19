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


def _prod(slug, rank, eligible, in_flight=0, mode="auto", pin=1, dispatch="auto"):
    return ProductCapacity(slug=slug, rank=rank, created_at="2026-01-01T00:00:00+00:00",
                           dispatch=dispatch, capacity_mode=mode, pin=pin,
                           in_flight=in_flight, eligible=eligible)


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


def test_hysteresis_applies_on_second_tick():
    i = _inputs()
    first = plan_capacity(i, push_level="steady")
    assert first.per_product == {"kindash": 1, "viberpit": 1, "ghostdriver": 1}
    assert first.pending == {"kindash": 2, "viberpit": 2, "ghostdriver": 2}

    second = plan_capacity(i, push_level="steady", previous=first)
    assert second.per_product == first.pending


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
