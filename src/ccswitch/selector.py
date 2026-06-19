"""Pure account selection: exclude accounts at/above ceiling, rank by headroom."""
from dataclasses import dataclass
from datetime import datetime

from .config import Config
from .oauth import Usage


@dataclass
class Selection:
    name: str


class AllAccountsBusy(Exception):
    def __init__(self, earliest_reset: datetime | None):
        super().__init__("all accounts at/above usage ceiling")
        self.earliest_reset = earliest_reset


def select_best(usages: dict[str, Usage | None], reserved: set[str],
                cfg: Config, now: datetime, exclude: set[str] = frozenset()) -> str:
    ceiling = cfg.hard_ceiling * 100.0
    candidates: list[tuple[float, float, str]] = []
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
        candidates.append((u.session_pct, u.weekly_pct, name))
    if not candidates:
        raise AllAccountsBusy(min(busy_resets) if busy_resets else None)
    candidates.sort()  # lowest session_pct, then weekly_pct, then name
    return candidates[0][2]
