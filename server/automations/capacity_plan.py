"""Pure capacity planner (spec 2026-09-19 §6.2). No I/O; table-tested."""
from __future__ import annotations

from dataclasses import dataclass, field

PUSH_LEVELS: tuple[str, ...] = ("conserve", "steady", "max")
RESERVE_PCT: dict[str, int] = {"conserve": 40, "steady": 20, "max": 5}
UNBOUNDED = 10**6


@dataclass(frozen=True)
class AccountHeadroom:
    name: str; engine: str; metered: bool; available: bool
    reason: str | None = None; next_reset_at: str | None = None


@dataclass(frozen=True)
class HostCapacity:
    name: str; enabled: bool; healthy: bool | None; active: int; max: int | None


@dataclass(frozen=True)
class ProductCapacity:
    slug: str; rank: int | None; created_at: str; dispatch: str; capacity_mode: str
    pin: int; in_flight: int; eligible: int


@dataclass(frozen=True)
class CapacityInputs:
    global_cap: int; global_active: int
    hosts: tuple[HostCapacity, ...]; accounts: tuple[AccountHeadroom, ...]
    runner_ceiling: int; runner_in_flight: int
    products: tuple[ProductCapacity, ...]


@dataclass(frozen=True)
class CapacityPlan:
    push_level: str
    fleet_concurrency: int
    per_product: dict[str, int]
    pending: dict[str, int]
    budget: dict
    reasons: list[str] = field(default_factory=list)
    limiting: str = "none"


def product_order(products) -> list[ProductCapacity]:
    return sorted(products, key=lambda p: (p.rank is None, p.rank or 0, p.created_at))


def plan_from_dict(d: dict | None) -> CapacityPlan | None:
    if not d:
        return None
    return CapacityPlan(push_level=d.get("pushLevel") or "steady",
                        fleet_concurrency=int(d.get("fleetConcurrency") or 0),
                        per_product={k: int(v) for k, v in (d.get("perProduct") or {}).items()},
                        pending={k: int(v) for k, v in (d.get("pending") or {}).items()},
                        budget=dict(d.get("budget") or {}))


def _fleet(inputs: CapacityInputs, sessions_per_account: int) -> tuple[int, str, dict]:
    available = [a for a in inputs.accounts if a.available]
    account_cap = len(available) * sessions_per_account
    host_free = 0
    for h in inputs.hosts:
        if not h.enabled or h.healthy is not True:
            continue
        host_free += UNBOUNDED if h.max is None else max(0, h.max - h.active)
    global_free = max(0, inputs.global_cap - inputs.global_active)
    candidates = {
        "runner": inputs.runner_ceiling,
        "account": account_cap,
        "host": host_free + inputs.runner_in_flight,
        "global": global_free + inputs.runner_in_flight,
    }
    limiting = min(candidates, key=lambda k: (candidates[k], ("runner", "account", "host", "global").index(k)))
    budget = {"accountsAvailable": len(available), "accountsTotal": len(inputs.accounts),
              "sessionsPerAccount": sessions_per_account, "accountCap": account_cap,
              "hostFree": min(host_free, UNBOUNDED), "globalFree": global_free,
              "runnerCeiling": inputs.runner_ceiling, "runnerInFlight": inputs.runner_in_flight}
    return max(0, candidates[limiting]), limiting, budget


def _split(fleet: int, products: list[ProductCapacity]) -> dict[str, int]:
    working = [p for p in products if p.dispatch != "off" and p.eligible > 0]
    caps: dict[str, int] = {}
    for p in working:
        if p.capacity_mode == "pinned":
            caps[p.slug] = min(p.pin, p.eligible)
            fleet -= caps[p.slug]
    auto = [p for p in working if p.capacity_mode != "pinned"]
    for p in auto:
        caps[p.slug] = 0
    changed = True
    while fleet > 0 and changed:
        changed = False
        for p in auto:
            if fleet <= 0:
                break
            if caps[p.slug] < p.eligible:
                caps[p.slug] += 1
                fleet -= 1
                changed = True
    for p in working:
        caps[p.slug] = max(caps[p.slug], p.in_flight)
    return caps


def plan_capacity(inputs: CapacityInputs, *, push_level: str, sessions_per_account: int = 2,
                  previous: CapacityPlan | None = None) -> CapacityPlan:
    if push_level not in PUSH_LEVELS:
        push_level = "steady"
    reserve = RESERVE_PCT[push_level]
    fleet, limiting, budget = _fleet(inputs, sessions_per_account)
    budget["reservePct"] = reserve
    ordered = product_order(inputs.products)
    proposed = _split(fleet, ordered)
    reasons: list[str] = []
    if fleet == 0 and limiting == "account":
        resets = sorted(a.next_reset_at for a in inputs.accounts if a.next_reset_at)
        reasons.append("no account has headroom under the %d%% reserve%s" % (
            reserve, f"; earliest reset {resets[0]}" if resets else ""))
    else:
        reasons.append(f"fleet {fleet}: limited by {limiting} "
                       f"(accounts {budget['accountsAvailable']}×{sessions_per_account}, "
                       f"host free {budget['hostFree']}, global free {budget['globalFree']}, "
                       f"runner ceiling {inputs.runner_ceiling})")
    per_product: dict[str, int] = {}
    pending: dict[str, int] = {}
    prev_caps = previous.per_product if previous else {}
    prev_pending = previous.pending if previous else {}
    by_slug = {p.slug: p for p in ordered}
    for slug, value in proposed.items():
        p = by_slug[slug]
        current = prev_caps.get(slug, p.pin)
        if p.capacity_mode == "pinned" or value == current or prev_pending.get(slug) == value:
            per_product[slug] = value
            reasons.append(f"{slug}: {value} (rank {p.rank if p.rank is not None else '—'}, "
                           f"{'pinned' if p.capacity_mode == 'pinned' else 'auto'}, {p.eligible} eligible)")
        else:
            per_product[slug] = current
            pending[slug] = value
            reasons.append(f"{slug}: {current} → {value} pending confirmation next tick")
    return CapacityPlan(push_level=push_level, fleet_concurrency=fleet, per_product=per_product,
                        pending=pending, budget=budget, reasons=reasons, limiting=limiting)
