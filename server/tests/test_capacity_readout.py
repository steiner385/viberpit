"""Capacity readout builder tests (spec 2026-09-19 §5, D6, D7).

Pure: every seam is an injected async/sync lambda over ``CapacitySources``, so
these tests never touch the DB.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from server.automations.capacity_readout import CapacitySources, build_readout, readout_to_inputs

_ACCOUNT_ROWS = [
    {"name": "acct1", "engine": "claude_code", "provider": "anthropic", "enabled": True,
     "metered": True, "available": True, "reason": None, "nextResetAt": None, "windows": []},
    {"name": "acct2", "engine": "claude_code", "provider": "anthropic", "enabled": True,
     "metered": True, "available": False, "reason": "at_ceiling",
     "nextResetAt": "2026-09-20T00:00:00+00:00", "windows": []},
]

_PRODUCTS = [{"slug": "viberpit", "name": "ViberPit", "priorityRank": 1,
              "createdAt": "2026-09-01", "policy": {"dispatch": "auto"}, "primaryRepo": "o/r"}]

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

    async def accounts(reserve):
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
