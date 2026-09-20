"""Build the capacity readout (spec 2026-09-19 §5) from injected seams. D6: the
binding constraint IS decide_implementation's first refused code, never a second
vocabulary; D7: a claim whose session is terminal is a defect, not load."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from server.db.models import INACTIVE_SESSION_STATUSES

from .backlog_runner import in_flight_by_product, is_in_flight
from .capacity_apply import apply_product_plan, plan_is_live
from .capacity_plan import (AccountHeadroom, CapacityInputs, HostCapacity, ProductCapacity,
                            RESERVE_PCT, product_order)
from .product_policy import effective_policy, is_on

#: Session statuses under which a token-bearing claim can never be released by
#: the runner's sweep (D7): the terminal rows plus `complete`, which releases its
#: driver at once and was the #2009 shape. Deliberately narrower than
#: CAPACITY_EXEMPT_STATUSES — `queued` and `pending_approval` hold no runtime slot
#: yet but will start, so their claims are pending, not dead.
DEAD_CLAIM_STATUSES: frozenset[str] = INACTIVE_SESSION_STATUSES | {"complete"}

log = logging.getLogger(__name__)

# The fixed vocabulary decide_implementation refuses with, plus the three
# spec-gap fallback codes below (deliberately OUTSIDE that vocabulary — nothing
# in the product loop ever produces them).
LAYER_OF_CODE = {"product_capacity_full": "product", "runner_ceiling_full": "runner",
                 "daily_budget_full": "runner", "dispatch_off": "product",
                 "approval_required": "product", "runner_off": "runner",
                 "no_account_headroom": "account", "hosts_full": "host",
                 "global_cap_full": "global",
                 # Ruling 20 (product_policy.capacity()): an occupied claim's
                 # ceiling constraint is now "permitted" (`*_held`), so it never
                 # refuses and never binds — these entries exist only so a
                 # future reader of one of these codes (e.g. a diagnostic view
                 # that doesn't filter by state) maps it to the same layer as
                 # its `_full` sibling rather than the "product" default.
                 "runner_ceiling_held": "runner", "product_capacity_held": "product"}


def _layer_for(code: str) -> str:
    """The ceiling-stack layer a binding CODE belongs to. A dict lookup for the
    fixed vocabulary above, plus two structural families that would otherwise need
    a growing dict entry per new constraint code: every ``ownership_*`` code is the
    ownership layer, and every ``*_unknown`` code (an unresolved/unavailable read)
    is the unknown layer. Anything else defaults to "product" — decide_implementation
    only ever refuses on a product-scoped concern it hasn't named one of the above."""
    if code in LAYER_OF_CODE:
        return LAYER_OF_CODE[code]
    if code.startswith("ownership_"):
        return "ownership"
    if code.endswith("_unknown"):
        return "unknown"
    return "product"


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
    # Optional (spec 2026-09-20 D3): the derivation behind ``global_cap`` —
    # ``GlobalCap.to_wire()`` — so the Global row can say which ceiling binds.
    # None (older callers / pure tests) degrades the three fields to null.
    global_limits: Callable[[], Awaitable[dict | None]] | None = None


