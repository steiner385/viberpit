"""Capacity readout builder tests (spec 2026-09-19 §5, D6, D7).

Pure: every seam is an injected async/sync lambda over ``CapacitySources``, so
these tests never touch the DB.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from types import SimpleNamespace

from server.automations.capacity_readout import CapacitySources, build_readout, readout_to_inputs

_ACCOUNT_ROWS = [
    {"name": "acct1", "engine": "claude_code", "provider": "anthropic", "enabled": True,
     "metered": True, "available": True, "reason": None, "nextResetAt": None, "reservePct": 20,
     "windows": []},
    {"name": "acct2", "engine": "claude_code", "provider": "anthropic", "enabled": True,
     "metered": True, "available": False, "reason": "at_ceiling",
     "nextResetAt": "2026-09-20T00:00:00+00:00", "reservePct": 20,
     "windows": []},
]

_PRODUCTS = [{"slug": "viberpit", "name": "ViberPit", "priorityRank": 1,
              "createdAt": "2026-09-01",
              "policy": {"dispatch": "auto", "max_pipeline_prs": 3}, "primaryRepo": "o/r"}]

_CANDIDATES = (10, 0, 0, {"viberpit": {"dispatch": "auto", "eligible": 10, "cap": 1}})

_FAKE_DECISION = SimpleNamespace(constraints=[
    SimpleNamespace(code="product_capacity_full", state="refused",
                    text="The product capacity limit is reached (1/1)."),
])


def _sources(*, session_status_value: str) -> CapacitySources:
    async def global_cap():
        return 6

    def global_active():
        return 2

    async def hosts():
        return [{"name": "dobby", "enabled": True, "healthy": True,
                  "activeSessions": 2, "maxSessions": 6}]

    async def accounts(push_level):
        return _ACCOUNT_ROWS

    async def runner_config():
        return ("on", {"max_concurrent": 6, "max_spawns_per_day": 500})

    async def target_states():
        return {"o/r#1": {"product": "viberpit", "session_id": "s1", "ownership_token": "t"}}

    async def candidates():
        return _CANDIDATES

    async def products():
        return _PRODUCTS

    async def decide(repo, slug):
        return _FAKE_DECISION, {}

    async def session_status(sid):
        return session_status_value

    async def plan():
        return None

    async def governor_config():
        return ("monitor", {"push_level": "steady", "sessions_per_account": 2})

    def kill_switch():
        return True

    return CapacitySources(
        global_cap=global_cap, global_active=global_active, hosts=hosts, accounts=accounts,
        runner_config=runner_config, target_states=target_states, candidates=candidates,
        products=products, decide=decide, session_status=session_status, plan=plan,
        governor_config=governor_config, kill_switch=kill_switch,
    )


def test_readout_binds_on_defect_before_product_constraint():
    async def run():
        return await build_readout(_sources(session_status_value="archived"))

    r = asyncio.run(run())
    assert r["products"][0]["constraint"] == "product_capacity_full"
    assert r["products"][0]["inFlight"] == 1
    assert r["defects"][0]["target"] == "o/r#1"
    assert r["binding"]["layer"] == "defect"
    assert r["accounts"]["available"] == 1
    assert readout_to_inputs(r).products[0].eligible == 10


def test_readout_binds_on_product_constraint_when_no_defect():
    async def run():
        return await build_readout(_sources(session_status_value="in_progress"))

    r = asyncio.run(run())
    assert r["defects"] == []
    assert r["binding"]["layer"] == "product"


# --- IMPORTANT 2: defect predicate is DEAD_CLAIM_STATUSES ∪ {None} (terminal + `complete`), not
# INACTIVE_SESSION_STATUSES (#2009: a `complete` session wedged a claim forever). ---

def test_defect_when_session_status_is_complete():
    """`complete` is CAPACITY_EXEMPT_STATUSES but NOT INACTIVE_SESSION_STATUSES —
    the #2009 shape: the session finished its turn but the claim's ownership token
    was never released."""
    async def run():
        return await build_readout(_sources(session_status_value="complete"))

    r = asyncio.run(run())
    assert r["defects"][0]["sessionStatus"] == "complete"
    assert r["binding"]["layer"] == "defect"


def test_defect_when_session_row_is_gone():
    """A missing row (session_status → None) is also a defect: the claim's token
    holder no longer exists at all, so nothing will ever release it either."""
    async def run():
        return await build_readout(_sources(session_status_value=None))

    r = asyncio.run(run())
    assert r["defects"][0]["sessionStatus"] is None
    assert r["binding"]["layer"] == "defect"


def test_no_defect_when_session_is_idle():
    """`idle` is neither gone nor terminal — ordinary live load, not a defect."""
    async def run():
        return await build_readout(_sources(session_status_value="idle"))

    r = asyncio.run(run())
    assert r["defects"] == []


@pytest.mark.parametrize("pending_status", ["queued", "pending_approval"])
def test_no_defect_when_session_is_pending(pending_status):
    """A claim held by a session that has not STARTED yet is a legitimate pending
    claim, not a dead one: it is capacity-exempt (holds no runtime slot) but it
    will start. The first live tick after deploy flagged viberpit#1831's queued
    runner session as a defect — the predicate must be the terminal set plus
    ``complete``, not the whole exempt set."""
    async def run():
        return await build_readout(_sources(session_status_value=pending_status))

    r = asyncio.run(run())
    assert r["defects"] == []
    assert r["binding"]["layer"] == "product"


# --- Generic source builder for the fallback/containment tests below: unlike
# `_sources`, every seam has a workable default so each test overrides only what
# it's exercising. ---

def _make_sources(*, global_cap=6, global_active=2, hosts=None, accounts=None,
                  runner_config=None, target_states=None, candidates=None,
                  products=None, decide=None, session_status=None, plan=None,
                  governor_config=None) -> CapacitySources:
    hosts = hosts if hosts is not None else [
        {"name": "dobby", "enabled": True, "healthy": True, "activeSessions": 2, "maxSessions": 6}]
    accounts = accounts if accounts is not None else _ACCOUNT_ROWS
    runner_config = runner_config or ("on", {"max_concurrent": 6, "max_spawns_per_day": 500})
    target_states = target_states if target_states is not None else {}
    candidates = candidates or (0, 0, 0, {})
    products = products if products is not None else []
    governor_config = governor_config or ("monitor", {"push_level": "steady", "sessions_per_account": 2})

    async def _global_cap():
        return global_cap

    def _global_active():
        return global_active

    async def _hosts():
        return hosts

    async def _accounts(push_level):
        return accounts

    async def _runner_config():
        return runner_config

    async def _target_states():
        return target_states

    async def _candidates():
        return candidates

    async def _products():
        return products

    async def _decide(repo, slug):
        if decide is not None:
            return await decide(repo, slug)
        raise AssertionError(f"decide() should not have been called for {slug!r}")

    async def _session_status(sid):
        return await session_status(sid) if session_status is not None else None

    async def _plan():
        return plan

    async def _governor_config():
        return governor_config

    return CapacitySources(
        global_cap=_global_cap, global_active=_global_active, hosts=_hosts, accounts=_accounts,
        runner_config=_runner_config, target_states=_target_states, candidates=_candidates,
        products=_products, decide=_decide, session_status=_session_status, plan=_plan,
        governor_config=_governor_config, kill_switch=lambda: True,
    )


# --- IMPORTANT 4: one product's admission read failing must not blank the stack. ---

def test_decide_failure_is_contained_to_its_own_product():
    async def decide(repo, slug):
        if slug == "broken":
            raise RuntimeError("gh boom")
        return _FAKE_DECISION, {}

    products = [
        {"slug": "broken", "name": "Broken", "priorityRank": 1, "createdAt": "2026-09-01",
         "policy": {"dispatch": "auto"}, "primaryRepo": "o/broken"},
        {"slug": "viberpit", "name": "ViberPit", "priorityRank": 2, "createdAt": "2026-09-01",
         "policy": {"dispatch": "auto"}, "primaryRepo": "o/r"},
    ]
    candidates = (15, 0, 0, {"broken": {"dispatch": "auto", "eligible": 5, "cap": 1},
                             "viberpit": {"dispatch": "auto", "eligible": 10, "cap": 1}})

    async def run():
        return await build_readout(
            _make_sources(products=products, candidates=candidates, decide=decide))

    r = asyncio.run(run())
    assert len(r["products"]) == 2
    by_slug = {p["slug"]: p for p in r["products"]}
    assert by_slug["broken"]["constraint"] == "readout_unavailable"
    # Fixed sentence, not the raw exception text (#2032 deferred-minor).
    assert by_slug["broken"]["waitingOn"] == "Admission read failed; retrying next tick."
    assert by_slug["broken"]["eligible"] == 5
    # The OTHER product's own read still ran normally and is unaffected.
    assert by_slug["viberpit"]["constraint"] == "product_capacity_full"
    assert by_slug["viberpit"]["eligible"] == 10


_FAKE_PR_DOCTOR_DECISION = SimpleNamespace(constraints=[
    SimpleNamespace(code="pr_doctor_priority", state="refused",
                    text="PR Doctor is working a stuck PR for this product first."),
])


def test_readout_surfaces_pr_doctor_priority_constraint():
    """Mirrors the ``product_capacity_full`` coverage above: any refused
    constraint ``decide()`` returns surfaces generically as the product row's
    ``constraint``/``waitingOn`` — spec 2026-09-21 pr-doctor-priority."""
    async def decide(repo, slug):
        return _FAKE_PR_DOCTOR_DECISION, {}

    async def run():
        return await build_readout(_make_sources(products=_PRODUCTS, decide=decide))

    r = asyncio.run(run())
    assert r["products"][0]["constraint"] == "pr_doctor_priority"
    assert "PR Doctor" in r["products"][0]["waitingOn"]


# --- SPEC GAPS (a): binding fallbacks below the product layer, one per layer. ---

def test_binding_falls_back_to_no_account_headroom():
    accounts = [{"name": "acct1", "engine": "claude_code", "provider": "anthropic",
                "enabled": True, "metered": True, "available": False,
                "reason": "at_ceiling", "nextResetAt": None, "windows": []}]

    async def run():
        return await build_readout(_make_sources(accounts=accounts))

    r = asyncio.run(run())
    assert r["binding"] == {"layer": "account", "code": "no_account_headroom",
                            "text": "no account has headroom under its reserve (push level 20%)"}


def test_binding_falls_back_to_hosts_full():
    accounts = [{"name": "acct1", "engine": "claude_code", "provider": "anthropic",
                "enabled": True, "metered": True, "available": True,
                "reason": None, "nextResetAt": None, "windows": []}]
    hosts = [{"name": "dobby", "enabled": True, "healthy": True,
              "activeSessions": 6, "maxSessions": 6}]

    async def run():
        return await build_readout(_make_sources(accounts=accounts, hosts=hosts))

    r = asyncio.run(run())
    assert r["binding"] == {"layer": "host", "code": "hosts_full",
                            "text": "every enabled healthy host is at its session cap"}


# --- Task 4: `effectiveCap` follows the live plan; readout gains `planLive`. ---

_PERMITTED_DECISION = SimpleNamespace(constraints=[
    SimpleNamespace(code="dispatch_allowed", state="permitted", text="ok"),
])


def _auto_product(capacity_mode: str | None = None) -> list[dict]:
    policy: dict = {"dispatch": "auto"}
    if capacity_mode is not None:
        policy["capacity_mode"] = capacity_mode
    return [{"slug": "viberpit", "name": "ViberPit", "priorityRank": 1, "createdAt": "2026-09-01",
             "policy": policy, "primaryRepo": "o/r"}]


_VIBERPIT_CANDIDATES = (10, 0, 0, {"viberpit": {"dispatch": "auto", "eligible": 10, "cap": 1}})


async def _decide_permitted(repo, slug):
    return _PERMITTED_DECISION, {}


def test_effective_cap_follows_live_plan_for_auto_product():
    now = datetime.now(timezone.utc)
    plan = {"computedAt": now.isoformat(), "applied": True, "perProduct": {"viberpit": 3}}

    async def run():
        return await build_readout(_make_sources(
            products=_auto_product(), candidates=_VIBERPIT_CANDIDATES,
            decide=_decide_permitted, plan=plan))

    r = asyncio.run(run())
    row = r["products"][0]
    assert row["effectiveCap"] == 3
    assert row["pin"] == 1
    assert r["planLive"] is True


def test_effective_cap_stays_pinned_when_product_is_pinned():
    now = datetime.now(timezone.utc)
    plan = {"computedAt": now.isoformat(), "applied": True, "perProduct": {"viberpit": 3}}

    async def run():
        return await build_readout(_make_sources(
            products=_auto_product("pinned"), candidates=_VIBERPIT_CANDIDATES,
            decide=_decide_permitted, plan=plan))

    r = asyncio.run(run())
    row = r["products"][0]
    assert row["effectiveCap"] == 1
    assert r["planLive"] is True


def test_effective_cap_falls_back_to_pin_when_plan_is_stale():
    stale = datetime.now(timezone.utc) - timedelta(minutes=11)
    plan = {"computedAt": stale.isoformat(), "applied": True, "perProduct": {"viberpit": 3}}

    async def run():
        return await build_readout(_make_sources(
            products=_auto_product(), candidates=_VIBERPIT_CANDIDATES,
            decide=_decide_permitted, plan=plan))

    r = asyncio.run(run())
    row = r["products"][0]
    assert row["effectiveCap"] == 1
    assert r["planLive"] is False


def test_effective_cap_falls_back_to_pin_when_plan_not_applied():
    now = datetime.now(timezone.utc)
    plan = {"computedAt": now.isoformat(), "applied": False, "perProduct": {"viberpit": 3}}

    async def run():
        return await build_readout(_make_sources(
            products=_auto_product(), candidates=_VIBERPIT_CANDIDATES,
            decide=_decide_permitted, plan=plan))

    r = asyncio.run(run())
    row = r["products"][0]
    assert row["effectiveCap"] == 1
    assert r["planLive"] is False


def test_binding_falls_back_to_global_cap_full():
    accounts = [{"name": "acct1", "engine": "claude_code", "provider": "anthropic",
                "enabled": True, "metered": True, "available": True,
                "reason": None, "nextResetAt": None, "windows": []}]
    # maxSessions None → unbounded free, so the host layer passes and the global
    # layer is what's left to explain zero fleet capacity.
    hosts = [{"name": "dobby", "enabled": True, "healthy": True,
              "activeSessions": 1, "maxSessions": None}]

    async def run():
        return await build_readout(_make_sources(
            accounts=accounts, hosts=hosts, global_cap=6, global_active=6))

    r = asyncio.run(run())
    assert r["binding"] == {"layer": "global", "code": "global_cap_full",
                            "text": "the global active-session cap (6) is reached"}


# --- N1, fix round 2: the runner row gains `effective` (what's actually       ---
# --- enforced) alongside the unchanged operator `ceiling`, and `derived`     ---
# --- (the plan's own fleetConcurrency) is gated on `planLive` like every     ---
# --- other overlay. ---

def test_runner_row_effective_follows_live_plan_fleet_concurrency():
    now = datetime.now(timezone.utc)
    plan = {"computedAt": now.isoformat(), "applied": True, "perProduct": {},
            "fleetConcurrency": 3}

    async def run():
        return await build_readout(_make_sources(
            runner_config=("on", {"max_concurrent": 6, "max_spawns_per_day": 500}), plan=plan))

    r = asyncio.run(run())
    assert r["planLive"] is True
    assert r["runner"]["ceiling"] == 6     # the operator's own stored value, unchanged
    assert r["runner"]["effective"] == 3   # min(ceiling, the live plan's fleetConcurrency)
    assert r["runner"]["derived"] == 3


def test_runner_row_effective_falls_back_to_ceiling_when_plan_is_not_live():
    stale = datetime.now(timezone.utc) - timedelta(minutes=11)
    plan = {"computedAt": stale.isoformat(), "applied": True, "perProduct": {},
            "fleetConcurrency": 3}

    async def run():
        return await build_readout(_make_sources(
            runner_config=("on", {"max_concurrent": 6, "max_spawns_per_day": 500}), plan=plan))

    r = asyncio.run(run())
    assert r["planLive"] is False
    assert r["runner"]["ceiling"] == 6
    assert r["runner"]["effective"] == 6   # not live: falls back to the stored ceiling
    assert r["runner"]["derived"] is None  # never shown as if it still governs


def test_product_and_runner_rows_both_fall_back_when_plan_not_applied():
    """Deferred-now 2, fix round 2: an unapplied (monitor-mode) plan must not
    overlay the per-product effectiveCap OR the runner row, in the same
    readout — a plan that applies to one but not the other would be a new
    way for the two halves of the same tick to disagree."""
    now = datetime.now(timezone.utc)
    plan = {"computedAt": now.isoformat(), "applied": False, "perProduct": {"viberpit": 3},
            "fleetConcurrency": 3}

    async def run():
        return await build_readout(_make_sources(
            products=_auto_product(), candidates=_VIBERPIT_CANDIDATES, decide=_decide_permitted,
            runner_config=("on", {"max_concurrent": 6, "max_spawns_per_day": 500}), plan=plan))

    r = asyncio.run(run())
    assert r["planLive"] is False
    row = r["products"][0]
    assert row["effectiveCap"] == 1        # the stored pin, not the plan's 3
    assert r["runner"]["ceiling"] == 6
    assert r["runner"]["effective"] == 6   # the stored ceiling, not the plan's 3
    assert r["runner"]["derived"] is None


def test_global_row_carries_the_two_ceilings_when_the_seam_is_wired():
    """spec 2026-09-20 D3: the stack's Global row says which ceiling binds."""
    async def limits():
        return {"physical": 10, "attention": 6, "effective": 6, "binding": "attention",
                "hosts": [{"name": "dobby", "cap": 4}, {"name": "rocky", "cap": 6}]}

    src = _make_sources(global_cap=6)
    src.global_limits = limits
    r = asyncio.run(build_readout(src))
    assert r["global"] == {"cap": 6, "active": 2, "free": 4,
                           "physical": 10, "attention": 6, "binding": "attention"}


