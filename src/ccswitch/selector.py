"""Pure account selection: filter to eligible accounts, then rank by perishability.

The ranking is provider-neutral: it asks each account's :class:`QuotaSnapshot`
which window BINDS (runs out first for the model in question) and reasons about
that one window. The predecessor hard-coded a session/weekly pair, which forced
single-window providers to fabricate a duplicate dimension just to be rankable.

Order of operations matters and is fixed: **filter first, score second.** Engine,
provider and model eligibility are yes/no facts about whether an account can serve
the request at all; an account that cannot must not appear in a tier, must not
contribute to ``earliest_reset`` (it is not "busy", it is inapplicable), and must
not be counted when deciding whether selection failed for lack of headroom.
"""
from collections.abc import Mapping, Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config
from .quota import QuotaSnapshot

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


def binding_window(u: QuotaSnapshot, model: str | None = None):
    """The window that decides this account's headroom for *model*.

    One indirection so every caller — perishability, ceiling checks, the rotation
    monitor, the UI's "resets at" — agrees on which window is the constraint.
    Returns None when the snapshot reports no applicable window (an unavailable
    read, or a provider that reported nothing).
    """
    return u.binding_window(model)


def usage_known(u: QuotaSnapshot | None) -> bool:
    """True only when the snapshot carries trustworthy windows.

    ``None`` (the fetch never produced anything) and a snapshot whose
    ``availability`` is not ``available`` are the SAME condition to the selector:
    the account's headroom is unknown. Both are excluded from the scored tiers
    unless the caller opted into the optimistic fallback — the conservative
    default the rotation and spawn paths depend on.
    """
    return u is not None and u.is_available and bool(u.windows)


def _perishability(u: QuotaSnapshot, ceiling: float, now: datetime,
                   model: str | None = None) -> float:
    """Quota (per hour) that will be wasted if unspent before the binding window resets.

    The account is bound by whichever window it hits the ceiling on first (the one
    with the least remaining headroom); that window's reset decides how perishable
    the capacity is. A higher score means "use this account now". Fully fresh
    accounts (binding window has no active reset) score 0 — their capacity isn't
    perishable yet, so they are saved for last.
    """
    w = binding_window(u, model)
    if w is None or w.resets_at is None:
        return 0.0
    hours = max((w.resets_at - now).total_seconds() / 3600.0, _TIME_FLOOR_HOURS)
    return w.remaining_pct(ceiling) / hours


def at_ceiling(u: QuotaSnapshot, ceiling: float, model: str | None = None) -> bool:
    """True when ANY window that meters *model* has reached *ceiling* (percent).

    The ONE definition of "this account has no headroom": rank_accounts drops such
    accounts for new picks, and the server's rotation monitor / crash recovery use
    it to decide a live session's account is dead. Every window counts — a session
    on a weekly-exhausted account 429s on its next turn exactly like a
    session-exhausted one.
    """
    return any(w.used_pct >= ceiling for w in u.windows_for(model))


def exhausted(u: QuotaSnapshot, ceiling: float, *, extra_usage_allowed: bool,
              model: str | None = None) -> bool:
    """at_ceiling AND no paid overflow to fall back on. The rotation monitor's
    definition of a dead account; mirrors rank_accounts' extra_paid tier."""
    return (at_ceiling(u, ceiling, model)
            and not (extra_usage_allowed and u.extra_usage_enabled))


def reserve_pct(u: QuotaSnapshot) -> float | None:
    """The usage percentage a per-account RESERVE is measured against.

    The reserve guards the long-horizon allowance, so it reads the snapshot's
    longest window — Anthropic's weekly (which the setting is still named after)
    and, for a single-allowance provider, that one window. None when the snapshot
    has no window to measure.
    """
    w = u.reserve_window()
    return None if w is None else w.used_pct


@dataclass
class RankedAccount:
    name: str
    rank: int
    reason: str
    optimistic: bool = False


def _reason(composite: float, acct_load: float) -> str:
    """Short, deterministic explanation of an account's standing (for the UI tooltip).

    composite == 0 means the binding window has no active reset — a fresh account
    whose quota isn't perishable yet, so the selector saves it for last.
    """
    if composite <= 0.0:
        return "fresh quota — saved for last"
    return "perishable quota, lowest load" if acct_load == 0.0 else "perishable quota, sharing live load"


