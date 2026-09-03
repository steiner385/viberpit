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
- ``unknown``     — the heartbeat snapshot could not be read at all (e.g. the DB
                    was unreachable at the edge). Distinct from a missing ROW, which
                    is ``fresh``: here we have NO evidence either way, so NFR-5
                    ("degradation is visible, never false-fresh") forbids claiming
                    ``fresh``. Surfaced as a visible, non-actionable degraded pill.

Clock skew: ``beat_at`` and ``now`` are both the DB clock (host_heartbeat writes and
reads ``CURRENT_TIMESTAMP``), so cross-host skew cannot arise. A negative age (should
now only happen under sub-second read/write ordering) is still treated as fresh.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from server.config import env_float

LivenessState = Literal["fresh", "stale", "unreachable", "dead", "unknown"]

# The two liveness tuning knobs (spec 002 FR-101/FR-102) live HERE, once: the daemon
# loop that writes heartbeats and the edge that reads them must agree on the names,
# the defaults, and the clamp, and three hand-rolled copies of
# ``float(os.environ.get(...))`` is exactly the drift ``env_float`` was extracted to
# stop (constitution VI; see server/tests/test_config_env_float.py).
#
# Routing through ``env_float`` also makes a typo'd override survivable rather than
# fatal: a raw ``float()`` on ``CCSWITCH_HOST_DEAD_AFTER_S=300s`` raises, and these
# values are read inside the session serializer and at the top of the heartbeat loop
# — a 500 on the whole sessions page, and a daemon that beats once and then never
# again (which, once Phase 4 adoption lands, becomes a false takeover of a live host).
HEARTBEAT_INTERVAL_ENV = "CCSWITCH_HOST_HEARTBEAT_INTERVAL_S"
HEARTBEAT_INTERVAL_DEFAULT_S = 10.0
DEAD_AFTER_ENV = "CCSWITCH_HOST_DEAD_AFTER_S"
DEAD_AFTER_DEFAULT_S = 300.0


def heartbeat_interval_s() -> float:
    """Configured beat interval in seconds; the ``fresh`` window is 2x this."""
    return env_float(HEARTBEAT_INTERVAL_ENV, HEARTBEAT_INTERVAL_DEFAULT_S, minimum=1.0)


def host_dead_after_s() -> float:
    """Configured silence (seconds) before a host's sessions read ``unreachable``."""
    return env_float(DEAD_AFTER_ENV, DEAD_AFTER_DEFAULT_S, minimum=1.0)


@dataclass(frozen=True)
class Liveness:
    """Derived liveness for a single host (immutable — safe to cache and log)."""
    state: LivenessState
    last_confirmed_alive_at: datetime | None


def derive_liveness(
    beat_at: datetime | None,
    now: datetime,
    *,
    interval_s: float = HEARTBEAT_INTERVAL_DEFAULT_S,
    dead_after_s: float = DEAD_AFTER_DEFAULT_S,
    declared_dead: bool = False,
    snapshot_failed: bool = False,
) -> Liveness:
    """Derive liveness state from a heartbeat timestamp.

    Args:
        beat_at:         Last beat_at from HostHeartbeat, or None when no row exists.
        now:             Caller-supplied DB clock (enables deterministic tests).
        interval_s:      Expected beat interval; fresh threshold is 2x this value.
        dead_after_s:    Seconds without a beat before unreachable → dead transition.
        declared_dead:   True when the primary has bumped the fence (FR-202).
        snapshot_failed: True when the heartbeat snapshot itself could not be read
                         (DB down at the edge). Yields ``unknown`` — NFR-5 forbids
                         reporting ``fresh`` when we have no evidence at all.
    """
    if snapshot_failed:
        # No snapshot ⇒ no evidence either way. Do NOT claim fresh (NFR-5).
        return Liveness(state="unknown", last_confirmed_alive_at=None)

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