async def build_readout(src: CapacitySources) -> dict:
    mode, gov = await src.governor_config()
    push_level = gov.get("push_level", "steady")
    reserve = RESERVE_PCT.get(push_level, 20)
    # The account quota appears twice on the wire on purpose: here it is LIVE
    # (this call's account rows × the governor's current sessions_per_account),
    # while `plan.budget.accountCap` / `.sessionsPerAccount` (capacity_plan._fleet)
    # is what the governor saw on its LAST tick and may be stale or absent. The
    # UI reads the readout's `accounts` block; the plan's copy explains the plan.
    sessions_per_account = int(gov.get("sessions_per_account") or 2)
    global_cap = await src.global_cap()
    global_active = src.global_active()
    limits = (await src.global_limits()) if src.global_limits is not None else None
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
        # A terminal session (or one whose row is simply GONE — None) still holds
        # the claim's ownership token, so nothing ever frees the slot: #2009's
        # shape was a `complete` session (not merely INACTIVE_SESSION_STATUSES)
        # wedging a product's in-flight count forever. NOT the whole
        # CAPACITY_EXEMPT_STATUSES set: `queued` / `pending_approval` sessions hold
        # no runtime slot yet but WILL start — a legitimate pending claim, not a
        # dead one (the first live tick flagged a queued runner session).
        if status is None or status in DEAD_CLAIM_STATUSES:
            defects.append({"product": st.get("product"), "target": target, "sessionId": sid,
                            "sessionStatus": status,
                            "text": f"{target}: claim held by {status or 'a missing'} session "
                                    f"{sid[:8]} — a defect, not load"})
    # A defect's "product" is None for a pre-flip claim (in_flight_by_product skips
    # those too) — it can never match a real product slug below, so it never binds;
    # it still surfaces in `defects` for the card.
    defect_products = {d["product"] for d in defects}

    now = datetime.now(timezone.utc)
    plan = await src.plan()
    live = plan_is_live(plan, now=now, enabled=src.kill_switch())

    products: list[dict] = []
    for p in await src.products():
        slug = p["slug"]
        policy = effective_policy(p.get("policy"))
        counts = per_product.get(slug) or {}
        constraint, waiting = None, None
        if not is_on(policy):
            # decide_implementation's own text for this code — the product loop
            # never has to call decide (and never has to reach GitHub) to know a
            # product that opted out is opted out.
            constraint = "dispatch_off"
            waiting = "Automatic issue dispatch is off for this product."
        elif not p.get("primaryRepo"):
            waiting = "No primary repository registered."
        else:
            try:
                decision, _pol = await src.decide(p["primaryRepo"], slug)
            except Exception as exc:  # noqa: BLE001 — one product's admission read must not blank the whole stack
                log.warning("capacity_readout.decide_failed slug=%s", slug, exc_info=True)
                constraint = "readout_unavailable"
                waiting = f"admission read failed: {exc}"
            else:
                refused = [c for c in decision.constraints if c.state != "permitted"]
                if refused:
                    constraint, waiting = refused[0].code, refused[0].text
        # THE overlay rule (fix round 1, F4): the same `apply_product_plan` +
        # `effective_policy` composition every other read seam uses, not a
        # second hand-rolled "auto + slug in perProduct" check that could
        # silently drift from the real one (e.g. the pinned-mode guard).
        overlaid_policy = apply_product_plan({slug: p.get("policy")}, plan if live else None)[slug]
        effective_cap = effective_policy(overlaid_policy)["max_in_flight"]
        products.append({
            "slug": slug, "name": p.get("name"), "rank": p.get("priorityRank"),
            "createdAt": p.get("createdAt"), "dispatch": policy["dispatch"],
            "capacityMode": policy["capacity_mode"], "pin": policy["max_in_flight"],
            "effectiveCap": effective_cap, "inFlight": by_product.get(slug, 0),
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
            binding = {"layer": _layer_for(r["constraint"]),
                       "code": r["constraint"], "text": f"{r['slug']}: {r['waitingOn']}"}
            break

    accounts_available = sum(1 for r in account_rows if r["available"])
    global_free = max(0, global_cap - global_active)
    if binding is None:
        # These three codes are deliberately outside decide_implementation's
        # vocabulary: no product is refusing on them individually (every eligible
        # product's own constraint was already checked above and cleared), but the
        # FLEET still can't dispatch anything, so the ceiling stack must say why.
        if accounts_available == 0:
            reset_note = f"; earliest reset {resets[0]}" if resets else ""
            binding = {"layer": "account", "code": "no_account_headroom",
                       "text": f"no account has headroom under the {reserve}% reserve{reset_note}"}
        elif not any(h["enabled"] and h["healthy"] is True and (h["free"] is None or h["free"] > 0)
                     for h in hosts):
            binding = {"layer": "host", "code": "hosts_full",
                       "text": "every enabled healthy host is at its session cap"}
        elif global_free == 0:
            binding = {"layer": "global", "code": "global_cap_full",
                       "text": f"the global active-session cap ({global_cap}) is reached"}

    # `ceiling` is the OPERATOR's stored value — unchanged, it is the planner's
    # own input (``readout_to_inputs`` reads it as ``runner_ceiling``) and must
    # never itself be overlaid or the governor would feed its own prior output
    # back into the next tick (fix round 1, F1). `derived`/`effective` are the
    # UI-facing "what the runner is actually enforcing right now" pair, gated
    # on `live` the same way every other overlay is (N1, fix round 2): a
    # stale/disabled/unapplied plan must never show as if it still governs.
    ceiling = int(runner_cfg.get("max_concurrent") or 0)
    derived: int | None = None
    if live and plan:
        try:
            fleet_concurrency = plan.get("fleetConcurrency")
            derived = None if fleet_concurrency is None else int(fleet_concurrency)
        except (TypeError, ValueError):
            derived = None
    effective = ceiling if derived is None else min(ceiling, derived)

    return {
        "computedAt": now.isoformat(), "pushLevel": push_level,
        "governorMode": mode, "killSwitch": src.kill_switch(),
        # C22: the three reserves ride on the wire so the push-level radio's
        # descriptions can never drift from the planner's RESERVE_PCT.
        "reservePctByLevel": dict(RESERVE_PCT),
        "global": {"cap": global_cap, "active": global_active, "free": global_free,
                   "physical": limits.get("physical") if limits else None,
                   "attention": limits.get("attention") if limits else None,
                   "binding": limits.get("binding") if limits else None},
        "hosts": hosts,
        "accounts": {"available": accounts_available,
                     "total": len(account_rows), "reservePct": reserve,
                     "sessionsPerAccount": sessions_per_account,
                     "accountCap": accounts_available * sessions_per_account,
                     "nextResetAt": resets[0] if resets else None, "rows": account_rows},
        "runner": {"ceiling": ceiling, "effective": effective, "inFlight": in_flight_total,
                   "derived": derived},
        "products": products, "binding": binding, "defects": defects, "plan": plan,
        "planLive": live,
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
