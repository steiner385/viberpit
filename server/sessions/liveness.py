"""Pure liveness derivation from a heartbeat timestamp (spec 002 FR-102).

No I/O — accepts pre-fetched values and returns a frozen ``Liveness`` dataclass.
Callers snapshot ``HostHeartbeat.beat_at`` and pass it here; the derivation logic
is isolated so it can be tested with synthetic clocks and reused by any layer
(API serialisation, frontend polling, health endpoints) without coupling to DB.

States:
- ``fresh``       — beat within 2x interval, OR no row yet (NFR-3: absence of
                    evidence is not evidence of death — rollout safety).
- ``stale``       — beat between 2x interval and dead_after_s.
- ``unreachable`` — beat older than dead_after_s, not yet declared dead.
- ``dead``        — explicitly declared via declare_dead (fence bump observed).

Clock skew: if beat_at is in the future (writer clock ahead of reader) the age
is negative — treated as fresh so skew never triggers a false demotion.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

LivenessState = Literal["fresh", "stale", "unreachable", "dead"]


@dataclass(frozen=True)
class Liveness:
    """Derived liveness for a single host (immutable — safe to cache and log)."""
    state: LivenessState
    last_confirmed_alive_at: datetime | None


def derive_liveness(
    beat_at: datetime | None,
    now: datetime,
    *,
    interval_s: float = 10.0,
    dead_after_s: float = 300.0,
    declared_dead: bool = False,
) -> Liveness:
    """Derive liveness state from a heartbeat timestamp.

    Args:
        beat_at:       Last beat_at from HostHeartbeat, or None when no row exists.
        now:           Caller-supplied clock (enables deterministic tests).
        interval_s:    Expected beat interval; fresh threshold is 2x this value.
        dead_after_s:  Seconds without a beat before unreachable → dead transition.
        declared_dead: True when the primary has bumped the fence (FR-202).
    """
    if declared_dead:
        return Liveness(state="dead", last_confirmed_alive_at=beat_at)

    if beat_at is None:
        # NFR-3: no row = host has never registered; treat as fresh-unknown
        # so a rolling deploy never diagnoses a not-yet-started worker as dead.
        return Liveness(state="fresh", last_confirmed_alive_at=None)

    age = (now - beat_at).total_seconds()

    if age <= 2 * interval_s:
        # Covers negative age (clock skew — writer ahead of reader) as well as
        # genuinely fresh beats.
        state: LivenessState = "fresh"
    elif age < dead_after_s:
        state = "stale"
    else:
        state = "unreachable"

    return Liveness(state=state, last_confirmed_alive_at=beat_at)
