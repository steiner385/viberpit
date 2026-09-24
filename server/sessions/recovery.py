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
# Hard cap on quarantine duration so a large API Retry-After (Anthropic can return 3600s)
# cannot block the only account with headroom for hours. The per-session recovery still
# handles repeated 429s via rotation/crash caps.
QUARANTINE_MAX: float = float(os.environ.get("CCSWITCH_QUARANTINE_MAX_SEC", "900"))
MAX_ROTATIONS: int = 3            # rotations per session allowed within ROTATION_WINDOW
ROTATION_WINDOW: float = 300.0
CRASH_CAP: int = 3                # crashes per session allowed within CRASH_WINDOW
CRASH_WINDOW: float = 120.0       # window (seconds) for tracking session crashes
STALL_CAP: int = 3                # consecutive stall-watchdog interrupts per session before escalating
STALL_TIMEOUT: float = float(os.environ.get("CCSWITCH_STALL_TIMEOUT_SEC", "300"))
# Extended stall timeout for a turn whose silence is attributable to API throttling
# (the driver is backing off, or its account is in recovery quarantine). The SDK can
# go silent during a 429/529 backoff without surfacing a per-wait progress event, so a
# throttled-but-alive turn must not be interrupted at the base timeout. It still gets
# interrupted past this longer window so a genuine hang is eventually caught. Issue #185.
STALL_TIMEOUT_THROTTLED: float = float(
    os.environ.get("CCSWITCH_STALL_TIMEOUT_THROTTLED_SEC", "900"))
# Observability-only threshold, well under STALL_TIMEOUT: a turn silent this long gets
# ONE diagnostic snapshot logged (driver.diagnostic_snapshot(), if the driver has one)
# so a turn that eventually times out has real evidence of what it was doing during
# the silence, not just "no progress for N seconds" after the fact (2026-09-22).
STALL_DIAG_THRESHOLD_S: float = float(
    os.environ.get("CCSWITCH_STALL_DIAG_THRESHOLD_SEC", "60"))
RESEND_CAP: int = 3              # auto-resends of a dropped turn per session within RESEND_WINDOW
RESEND_WINDOW: float = 120.0     # window (seconds) for tracking dropped-turn resends


class RecoveryState:
    def __init__(self, *, max_rotations: int = MAX_ROTATIONS, window: float = ROTATION_WINDOW) -> None:
        self._quarantine: dict[str, float] = {}        # account -> until (absolute ts)
        self._rotations: dict[str, list[float]] = defaultdict(list)  # session_id -> timestamps
        self._max_rotations = max_rotations
        self._window = window
        self._crashes: dict[str, list[float]] = defaultdict(list)  # session_id -> crash timestamps
        self._crash_cap = CRASH_CAP
        self._crash_window = CRASH_WINDOW
        self._stalls: dict[str, int] = defaultdict(int)  # session_id -> consecutive stall count
        self._stall_cap = STALL_CAP
        self._resends: dict[str, list[float]] = defaultdict(list)  # session_id -> resend timestamps
        self._resend_cap = RESEND_CAP
        self._resend_window = RESEND_WINDOW

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
    # A *consecutive-streak* counter, not a sliding time window like the caps above.
    # A wedged turn re-stalls roughly once per STALL_TIMEOUT (or the longer throttled
    # timeout) after each interrupt — both comfortably exceed any window short enough
    # to still be a useful rate limit — so a window-pruned count could never reach
    # stall_cap and a turn that reliably re-wedges every watchdog cycle looped forever
    # instead of escalating (observed: session 808f63fc, 2026-09-22, 3.5h/2 account
    # rotations). The streak accumulates across repeated stalls regardless of spacing
    # and only resets via clear_stalls(), which the watchdog calls once it observes
    # the turn genuinely resolve (no longer active or draining).
    def record_stall(self, session_id: str, *, now: float) -> None:
        self._stalls[session_id] += 1

    def stall_streak(self, session_id: str) -> int:
        """Consecutive stall-watchdog interrupts recorded for *session_id*.

        The watchdog logs this so one line says whether the previous interrupt landed:
        streak 1 is the first-line recovery that usually works, a growing streak is a
        turn the control channel is not clearing (#1718).
        """
        return self._stalls.get(session_id, 0)

    def stall_cap_reached(self, session_id: str, *, now: float) -> bool:
        """True if *session_id* has already stalled ``stall_cap`` times in a row."""
        return self.stall_streak(session_id) >= self._stall_cap

    def clear_stalls(self, session_id: str) -> None:
        """Reset the consecutive-stall streak once a turn is seen to genuinely resolve."""
        self._stalls.pop(session_id, None)

    # --- per-session dropped-turn resend cap ----------------------------------
    def record_resend(self, session_id: str, *, now: float) -> None:
        self._resends[session_id].append(now)

    def resend_count(self, session_id: str, *, now: float) -> int:
        """Dropped-turn resends recorded for *session_id* within the window
        (expired entries are pruned, matching the cap check)."""
        cutoff = now - self._resend_window
        recent = [t for t in self._resends.get(session_id, ()) if t > cutoff]
        self._resends[session_id] = recent
        return len(recent)

    def resend_cap_reached(self, session_id: str, *, now: float) -> bool:
        """True if *session_id* has already auto-resent ``resend_cap`` dropped turns
        within the window."""
        return self.resend_count(session_id, now=now) >= self._resend_cap
