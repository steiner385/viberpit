"""Pure capacity planner (spec 2026-09-19 §6.2). No I/O; table-tested."""
from __future__ import annotations

from dataclasses import dataclass, field

PUSH_LEVELS: tuple[str, ...] = ("conserve", "steady", "max")
RESERVE_PCT: dict[str, int] = {"conserve": 40, "steady": 20, "max": 5}
UNBOUNDED = 10**6


def normalise_push_level(level: str | None) -> str:
    """A push level normalised to a member of ``PUSH_LEVELS``: anything else
    (``None``, an absent row, a stale enum value) reads as ``steady`` — the ONE
    normaliser every caller (the governor config reader via ``push_level_of``,
    ``plan_from_dict``, ``plan_capacity``) shares, so they all derive the same
    default reserve (spec 2026-09-20 D4)."""
    return level if level in PUSH_LEVELS else "steady"


def push_level_of(gov: dict) -> str:
    """The governor row's push level, normalised: anything that is not a member
    of ``PUSH_LEVELS`` (an absent row, a stale enum value) reads as ``steady`` —
    the ONE normaliser the readout and the accounts list share (spec 2026-09-20
    D4), so both derive the same default reserve."""
    return normalise_push_level(gov.get("push_level"))


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
    pipeline: int = 0; pipeline_cap: int | None = None


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
    # Consecutive ticks each `pending` value has been proposed (hysteresis
    # bookkeeping). Persisted inside `budget` — see `plan_from_dict`.
    pending_streak: dict[str, int] = field(default_factory=dict)


def product_order(products) -> list[ProductCapacity]:
    return sorted(products, key=lambda p: (p.rank is None, p.rank or 0, p.created_at))


def plan_from_dict(d: dict | None) -> CapacityPlan | None:
    if not d:
        return None
    return CapacityPlan(push_level=normalise_push_level(d.get("pushLevel")),
                        fleet_concurrency=int(d.get("fleetConcurrency") or 0),
                        per_product={k: int(v) for k, v in (d.get("perProduct") or {}).items()},
                        pending={k: int(v) for k, v in (d.get("pending") or {}).items()},
                        budget=dict(d.get("budget") or {}),
                        # The store has no dedicated `limiting` column, so the writer
                        # (CapacityGovernorLoop) smuggles it into `budget`; restore it
                        # here rather than leave every reader of a round-tripped plan
                        # seeing the CapacityPlan default ("none").
                        limiting=str((d.get("budget") or {}).get("limiting") or "none"),
                        # Same story for the hysteresis streak: no column, so the
                        # writer carries it in `budget` (`budget.pendingStreak`).
                        pending_streak={k: int(v) for k, v in
                                        ((d.get("budget") or {}).get("pendingStreak") or {}).items()})


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
    raw_fleet = max(0, candidates[limiting])
    budget = {"accountsAvailable": len(available), "accountsTotal": len(inputs.accounts),
              "sessionsPerAccount": sessions_per_account, "accountCap": account_cap,
              "hostFree": min(host_free, UNBOUNDED), "globalFree": global_free,
              "runnerCeiling": inputs.runner_ceiling, "runnerInFlight": inputs.runner_in_flight,
              "rawFleet": raw_fleet}
    # Never below the runner's own in-flight count — the fleet-layer twin of
    # `_split`'s per-product floor (fix round 2, N2). Without this, a live
    # plan can compute e.g. fleet 0 when every account is at its reserve
    # ceiling even though the runner already holds 2 sessions:
    # `decide_implementation` short-circuits on `cap == 0` before it ever
    # reaches the `occupied` adjustment, so a session the runner already
    # reserved gets refused capacity on its next claim revalidation and
    # `_automation_before_execution` raises StaleOwnership on the refusal.
    # `limiting` is left naming the layer that WOULD have bound (unfloored) —
    # the floor is a safety clamp on the number, not a change of cause.
    # `floor` is only emitted when it actually changed the number (deferred-now,
    # #2040): `"floor": inputs.runner_in_flight` unconditionally made every plan
    # carry a floor value even when `rawFleet` already cleared it, which reads as
    # "the floor is always binding" to any consumer that doesn't also compare it
    # against `rawFleet` itself.
    if raw_fleet < inputs.runner_in_flight:
        budget["floor"] = inputs.runner_in_flight
    return max(raw_fleet, inputs.runner_in_flight), limiting, budget


