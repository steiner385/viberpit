"""Build the capacity readout (spec 2026-09-19 §5) from injected seams. D6: the
binding constraint IS decide_implementation's first refused code, never a second
vocabulary; D7: a claim whose session is terminal is a defect, not load."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from server.db.models import INACTIVE_SESSION_STATUSES

from .backlog_runner import in_flight_by_product, is_in_flight
from .capacity_plan import (AccountHeadroom, CapacityInputs, HostCapacity, ProductCapacity,
                            RESERVE_PCT, product_order)
from .product_policy import effective_policy, is_on

LAYER_OF_CODE = {"product_capacity_full": "product", "global_capacity_full": "runner",
                 "daily_budget_full": "runner", "dispatch_off": "product",
                 "approval_required": "product", "runner_off": "runner"}


@dataclass
class CapacitySources:
    global_cap: Callable[[], Awaitable[int]]
    global_active: Callable[[], int]
    hosts: Callable[[], Awaitable[list[dict]]]
    accounts: Callable[[float], Awaitable[list[dict]]]
    runner_config: Callable[[], Awaitable[tuple[str, dict]]]
    target_states: Callable[[], Awaitable[dict[str, dict]]]
    candidates: Callable[[], Awaitable[tuple[int, int, int, dict | None]]]
    products: Callable[[], Awaitable[list[dict]]]
    decide: Callable[[str, str], Awaitable[tuple[Any, dict]]]
    session_status: Callable[[str], Awaitable[str | None]]
    plan: Callable[[], Awaitable[dict | None]]
    governor_config: Callable[[], Awaitable[tuple[str, dict]]]
    kill_switch: Callable[[], bool]


async def build_readout(src: CapacitySources) -> dict:
    mode, gov = await src.governor_config()
    push_level = gov.get("push_level", "steady")
    reserve = RESERVE_PCT.get(push_level, 20)
    global_cap = await src.global_cap()
    global_active = src.global_active()
    hosts = [{"name": h["name"], "enabled": bool(h.get("enabled")), "healthy": h.get("healthy"),
              "active": int(h.get("activeSessions") or 0), "max": h.get("maxSessions"),
              "free": (None if h.get("maxSessions") is None
                       else max(0, int(h["maxSessions"]) - int(h.get("activeSessions") or 0)))}
             for h in await src.hosts()]
    account_rows = await src.accounts(reserve)
    resets = sorted(r["nextResetAt"] for r in account_rows if r.get("nextResetAt"))
    _runner_mode, runner_cfg = await src.runner_config()
    states = await src.target_states()
    in_flight_total = sum(1 for s in states.values() if is_in_flight(s))
    by_product = in_flight_by_product(states)
    _eligible, _blocked, _untriaged, per_product = await src.candidates()
    per_product = per_product or {}

    defects: list[dict] = []
    for target, st in states.items():
        sid = st.get("session_id")
        if not is_in_flight(st) or not st.get("ownership_token") or not sid:
            continue
        status = await src.session_status(sid)
        if status in INACTIVE_SESSION_STATUSES:
            defects.append({"product": st.get("product"), "target": target, "sessionId": sid,
                            "sessionStatus": status,
                            "text": f"{target}: claim held by {status} session {sid[:8]} — a defect, not load"})
    defect_products = {d["product"] for d in defects}

    products: list[dict] = []
    for p in await src.products():
        slug = p["slug"]
        policy = effective_policy(p.get("policy"))
        counts = per_product.get(slug) or {}
        constraint, waiting = None, None
        if p.get("primaryRepo") and is_on(policy):
            decision, _pol = await src.decide(p["primaryRepo"], slug)
            refused = [c for c in decision.constraints if c.state != "permitted"]
            if refused:
                constraint, waiting = refused[0].code, refused[0].text
        products.append({
            "slug": slug, "name": p.get("name"), "rank": p.get("priorityRank"),
            "createdAt": p.get("createdAt"), "dispatch": policy["dispatch"],
            "capacityMode": policy["capacity_mode"], "pin": policy["max_in_flight"],
            "effectiveCap": policy["max_in_flight"], "inFlight": by_product.get(slug, 0),
            "eligible": int(counts.get("eligible") or 0), "constraint": constraint,
            "waitingOn": waiting, "defect": slug in defect_products,
        })
    ordered = product_order([ProductCapacity(
        slug=r["slug"], rank=r["rank"], created_at=r["createdAt"] or "", dispatch=r["dispatch"],
        capacity_mode=r["capacityMode"], pin=r["pin"], in_flight=r["inFlight"], eligible=r["eligible"])
        for r in products])
    order = [p.slug for p in ordered]
    products.sort(key=lambda r: order.index(r["slug"]))

    binding = None
    for r in products:
        if r["dispatch"] == "off" or r["eligible"] == 0:
            continue
        if r["defect"]:
            d = next(d for d in defects if d["product"] == r["slug"])
            binding = {"layer": "defect", "code": "capacity_defect", "text": d["text"]}
            break
        if r["constraint"]:
            binding = {"layer": LAYER_OF_CODE.get(r["constraint"], "product"),
                       "code": r["constraint"], "text": f"{r['slug']}: {r['waitingOn']}"}
            break
    plan = await src.plan()
    return {
        "computedAt": datetime.now(timezone.utc).isoformat(), "pushLevel": push_level,
        "governorMode": mode, "killSwitch": src.kill_switch(),
        "global": {"cap": global_cap, "active": global_active, "free": max(0, global_cap - global_active)},
        "hosts": hosts,
        "accounts": {"available": sum(1 for r in account_rows if r["available"]),
                     "total": len(account_rows), "reservePct": reserve,
                     "nextResetAt": resets[0] if resets else None, "rows": account_rows},
        "runner": {"ceiling": int(runner_cfg.get("max_concurrent") or 0), "inFlight": in_flight_total,
                   "derived": (plan or {}).get("fleetConcurrency")},
        "products": products, "binding": binding, "defects": defects, "plan": plan,
    }


def readout_to_inputs(r: dict) -> CapacityInputs:
    return CapacityInputs(
        global_cap=r["global"]["cap"], global_active=r["global"]["active"],
        hosts=tuple(HostCapacity(h["name"], h["enabled"], h["healthy"], h["active"], h["max"]) for h in r["hosts"]),
        accounts=tuple(AccountHeadroom(a["name"], a["engine"], a["metered"], a["available"],
                                       a.get("reason"), a.get("nextResetAt")) for a in r["accounts"]["rows"]),
        runner_ceiling=r["runner"]["ceiling"], runner_in_flight=r["runner"]["inFlight"],
        products=tuple(ProductCapacity(p["slug"], p["rank"], p["createdAt"] or "", p["dispatch"],
                                       p["capacityMode"], p["pin"], p["inFlight"], p["eligible"])
                       for p in r["products"]),
    )
