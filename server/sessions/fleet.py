"""Host ranking for multi-host session dispatch.

Pure functions only — no DB, no network — so dispatch policy is unit-testable
without two live hosts (spec: "Dispatch logic must be unit testable without two
live hosts").

Repo locality is deliberately **not** an input. ``ensure_clone``
(``src/ccswitch/gitrepos.py``) is lazy and idempotent, so a cold host clones on
demand; filtering on locality would strand nearly every session on dobby and
defeat the offload goal (spec open question 1, resolved 2026-08-21).

Host choice and account choice are independent: spec decision 5 removed
account-to-host affinity, so ``select_account`` runs unchanged and this ranks
hosts as a separate, composable step.
"""
from __future__ import annotations

from dataclasses import dataclass

# Free memory at or above this is "plenty" — more does not score higher.
_MEM_SATURATION_MB = 4096
# Score weights; they sum to 1.0 so a host's base score is bounded by its weight.
_W_HEADROOM, _W_MEM, _W_LOAD = 0.5, 0.3, 0.2
# Used when a probe returned no value for a metric. Deliberately mid-range: an
# unmeasured host should neither win outright nor be effectively excluded.
_UNKNOWN_METRIC = 0.5

__all__ = ["HostCandidate", "rank_hosts", "score_host"]


@dataclass(frozen=True)
class HostCandidate:
    """A snapshot of one host's dispatchability, built from a ``Host`` row."""

    name: str
    enabled: bool
    healthy: bool | None
    weight: int
    active_sessions: int
    max_sessions: int | None
    load_avg: float | None
    mem_available_mb: int | None


def _dispatchable(c: HostCandidate) -> bool:
    """True iff *c* may receive a new session right now.

    ``healthy is None`` (never probed) is NOT dispatchable. Treating unknown as
    up is how you dispatch into a host that has been down since boot.
    """
    if not c.enabled or c.healthy is not True:
        return False
    if c.max_sessions is not None and c.active_sessions >= c.max_sessions:
        return False
    return True


def score_host(c: HostCandidate) -> float:
    """Higher is better. Only meaningful for dispatchable candidates."""
    if c.max_sessions:
        headroom = max(0, c.max_sessions - c.active_sessions) / c.max_sessions
    else:
        headroom = 1.0

    mem = (_UNKNOWN_METRIC if c.mem_available_mb is None
           else min(1.0, c.mem_available_mb / _MEM_SATURATION_MB))

    load = (_UNKNOWN_METRIC if c.load_avg is None
            else 1.0 / (1.0 + max(0.0, c.load_avg)))

    return c.weight * (_W_HEADROOM * headroom + _W_MEM * mem + _W_LOAD * load)


def rank_hosts(candidates: list[HostCandidate]) -> list[str]:
    """Return dispatchable host names, best first.

    Ties break by name so dispatch is deterministic and reproducible in tests.
    """
    usable = [c for c in candidates if _dispatchable(c)]
    usable.sort(key=lambda c: (-score_host(c), c.name))
    return [c.name for c in usable]