def pipeline_full(p: ProductCapacity) -> bool:
    """Spec 2026-09-20 D13: at or over its pipeline cap (blank cap = never)."""
    return p.pipeline_cap is not None and p.pipeline >= p.pipeline_cap


def _plannable_eligible(p: ProductCapacity) -> int:
    # A pipeline-full product cannot take a new claim (admission refuses it), so
    # planning slots for it would leave them idle: it is planned as eligible 0
    # and keeps only its in-flight floor; the freed slots go to the next rank.
    return 0 if pipeline_full(p) else p.eligible


def _split(fleet: int, products: list[ProductCapacity]) -> dict[str, int]:
    # A product with live claims stays in the plan even with an empty backlog
    # (`eligible == 0`): its cap is its in-flight floor, so it gets no
    # round-robin slots (`caps < eligible` is false) but is not dropped from
    # `per_product` either — dropping it would let a later, unfloored read
    # treat its cap as absent rather than as "its current in-flight count".
    # An empty backlog with NO in-flight claim still gets nothing.
    working = [p for p in products
               if p.dispatch != "off" and (_plannable_eligible(p) > 0 or p.in_flight > 0)]
    caps: dict[str, int] = {}
    for p in working:
        if p.capacity_mode == "pinned":
            caps[p.slug] = max(min(p.pin, _plannable_eligible(p)), p.in_flight)
        else:
            caps[p.slug] = p.in_flight
        fleet -= caps[p.slug]
    auto = [p for p in working if p.capacity_mode != "pinned"]
    changed = True
    while fleet > 0 and changed:
        changed = False
        for p in auto:
            if fleet <= 0:
                break
            if caps[p.slug] < _plannable_eligible(p):
                caps[p.slug] += 1
                fleet -= 1
                changed = True
    return caps


DEFAULT_HYSTERESIS_TICKS = 2


def plan_capacity(inputs: CapacityInputs, *, push_level: str, sessions_per_account: int = 2,
                  hysteresis_ticks: int = DEFAULT_HYSTERESIS_TICKS,
                  previous: CapacityPlan | None = None) -> CapacityPlan:
    push_level = normalise_push_level(push_level)
    reserve = RESERVE_PCT[push_level]
    fleet, limiting, budget = _fleet(inputs, sessions_per_account)
    budget["reservePct"] = reserve
    budget["pipeline"] = {p.slug: {"open": p.pipeline, "cap": p.pipeline_cap}
                          for p in inputs.products if p.pipeline_cap is not None}
    ordered = product_order(inputs.products)
    # `fleet` is floored at ALL of the runner's in-flight claims, but `_split` only
    # deducts the claims of products it plans (`working`): a claim on an off
    # product, or a pre-flip one with no product at all, holds fleet capacity
    # without being subtracted, so the per-product caps can sum to more than the
    # fleet by that count. Deliberately left optimistic (#2040): admission's own
    # runner-ceiling gate (`runner_ceiling_full`) still binds every NEW claim
    # against the true fleet count, so the surplus is never actually spawned.
    proposed = _split(fleet, ordered)
    reasons: list[str] = []
    floored = budget["rawFleet"] < inputs.runner_in_flight
    if fleet == 0 and limiting == "account":
        resets = sorted(a.next_reset_at for a in inputs.accounts if a.next_reset_at)
        reasons.append("no account has headroom under its reserve (push level %d%%)%s" % (
            reserve, f"; earliest reset {resets[0]}" if resets else ""))
    else:
        reason = (f"fleet {fleet}: limited by {limiting} "
                 f"(accounts {budget['accountsAvailable']}×{sessions_per_account}, "
                 f"host free {budget['hostFree']}, global free {budget['globalFree']}, "
                 f"runner ceiling {inputs.runner_ceiling})")
        if floored:
            reason += (f"; floored at the runner's {inputs.runner_in_flight} in-flight "
                       f"(would have been {budget['rawFleet']})")
        reasons.append(reason)
    for p in ordered:
        # `p.slug in proposed`: a full product with eligible 0 AND in_flight 0
        # never enters `working` (`_split`), so it never enters `proposed` either —
        # nothing was planned for it and no reason line explains a redistribution
        # that has nothing to redistribute.
        if pipeline_full(p) and p.dispatch != "off" and p.slug in proposed:
            reasons.append(f"{p.slug}: pipeline full ({p.pipeline}/{p.pipeline_cap} claims and PRs), "
                           "its slots go to the next rank")
    per_product: dict[str, int] = {}
    pending: dict[str, int] = {}
    prev_caps = previous.per_product if previous else {}
    prev_pending = previous.pending if previous else {}
    prev_streak = previous.pending_streak if previous else {}
    pending_streak: dict[str, int] = {}
    by_slug = {p.slug: p for p in ordered}
    for slug, value in proposed.items():
        p = by_slug[slug]
        current = prev_caps.get(slug, p.pin)
        # Hysteresis (spec §6.2): a change applies only once the SAME new value has
        # been proposed on `hysteresis_ticks` consecutive ticks, this one included.
        # The streak restarts at 1 the moment the proposal changes, so a flapping
        # source never accumulates confirmation across different values.
        streak = prev_streak.get(slug, 1) + 1 if prev_pending.get(slug) == value else 1
        if p.capacity_mode == "pinned" or value == current or streak >= max(1, hysteresis_ticks):
            per_product[slug] = value
            reasons.append(f"{slug}: {value} (rank {p.rank if p.rank is not None else '—'}, "
                           f"{'pinned' if p.capacity_mode == 'pinned' else 'auto'}, {p.eligible} eligible)")
        else:
            # The carried value is floored at the product's current in-flight
            # count too (D8's own floor, not just the confirmed-value path
            # above): hysteresis must never carry forward a stale cap LOWER
            # than what the product already holds, or an occupied claim would
            # read a shrunk cap on the seam that still checks it directly.
            carried = max(current, p.in_flight)
            per_product[slug] = carried
            pending[slug] = value
            pending_streak[slug] = streak
            reasons.append(f"{slug}: {carried} → {value} pending confirmation next tick")
    return CapacityPlan(push_level=push_level, fleet_concurrency=fleet, per_product=per_product,
                        pending=pending, pending_streak=pending_streak, budget=budget,
                        reasons=reasons, limiting=limiting)


