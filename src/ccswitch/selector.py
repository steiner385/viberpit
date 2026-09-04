"""Pure account selection: exclude accounts at/above ceiling, rank by perishability."""
from collections.abc import Mapping, Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config
from .oauth import Usage

# Floor on time-to-reset so an imminent/just-passed reset can't divide-by-zero or
# produce a runaway score; one minute keeps near-reset accounts strongly preferred
# without the ranking becoming numerically unstable.
_TIME_FLOOR_HOURS = 1.0 / 60.0

# Sorts an at-ceiling account with no known reset behind every account that has one.
_NO_RESET = datetime.max.replace(tzinfo=timezone.utc)


@dataclass
class Selection:
    name: str


@dataclass
class Picked:
    name: str
    optimistic: bool = False


class AllAccountsBusy(Exception):
    def __init__(self, earliest_reset: datetime | None, kind: str = "at_ceiling"):
        super().__init__("all accounts at/above usage ceiling")
        self.earliest_reset = earliest_reset
        self.kind = kind

    @property
    def error_data(self) -> dict:
        """Structured payload so the typed exception survives the daemon RPC
        boundary (which otherwise flattens exceptions to type+message). The remote
        client rebuilds ``AllAccountsBusy`` from this so the edge renders a clear
        'all accounts at limit, resets <date>' message instead of a generic 500."""
        return {
            "earliest_reset": self.earliest_reset.isoformat() if self.earliest_reset else None,
            "kind": self.kind,
        }


def _perishability(u: Usage, ceiling: float, now: datetime) -> float:
    """Quota (per hour) that will be wasted if unspent before the binding window resets.

    The account is bound by whichever dimension it hits the ceiling on first (the one
    with the least remaining headroom); that dimension's reset decides how perishable
    the capacity is. A higher score means "use this account now". Fully fresh accounts
    (binding dimension has no active window) score 0 — their capacity isn't perishable
    yet, so they are saved for last.
    """
    session_remaining = ceiling - u.session_pct
    weekly_remaining = ceiling - u.weekly_pct
    if weekly_remaining < session_remaining:
        remaining, reset = weekly_remaining, u.weekly_resets_at
    else:
        remaining, reset = session_remaining, u.session_resets_at
    if reset is None:
        return 0.0
    hours = max((reset - now).total_seconds() / 3600.0, _TIME_FLOOR_HOURS)
    return remaining / hours


def at_ceiling(u: Usage, ceiling: float) -> bool:
    """True when either usage dimension has reached *ceiling* (percent).

    The ONE definition of "this account has no headroom": rank_accounts drops such
    accounts for new picks, and the server's rotation monitor / crash recovery use
    it to decide a live session's account is dead. The weekly dimension counts —
    a session on a weekly-exhausted account 429s on its next turn exactly like a
    session-exhausted one.
    """
    return u.session_pct >= ceiling or u.weekly_pct >= ceiling


@dataclass
class RankedAccount:
    name: str
    rank: int
    reason: str
    optimistic: bool = False


def _reason(composite: float, acct_load: float) -> str:
    """Short, deterministic explanation of an account's standing (for the UI tooltip).

    composite == 0 means the binding dimension has no active reset window — a fresh
    account whose quota isn't perishable yet, so the selector saves it for last.
    """
    if composite <= 0.0:
        return "fresh quota — saved for last"
    return "perishable quota, lowest load" if acct_load == 0.0 else "perishable quota, sharing live load"


