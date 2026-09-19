"""CapacityGovernorLoop tests — every seam faked, no DB and no network (Task 7).

Same shape as ``test_control_band_monitor.py``: the loop composes the pure
planner (``capacity_plan.plan_capacity``) with the readout builder's output
over injected seams, so the fakes here record what it *decided* and *wrote*.
"""
from __future__ import annotations

import pytest

from server.automations.capacity_governor import CapacityGovernor, CapacityGovernorLoop
from server.automations.engine_policy import EnginePolicy


class FakeCtx:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def emit(self, level, summary, *, kind=None, target=None,
                   session_id=None, detail=None):
        self.events.append({"level": level, "summary": summary, "kind": kind,
                            "target": target, "session_id": session_id,
                            "detail": detail or {}})

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]


def _account_rows(available: int, total: int = 2) -> list[dict]:
    rows = []
    for i in range(total):
        ok = i < available
        rows.append({"name": f"acct{i}", "engine": "claude_code", "metered": True,
                     "available": ok, "reason": None if ok else "at_ceiling",
                     "nextResetAt": None})
    return rows


def _product(slug: str, *, rank: int = 1, dispatch: str = "auto",
            capacity_mode: str = "auto", pin: int = 0, in_flight: int = 0,
            eligible: int = 5) -> dict:
    return {"slug": slug, "rank": rank, "createdAt": "2026-09-01", "dispatch": dispatch,
            "capacityMode": capacity_mode, "pin": pin, "inFlight": in_flight,
            "eligible": eligible}


_DEFECT = {"product": "viberpit", "target": "o/r#1", "sessionId": "sess-1",
           "text": "o/r#1: claim held by archived session sess-1 — a defect, not load"}


def _readout(*, products: list[dict] | None = None, defects: list[dict] | None = None,
            accounts_available: int = 2, accounts_total: int = 2,
            runner_ceiling: int = 10, global_cap: int = 10) -> dict:
    if products is None:
        products = [_product("viberpit", rank=1), _product("kindash", rank=2)]
    return {
        "global": {"cap": global_cap, "active": 0},
        "hosts": [{"name": "dobby", "enabled": True, "healthy": True, "active": 0, "max": None}],
        "accounts": {"available": accounts_available,
                     "rows": _account_rows(accounts_available, accounts_total)},
        "runner": {"ceiling": runner_ceiling, "inFlight": 0},
        "products": products,
        "binding": None,
        "defects": defects or [],
    }


def _build_loop(*, mode: str = "monitor", config: dict | None = None,
                readout: dict | None = None, readout_raises: bool = False,
                initial_plan: dict | None = None, kill_switch=lambda: True):
    ctx = FakeCtx()
    store: dict = {"plan": initial_plan}
    calls: dict = {"write_plan": [], "read_readout": 0}
    readout = readout if readout is not None else _readout()

    async def read_config():
        return mode, {"push_level": "steady", "sessions_per_account": 2, **(config or {})}

    async def read_readout():
        calls["read_readout"] += 1
        if readout_raises:
            raise RuntimeError("readout boom")
        return readout

    async def read_plan():
        return store["plan"]

    async def write_plan(plan):
        calls["write_plan"].append(plan)
        store["plan"] = plan

    loop = CapacityGovernorLoop(
        ctx, read_config=read_config, read_readout=read_readout,
        read_plan=read_plan, write_plan=write_plan, kill_switch=kill_switch,
    )
    return loop, ctx, calls, store


# --------------------------------------------------------------------------- #
# Tick behaviour                                                              #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_monitor_writes_plan_unapplied():
    loop, _ctx, calls, _store = _build_loop(mode="monitor")
    await loop.tick()
    assert len(calls["write_plan"]) == 1
    written = calls["write_plan"][0]
    assert written["applied"] is False
    assert written["perProduct"]


@pytest.mark.asyncio
async def test_act_with_kill_switch_applies():
    loop_on, _ctx1, calls_on, _store1 = _build_loop(mode="act", kill_switch=lambda: True)
    await loop_on.tick()
    assert calls_on["write_plan"][0]["applied"] is True

    loop_off, _ctx2, calls_off, _store2 = _build_loop(mode="act", kill_switch=lambda: False)
    await loop_off.tick()
    assert calls_off["write_plan"][0]["applied"] is False


@pytest.mark.asyncio
async def test_off_writes_nothing():
    loop, ctx, calls, _store = _build_loop(mode="off")
    await loop.tick()
    assert calls["write_plan"] == []
    assert calls["read_readout"] == 0
    assert ctx.events == []


@pytest.mark.asyncio
async def test_cap_changed_emitted_once_per_confirmed_change():
    # Single product, one account available (sessions_per_account=2 → fleet=2),
    # so the planner proposes 2 for "viberpit" — matching the pending value
    # the previous tick already parked, so it confirms on this tick.
    readout = _readout(products=[_product("viberpit", rank=1, eligible=5)],
                       accounts_available=1, accounts_total=1)
    previous_plan = {"pushLevel": "steady", "applied": False, "fleetConcurrency": 2,
                     "perProduct": {"viberpit": 1}, "pending": {"viberpit": 2}, "budget": {}}
    loop, ctx, calls, store = _build_loop(readout=readout, initial_plan=previous_plan)

    await loop.tick()
    cap_changed = [e for e in ctx.events if e["kind"] == "cap_changed"]
    assert len(cap_changed) == 1
    assert cap_changed[0]["target"] == "viberpit"
    assert calls["write_plan"][0]["perProduct"] == {"viberpit": 2}

    # A second tick with the same readout has nothing new to confirm.
    await loop.tick()
    cap_changed = [e for e in ctx.events if e["kind"] == "cap_changed"]
    assert len(cap_changed) == 1


@pytest.mark.asyncio
async def test_readout_failure_keeps_previous_plan_and_emits_plan_stale_once():
    loop, ctx, calls, _store = _build_loop(readout_raises=True)
    await loop.tick()
    await loop.tick()
    assert ctx.kinds().count("plan_stale") == 1
    assert calls["write_plan"] == []


@pytest.mark.asyncio
async def test_defect_event_once_per_target():
    readout = _readout(defects=[_DEFECT])
    loop, ctx, _calls, _store = _build_loop(readout=readout)
    await loop.tick()
    await loop.tick()
    defect_events = [e for e in ctx.events if e["kind"] == "capacity_defect"]
    assert len(defect_events) == 1
    assert defect_events[0]["level"] == "warn"
    assert defect_events[0]["target"] == "o/r#1"


# --------------------------------------------------------------------------- #
# Automation contract surface                                                 #
# --------------------------------------------------------------------------- #

def test_the_automation_surface_is_claude_only_with_off_monitor_act():
    a = CapacityGovernor()
    assert a.id == "capacity-governor"
    assert a.modes == ("off", "monitor", "act")
    assert a.engine_policy is EnginePolicy.CLAUDE_ONLY
    assert {f.key for f in a.config_schema} == {"push_level", "sessions_per_account"}


def test_status_is_idle_with_no_observation():
    a = CapacityGovernor()
    assert a.status(None).level == "idle"
