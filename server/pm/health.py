"""Roadmap drift readout (5S plan, Sustain). Pure: the API loads rows and
calls `roadmap_health`; nothing here decides or mutates."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable


@dataclass(frozen=True)
class RoadmapHealth:
    unparented_capabilities: tuple[str, ...]
    unlinked_initiatives: tuple[str, ...]
    stale_proposed: tuple[str, ...]
    stale_inbox: int
    unindexed_docs: tuple[tuple[str, str], ...] = ()


def roadmap_health(*, capabilities: Iterable[tuple[str, str | None, str | None, datetime | None]],
                   initiatives: Iterable[tuple[str, int, bool, bool]],
                   proposals: Iterable[datetime | None],
                   now: datetime, stale_days: int = 30, inbox_days: int = 7,
                   unindexed_docs: Iterable[tuple[str, str]] = ()) -> RoadmapHealth:
    caps = list(capabilities)
    has_child = {p for _, p, _, _ in caps if p}
    tree_in_use = any(p is None and s in has_child for s, p, _, _ in caps)
    unparented = tuple(sorted(s for s, p, _, _ in caps
                              if tree_in_use and p is None and s not in has_child))
    # A program (`prog`) is judged by its phases, never itself — and a `done`
    # initiative (leaf or program) is finished, not drifting, so neither
    # belongs in the "needs a capability" readout.
    unlinked = tuple(sorted(s for s, n, prog, done in initiatives if n == 0 and not prog and not done))
    cutoff = now - timedelta(days=stale_days)
    stale = tuple(sorted(s for s, _, st, upd in caps
                         if st == "proposed" and upd is not None and _aware(upd) < cutoff))
    inbox_cutoff = now - timedelta(days=inbox_days)
    stale_inbox = sum(1 for c in proposals if c is not None and _aware(c) < inbox_cutoff)
    unindexed = tuple(sorted(unindexed_docs))
    return RoadmapHealth(unparented, unlinked, stale, stale_inbox, unindexed)


def _aware(dt: datetime) -> datetime:
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