#: The plan's `limiting` layer as a reader names it (#2684).
_LIMITING_NAME = {"runner": "the runner ceiling", "account": "accounts", "host": "hosts",
                  "global": "the global cap"}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def fleet_reason(plan: dict | None) -> str | None:
    """One sentence naming why a plan's ``fleetConcurrency`` is what it is — the
    layer that bound it, that layer's own numbers, and the in-flight floor when
    it lifted the number (#2684). Reads the plan's wire dict (``budget`` carries
    ``limiting``, see ``plan_from_dict``), so the readout, admission's refusal
    text and the panel all say the same thing. None for no/malformed plan."""
    if not plan:
        return None
    try:
        budget = plan.get("budget") or {}
        fleet = int(plan["fleetConcurrency"])
        limiting = str(budget.get("limiting") or "none")
        if limiting == "account":
            detail = (f"{budget['accountsAvailable']} of {budget['accountsTotal']} with headroom "
                      f"× {budget['sessionsPerAccount']} = {budget['accountCap']}")
        elif limiting == "host":
            detail = f"{budget['hostFree']} free + {budget['runnerInFlight']} in flight"
        elif limiting == "global":
            detail = f"{budget['globalFree']} free + {budget['runnerInFlight']} in flight"
        else:
            detail = None
        text = f"capacity plan: fleet {fleet}, limited by {_LIMITING_NAME.get(limiting, limiting)}"
        if detail:
            text += f" ({detail})"
        if budget.get("floor") is not None:
            text += (f"; floored at the runner's {_plural(int(budget['floor']), 'in-flight claim')} "
                     f"(would have been {budget['rawFleet']})")
        return text
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def product_cap_note(slug: str, policy: dict, plan: dict | None, *, overlaid: bool) -> str:
    """Where a product's effective in-flight cap came from (#2684): the live
    plan's split (``overlaid``), an operator pin, or the stored value of an
    auto product no live plan is governing. ``policy`` is the EFFECTIVE policy."""
    cap = policy["max_in_flight"]
    if policy["dispatch"] == "off":
        return "dispatch off"
    if overlaid and plan:
        limiting = str((plan.get("budget") or {}).get("limiting") or "none")
        return (f"capacity plan gives {slug} {cap} of fleet {plan.get('fleetConcurrency')} "
                f"(limited by {_LIMITING_NAME.get(limiting, limiting)})")
    if policy["capacity_mode"] == "pinned":
        return f"pinned at {cap}"
    return f"stored {cap} (auto; no live capacity plan)"
