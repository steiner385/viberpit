"""In-memory guardrail state for failure-driven account rotation.

Tracks two things, both intentionally non-persistent (rate limits are transient,
and this avoids a ledger migration / the dev-SQLite no-ALTER constraint):

- a **quarantine** map of accounts that recently 429'd or failed a 401 recompose,
  excluded from selection until a cooldown expires; and
- a per-session **rotation cap** so a session can't thrash across accounts during a
  storm where every account is struggling.

The clock is injected (``now``) so behavior is deterministic in tests.
"""
from __future__ import annotations

import os
from collections import defaultdict

# Recovery tunables (seconds), defined in one place.
RETRY_AFTER_MAX: float = 30.0     # wait-and-retry the same account if Retry-After <= this
QUARANTINE_429: float = 60.0      # cooldown after a 429 with no usable Retry-After
QUARANTINE_401: float = 300.0     # cooldown after a recompose-failed 401 (creds genuinely bad)
MAX_ROTATIONS: int = 3            # rotations per session allowed within ROTATION_WINDOW
ROTATION_WINDOW: float = 300.0
CRASH_CAP: int = 3                # crashes per session allowed within CRASH_WINDOW
CRASH_WINDOW: float = 120.0       # window (seconds) for tracking session crashes
STALL_CAP: int = 3                # stall-watchdog interrupts per session within STALL_WINDOW
STALL_WINDOW: float = 120.0       # window (seconds) for tracking session stalls
STALL_TIMEOUT: float = float(os.environ.get("CCSWITCH_STALL_TIMEOUT_SEC", "300"))


class RecoveryState:
    def __init__(self, *, max_rotations: int = MAX_ROTATIONS, window: float = ROTATION_WINDOW) -> None:
        self._quarantine: dict[str, float] = {}        # account -> until (absolute ts)
        self._rotations: dict[str, list[float]] = defaultdict(list)  # session_id -> timestamps
        self._max_rotations = max_rotations
        self._window = window
        self._crashes: dict[str, list[float]] = defaultdict(list)  # session_id -> crash timestamps
        self._crash_cap = CRASH_CAP
        self._crash_window = CRASH_WINDOW
        self._stalls: dict[str, list[float]] = defaultdict(list)  # session_id -> stall timestamps
        self._stall_cap = STALL_CAP
        self._stall_window = STALL_WINDOW

    # --- quarantine -----------------------------------------------------------
    def quarantine(self, account: str, *, until: float) -> None:
        """Quarantine *account* until the absolute timestamp *until* (extends if later)."""
        self._quarantine[account] = max(self._quarantine.get(account, 0.0), until)

    def quarantined(self, *, now: float) -> frozenset[str]:
        """Accounts still in cooldown at *now* (expired entries are pruned)."""
        expired = [a for a, until in self._quarantine.items() if until <= now]
        for a in expired:
            del self._quarantine[a]
        return frozenset(self._quarantine)

    # --- per-session rotation cap ---------------------------------------------
    def record_rotation(self, session_id: str, *, now: float) -> None:
        self._rotations[session_id].append(now)

    def rotation_cap_reached(self, session_id: str, *, now: float) -> bool:
        """True if *session_id* has already rotated ``max_rotations`` times in the window."""
        cutoff = now - self._window
        recent = [t for t in self._rotations.get(session_id, ()) if t > cutoff]
        self._rotations[session_id] = recent
        return len(recent) >= self._max_rotations

    # --- per-session crash cap ------------------------------------------------
    def record_crash(self, session_id: str, *, now: float) -> None:
        self._crashes[session_id].append(now)

    def crash_cap_reached(self, session_id: str, *, now: float) -> bool:
        """True if *session_id* has already crashed ``crash_cap`` times in the window."""
        cutoff = now - self._crash_window
        recent = [t for t in self._crashes.get(session_id, ()) if t > cutoff]
        self._crashes[session_id] = recent
        return len(recent) >= self._crash_cap

    # --- per-session stall cap ------------------------------------------------
    def record_stall(self, session_id: str, *, now: float) -> None:
        self._stalls[session_id].append(now)

    def stall_cap_reached(self, session_id: str, *, now: float) -> bool:
        """True if *session_id* has already stalled ``stall_cap`` times in the window."""
        cutoff = now - self._stall_window
        recent = [t for t in self._stalls.get(session_id, ()) if t > cutoff]
        self._stalls[session_id] = recent
        return len(recent) >= self._stall_cap
