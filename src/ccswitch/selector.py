"""Pure account selection: exclude accounts at/above ceiling, rank by perishability."""
from dataclasses import dataclass
from datetime import datetime

from .config import Config
from .oauth import Usage

# Floor on time-to-reset so an imminent/just-passed reset can't divide-by-zero or
# produce a runaway score; one minute keeps near-reset accounts strongly preferred
# without the ranking becoming numerically unstable.
_TIME_FLOOR_HOURS = 1.0 / 60.0


@dataclass
class Selection:
    name: str


class AllAccountsBusy(Exception):
    def __init__(self, earliest_reset: datetime | None):
        super().__init__("all accounts at/above usage ceiling")
        self.earliest_reset = earliest_reset


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


def select_best(usages: dict[str, Usage | None], reserved: set[str],
                cfg: Config, now: datetime, exclude: set[str] = frozenset(),
                load: dict[str, float] | None = None) -> str:
    load = load or {}
    ceiling = cfg.hard_ceiling * 100.0
    candidates: list[tuple[float, float, float, float, str]] = []
    busy_resets: list[datetime] = []
    for name, u in usages.items():
        if name in exclude or name in reserved or u is None:
            continue
        at_ceiling = u.session_pct >= ceiling or u.weekly_pct >= ceiling
        if at_ceiling:
            # only the dimension(s) actually at ceiling contribute a reset time;
            # a reset can be None (0%/no active window) — exclude those
            resets = []
            if u.session_pct >= ceiling and u.session_resets_at is not None:
                resets.append(u.session_resets_at)
            if u.weekly_pct >= ceiling and u.weekly_resets_at is not None:
                resets.append(u.weekly_resets_at)
            if resets:
                busy_resets.append(min(resets))
            continue
        acct_load = load.get(name, 0.0)
        perish = _perishability(u, ceiling, now) if cfg.prefer_soon_reset else 0.0
        # Multiplicative discount: live load shrinks an account's score, so a busy
        # perishable account can drop below a less-busy one. Bounded (1 + λ·load >= 1)
        # so no divide-by-zero / NaN; with load={} this reduces to the legacy key.
        composite = perish / (1.0 + cfg.load_sensitivity * acct_load)
        # Sort ascending: most-perishable-after-discount first (-composite); then raw
        # load so spreading still applies when perishability can't differentiate
        # (prefer_soon_reset off or all-fresh -> every composite is 0); then the
        # legacy lowest-usage ordering; then name for determinism.
        candidates.append((-composite, acct_load, u.session_pct, u.weekly_pct, name))
    if not candidates:
        raise AllAccountsBusy(min(busy_resets) if busy_resets else None)
    candidates.sort()
    return candidates[0][4]