def eligible_names(names, *, providers: Mapping[str, str] | None = None,
                   engines: Mapping[str, str | None] | None = None,
                   require_provider: str | None = None,
                   require_engine: str | None = None,
                   default_engine: str | None = None) -> list[str]:
    """The subset of *names* that can serve a request for the given engine/provider.

    Applied BEFORE any scoring. An account filtered out here is inapplicable, not
    busy: it never lands in a tier and never contributes a reset time, so a
    Codex-only request finding no Codex account reports ``no_usable_accounts``
    rather than an Anthropic account's reset date.

    ``engines`` maps a name to its DECLARED engine, where ``None`` means "resolve
    through the engine registry" — which every account predating the manifest
    does. The CALLER supplies what that resolves to via ``default_engine``,
    because the engine vocabulary lives in the engine registry
    (``server.sessions.engine``) and this module must not hold a second copy of
    it. With ``default_engine`` unset, an undeclared account matches no
    ``require_engine`` at all, which is the safe direction: it is never dispatched
    to an engine nobody claimed it could run.
    """
    out = []
    for name in names:
        if require_provider is not None and (providers or {}).get(name) != require_provider:
            continue
        if require_engine is not None:
            declared = (engines or {}).get(name) or default_engine
            if declared != require_engine:
                continue
        out.append(name)
    return out


def rank_accounts(usages: dict[str, QuotaSnapshot | None], reserved: AbstractSet[str],
                  cfg: Config, now: datetime, exclude: AbstractSet[str] = frozenset(),
                  load: dict[str, float] | None = None,
                  unhealthy: AbstractSet[str] = frozenset(),
                  optimistic_fallback: bool = False,
                  reserves: Mapping[str, float] | None = None,
                  extra_usage_allowed: AbstractSet[str] = frozenset(),
                  *,
                  model: str | None = None,
                  providers: Mapping[str, str] | None = None,
                  engines: Mapping[str, str | None] | None = None,
                  require_provider: str | None = None,
                  require_engine: str | None = None,
                  default_engine: str | None = None,
                  ) -> tuple[list[RankedAccount], datetime | None]:
    """Rank all eligible accounts in selection-priority order.

    Returns ``(ranked, earliest_reset)``: ``ranked`` is the priority-ordered list
    (rank 1 = next pick); ``earliest_reset`` is the soonest reset among at-ceiling
    accounts when nothing is eligible (else None). ``select_best`` is the rank-1 name.

    Engine/provider/model filtering runs FIRST (``eligible_names``); everything
    below scores only what survived.

    When ``optimistic_fallback=False`` (default), accounts with unknown usage are
    excluded entirely — identical to legacy behavior. When ``True``, healthy
    accounts with unreadable usage are appended as a last-resort tier, ranked
    strictly after every readable candidate, ordered by least load.

    ``reserves`` maps account name -> reserve threshold (0-100 pct) on the
    account's longest window. An account at/over its reserve (but under the hard
    ceiling) is demoted to a final last-resort tier — ranked after every normal
    and optimistic candidate, least-used first — so the reserve alone can never
    make selection fail (soft fallback).

    ``extra_usage_allowed`` names the accounts the user has opted into spending
    paid "extra usage" credits on. An at-ceiling account in that set whose
    ``QuotaSnapshot.extra_usage_enabled`` is also true upstream is demoted to the
    FINAL tier rather than excluded, so credits are spent only once every free
    candidate — including over-reserve ones — is exhausted. Both halves are
    required: the opt-in without the provider flag would hand out a session that
    dies immediately on rate limits, and the provider flag without the opt-in
    would spend money the user never authorized.
    """
    load = load or {}
    ceiling = cfg.hard_ceiling * 100.0
    candidates: list[tuple[float, float, float, str]] = []
    optimistic: list[tuple[float, str]] = []   # (load, name) — last-resort tier
    reserve_blocked: list[tuple[float, str]] = []  # (reserve pct, name) — soft-fallback tier
    extra_paid: list[tuple[datetime, str]] = []    # (binding reset, name) — final paid tier
    busy_resets: list[datetime] = []
    # Filter BEFORE scoring: engine/provider eligibility is not a usage question.
    applicable = set(eligible_names(
        usages, providers=providers, engines=engines,
        require_provider=require_provider, require_engine=require_engine,
        default_engine=default_engine))
    for name, u in usages.items():
        if name not in applicable:
            continue
        if name in exclude or name in reserved:
            continue
        if not usage_known(u):
            # Usage unreadable (never fetched, or the snapshot says unknown /
            # auth_error). Only an opt-in caller treats a healthy account as a
            # last-resort optimistic pick; legacy callers exclude it entirely.
            if optimistic_fallback and name not in unhealthy:
                optimistic.append((load.get(name, 0.0), name))
            continue
        if u is None:  # unreachable — usage_known() already excluded it; narrows the type
            continue
        if at_ceiling(u, ceiling, model):
            resets = [w.resets_at for w in u.windows_at_or_over(ceiling, model)
                      if w.resets_at is not None]
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
        used_on_reserve_window = reserve_pct(u)
        if (reserve is not None and used_on_reserve_window is not None
                and used_on_reserve_window >= reserve):
            reserve_blocked.append((used_on_reserve_window, name))
            continue
        acct_load = load.get(name, 0.0)
        perish = _perishability(u, ceiling, now, model) if cfg.prefer_soon_reset else 0.0
        composite = perish / (1.0 + cfg.load_sensitivity * acct_load)
        # Tie-break on the BINDING window's utilisation, then on name. The key is
        # deliberately FIXED-WIDTH and provider-independent: a per-provider tuple
        # of every window's used_pct made a 1-window Copilot key a strict PREFIX
        # of a 2-window Anthropic one, and Python orders a prefix first — so with
        # composite and load bit-equal, Copilot at 40% sorted ahead of an
        # Anthropic account at session 40% / weekly 0% purely because it has fewer
        # windows. (A differential sweep over 200k randomised fleets found this in
        # 0.15% of MIXED-provider fleets and nowhere else.) The binding window is
        # already the one number the rest of this module reasons about.
        binding = binding_window(u, model)
        candidates.append((-composite, acct_load,
                           binding.used_pct if binding is not None else 0.0, name))
    candidates.sort()
    optimistic.sort()  # least live load first
    earliest = min(busy_resets) if busy_resets else None
    ranked = [
        RankedAccount(name=c[3], rank=i + 1, reason=_reason(-c[0], c[1]))
        for i, c in enumerate(candidates)
    ]
    ranked += [
        RankedAccount(name=name, rank=len(ranked) + i + 1,
                      reason="usage unverified — optimistic pick", optimistic=True)
        for i, (_, name) in enumerate(optimistic)
    ]
    reserve_blocked.sort()  # least-used first — burn reserved headroom last
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