def test_global_row_degrades_to_nulls_without_the_seam():
    r = asyncio.run(build_readout(_make_sources(global_cap=6)))
    assert r["global"]["physical"] is None and r["global"]["attention"] is None
    assert r["global"]["binding"] is None


def test_readout_carries_the_reserve_for_every_push_level():
    """spec 2026-09-20 C22: the web no longer mirrors RESERVE_PCT."""
    from server.automations.capacity_plan import RESERVE_PCT

    r = asyncio.run(build_readout(_make_sources()))
    assert r["reservePctByLevel"] == dict(RESERVE_PCT)
    assert set(r["reservePctByLevel"]) == {"conserve", "steady", "max"}


def test_wired_global_limits_degrades_to_null_when_the_read_fails(monkeypatch):
    """A DB blip in the Global row's derivation must not 500 the readout (same
    contract as the plan seam's _safe_plan)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from server.automations import capacity_wiring

    async def boom(db):
        raise RuntimeError("db blip")

    monkeypatch.setattr(capacity_wiring, "read_global_cap", boom)
    orch = SimpleNamespace(
        _max_active_sessions=AsyncMock(return_value=6), _active_session_count=lambda: 2,
        _backlog_read_config=AsyncMock(), _backlog_ctx=SimpleNamespace(list_target_states=AsyncMock()),
        _backlog_count_candidates=AsyncMock(), _capacity_governor_read_config=AsyncMock(),
    )
    src = capacity_wiring.sources_for(orch)
    assert asyncio.run(src.global_limits()) is None


def test_readout_carries_the_account_quota_for_the_runner_row():
    """spec 2026-09-20 §5.2 (Runner row: 'quota: N accounts × M'): available × sessions_per_account."""
    r = asyncio.run(build_readout(_make_sources(
        governor_config=("act", {"push_level": "steady", "sessions_per_account": 3}))))
    assert r["accounts"]["sessionsPerAccount"] == 3
    assert r["accounts"]["accountCap"] == r["accounts"]["available"] * 3


def test_runner_layer_codes_are_named_for_the_runner_ceiling():
    from server.automations.capacity_readout import LAYER_OF_CODE
    assert LAYER_OF_CODE["runner_ceiling_full"] == "runner"
    assert LAYER_OF_CODE["runner_ceiling_held"] == "runner"
    assert LAYER_OF_CODE["product_pipeline_full"] == "product"
    assert "global_capacity_full" not in LAYER_OF_CODE


def test_readout_hands_the_push_level_to_the_account_seam_and_keeps_each_rows_reserve():
    """Spec 2026-09-20 D4/§7: the seam is called with the PUSH LEVEL (it derives each
    account's effective reserve itself), and the per-row reservePct rides through
    untouched; the block-level reservePct stays the push level's default."""
    seen = {}
    rows = [dict(_ACCOUNT_ROWS[0], reservePct=60), dict(_ACCOUNT_ROWS[1])]

    async def accounts(push_level):
        seen["level"] = push_level
        return rows

    async def run():
        src = _make_sources(accounts=rows,
                            governor_config=("monitor", {"push_level": "max", "sessions_per_account": 2}))
        src.accounts = accounts   # replace the helper's list-wrapping seam with the spying one
        return await build_readout(src)

    r = asyncio.run(run())
    assert seen["level"] == "max"
    assert r["accounts"]["reservePct"] == 5
    assert [row["reservePct"] for row in r["accounts"]["rows"]] == [60, 20]


def test_readout_publishes_the_pipeline_count_cap_and_repo():
    """Spec 2026-09-20 §7/§9: products[].pipeline counts every row that
    counts_toward_pipeline (handed-off rows included, gave_up and token-less
    handed_off excluded), pipelineCap is the policy's max_pipeline_prs (None when
    blank), primaryRepo rides along for the Waiting-on link."""
    states = {
        "o/r#1": {"product": "viberpit", "session_id": "s1", "ownership_token": "t1"},
        "o/r#2": {"product": "viberpit", "session_id": "s2", "ownership_token": "t2", "handed_off": True},
        "o/r#3": {"product": "viberpit", "handed_off": True},                 # no token: excluded
        "o/r#4": {"product": "viberpit", "gave_up": True},                     # terminal: excluded
    }

    async def live(_sid):
        return "running"          # a live session: no defect for the token-bearing rows

    async def run():
        return await build_readout(_make_sources(
            products=_PRODUCTS, target_states=states, session_status=live))

    r = asyncio.run(run())
    row = r["products"][0]
    assert row["pipeline"] == 2
    assert row["pipelineCap"] == 3
    assert row["primaryRepo"] == "o/r"
    inputs = readout_to_inputs(r)
    assert inputs.products[0].pipeline == 2 and inputs.products[0].pipeline_cap == 3