def rank_accounts(usages: dict[str, Usage | None], reserved: AbstractSet[str],
                  cfg: Config, now: datetime, exclude: AbstractSet[str] = frozenset(),
                  load: dict[str, float] | None = None,
                  unhealthy: AbstractSet[str] = frozenset(),
                  optimistic_fallback: bool = False,
                  reserves: Mapping[str, float] | None = None,
                  extra_usage_allowed: AbstractSet[str] = frozenset(),
                  ) -> tuple[list[RankedAccount], datetime | None]:
    """Rank all eligible accounts in selection-priority order.

    Returns ``(ranked, earliest_reset)``: ``ranked`` is the priority-ordered list
    (rank 1 = next pick); ``earliest_reset`` is the soonest reset among at-ceiling
    accounts when nothing is eligible (else None). ``select_best`` is the rank-1 name.

    When ``optimistic_fallback=False`` (default), accounts with ``u is None`` are
    excluded entirely — identical to legacy behavior. When ``True``, healthy accounts
    with unreadable usage are appended as a last-resort tier, ranked strictly after
    every readable candidate, ordered by least load.

    ``reserves`` maps account name -> weekly reserve threshold (0-100 pct). An
    account whose ``weekly_pct`` is at/over its reserve (but under the hard
    ceiling) is demoted to a final last-resort tier — ranked after every normal
    and optimistic candidate, least weekly-used first — so the reserve alone can
    never make selection fail (soft fallback).

    ``extra_usage_allowed`` names the accounts the user has opted into spending
    paid "extra usage" credits on. An at-ceiling account in that set whose
    ``Usage.extra_usage_enabled`` is also true upstream is demoted to the FINAL
    tier rather than excluded, so credits are spent only once every free
    candidate — including over-reserve ones — is exhausted. Both halves are
    required: the opt-in without the provider flag would hand out a session that
    dies immediately on rate limits, and the provider flag without the opt-in
    would spend money the user never authorized.
    """
    load = load or {}
    ceiling = cfg.hard_ceiling * 100.0
    candidates: list[tuple[float, float, float, float, str]] = []
    optimistic: list[tuple[float, str]] = []   # (load, name) — last-resort tier
    reserve_blocked: list[tuple[float, str]] = []  # (weekly_pct, name) — soft-fallback tier
    extra_paid: list[tuple[datetime, str]] = []    # (binding reset, name) — final paid tier
    busy_resets: list[datetime] = []
    for name, u in usages.items():
        if name in exclude or name in reserved:
            continue
        if u is None:
            # Usage unreadable. Only an opt-in caller treats a healthy account as a
            # last-resort optimistic pick; legacy callers exclude it entirely.
            if optimistic_fallback and name not in unhealthy:
                optimistic.append((load.get(name, 0.0), name))
            continue
        if at_ceiling(u, ceiling):
            resets = []
            if u.session_pct >= ceiling and u.session_resets_at is not None:
                resets.append(u.session_resets_at)
            if u.weekly_pct >= ceiling and u.weekly_resets_at is not None:
                resets.append(u.weekly_resets_at)
            # Opted into extra usage AND the provider actually has it enabled: the
            # account is spendable, so demote it to the paid tier instead of dropping
            # it — and do NOT count it as busy, since it is a usable candidate.
            if name in extra_usage_allowed and u.extra_usage_enabled:
                extra_paid.append((min(resets) if resets else _NO_RESET, name))
                continue
            if resets:
                busy_resets.append(min(resets))
            continue
        reserve = (reserves or {}).get(name)
        if reserve is not None and u.weekly_pct >= reserve:
            reserve_blocked.append((u.weekly_pct, name))
            continue
        acct_load = load.get(name, 0.0)
        perish = _perishability(u, ceiling, now) if cfg.prefer_soon_reset else 0.0
        composite = perish / (1.0 + cfg.load_sensitivity * acct_load)
        candidates.append((-composite, acct_load, u.session_pct, u.weekly_pct, name))
    candidates.sort()
    optimistic.sort()  # least live load first
    earliest = min(busy_resets) if busy_resets else None
    ranked = [
        RankedAccount(name=c[4], rank=i + 1, reason=_reason(-c[0], c[1]))
        for i, c in enumerate(candidates)
    ]
    ranked += [
        RankedAccount(name=name, rank=len(ranked) + i + 1,
                      reason="usage unverified — optimistic pick", optimistic=True)
        for i, (_, name) in enumerate(optimistic)
    ]
    reserve_blocked.sort()  # least weekly-used first — burn reserved headroom last
    ranked += [
        RankedAccount(name=name, rank=len(ranked) + i + 1,
                      reason="over weekly reserve — fallback")
        for i, (_, name) in enumerate(reserve_blocked)
    ]
    # Soonest free-quota reset first: a session stays pinned to the account it was
    # launched on, so the account that returns to free quota soonest spends the
    # fewest paid credits before it does. (Ties broken by name for determinism.)
    extra_paid.sort()
    ranked += [
        RankedAccount(name=name, rank=len(ranked) + i + 1,
                      reason="at ceiling — burning extra usage credits")
        for i, (_, name) in enumerate(extra_paid)
    ]
    return ranked, earliest


def select_picked(usages: dict[str, Usage | None], reserved: AbstractSet[str],
                  cfg: Config, now: datetime, exclude: AbstractSet[str] = frozenset(),
                  load: dict[str, float] | None = None,
                  unhealthy: AbstractSet[str] = frozenset(),
                  optimistic_fallback: bool = False,
                  reserves: Mapping[str, float] | None = None,
                  extra_usage_allowed: AbstractSet[str] = frozenset()) -> Picked:
    ranked, earliest = rank_accounts(usages, reserved, cfg, now, exclude, load,
                                     unhealthy, optimistic_fallback, reserves=reserves,
                                     extra_usage_allowed=extra_usage_allowed)
    if not ranked:
        kind = "at_ceiling" if earliest is not None else "no_usable_accounts"
        raise AllAccountsBusy(earliest, kind)
    top = ranked[0]
    return Picked(name=top.name, optimistic=top.optimistic)


def select_best(usages: dict[str, Usage | None], reserved: AbstractSet[str],
                cfg: Config, now: datetime, exclude: AbstractSet[str] = frozenset(),
                load: dict[str, float] | None = None,
                reserves: Mapping[str, float] | None = None,
                extra_usage_allowed: AbstractSet[str] = frozenset()) -> str:
    return select_picked(usages, reserved, cfg, now, exclude, load,
                         reserves=reserves,
                         extra_usage_allowed=extra_usage_allowed).name