def select_picked(usages: dict[str, QuotaSnapshot | None], reserved: AbstractSet[str],
                  cfg: Config, now: datetime, exclude: AbstractSet[str] = frozenset(),
                  load: dict[str, float] | None = None,
                  unhealthy: AbstractSet[str] = frozenset(),
                  optimistic_fallback: bool = False,
                  reserves: Mapping[str, float] | None = None,
                  extra_usage_allowed: AbstractSet[str] = frozenset(),
                  **filters) -> Picked:
    ranked, earliest = rank_accounts(usages, reserved, cfg, now, exclude, load,
                                     unhealthy, optimistic_fallback, reserves=reserves,
                                     extra_usage_allowed=extra_usage_allowed, **filters)
    if not ranked:
        kind = "at_ceiling" if earliest is not None else "no_usable_accounts"
        raise AllAccountsBusy(earliest, kind)
    top = ranked[0]
    return Picked(name=top.name, optimistic=top.optimistic)


def select_best(usages: dict[str, QuotaSnapshot | None], reserved: AbstractSet[str],
                cfg: Config, now: datetime, exclude: AbstractSet[str] = frozenset(),
                load: dict[str, float] | None = None,
                reserves: Mapping[str, float] | None = None,
                extra_usage_allowed: AbstractSet[str] = frozenset(),
                **filters) -> str:
    return select_picked(usages, reserved, cfg, now, exclude, load,
                         reserves=reserves,
                         extra_usage_allowed=extra_usage_allowed, **filters).name
