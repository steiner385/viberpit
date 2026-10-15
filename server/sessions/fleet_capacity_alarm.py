"""Surface fleet capacity conditions before they become a dispatch failure (#1517).

On 2026-09-09 dobby published ``activeSessions: 11`` against a cap of 6 for an
afternoon (7 → 8 → 11) with no signal; the first anyone knew was a user-facing
"no enabled, healthy host with capacity". #1516 fixed the count, and a launch on
a full fleet now queues instead of failing — but the conditions themselves were
still silent. This watches them on the probe cadence (``host_probe.probe_all``):

* **Over cap** (``active > max``): the daemon's own gate never admits past its cap,
  so a count that stays above it is an accounting fault (the phantom count above)
  or a cap lowered under live work that is not draining. WARNING — an auto-error.
* **At cap** (``active == max``): routine now that the Backlog Runner fills slots
  and launches queue. INFO on entry/exit only, so the journal answers "when did it
  fill?"; the fleet page badges it.
* **No dispatchable host**: at least one host is enabled and none is enabled AND
  healthy, so nothing can start — not even a queued session. Strictly worse than
  any one host being down, and the per-host probe WARNINGs never say it. WARNING.

Every alarm is edge-triggered: one WARNING per spell, after the condition has held
for a run of consecutive ticks, and an INFO when it clears. A per-tick WARNING would
file one auto-error per record (``server.obs.errsink``); a single-tick one would fire
on every deploy restart and every cap edit. Capacity is read with ``_dispatchable``,
the ranker's own verdict, so the alarm cannot disagree with dispatch.
"""
from __future__ import annotations

import logging
from typing import Iterable

from server.sessions.fleet import HostCandidate, _dispatchable

log = logging.getLogger(__name__)

__all__ = ["CapacityAlarm", "OVER_CAP_ESCALATE_AFTER", "NO_HOST_ESCALATE_AFTER"]

# ~5 min at the 30 s probe interval: long enough for a just-lowered cap to drain a
# finishing turn, far shorter than the afternoon the 2026-09-09 count went unseen.
OVER_CAP_ESCALATE_AFTER = 10
# ~90 s — the same run ``host_probe`` requires before a single unreachable host is a
# WARNING, so a deploy restart of a one-host fleet does not read as an outage.
NO_HOST_ESCALATE_AFTER = 3


class CapacityAlarm:
    """Edge-triggered capacity alarms over successive host snapshots."""

    def __init__(self) -> None:
        self._over_ticks: dict[str, int] = {}
        self._over_warned: set[str] = set()
        self._at_cap: set[str] = set()
        self._no_host_ticks = 0
        self._no_host_warned = False

    def clear(self) -> None:
        """Forget every spell (tests share the module-level instance)."""
        self._over_ticks.clear()
        self._over_warned.clear()
        self._at_cap.clear()
        self._no_host_ticks, self._no_host_warned = 0, False

    def observe(self, candidates: Iterable[HostCandidate]) -> None:
        hosts = list(candidates)
        enabled = [c for c in hosts if c.enabled]
        self._observe_hosts(enabled)
        self._observe_fleet(enabled)

    def _observe_hosts(self, enabled: list[HostCandidate]) -> None:
        seen = {c.name for c in enabled}
        for c in enabled:
            cap = c.max_sessions
            over = cap is not None and c.active_sessions > cap
            at = cap is not None and c.active_sessions == cap
            if over:
                n = self._over_ticks[c.name] = self._over_ticks.get(c.name, 0) + 1
                if n >= OVER_CAP_ESCALATE_AFTER and c.name not in self._over_warned:
                    self._over_warned.add(c.name)
                    log.warning(
                        "fleet.capacity: host %s reports %d active sessions over its cap "
                        "of %d — it receives no new work until the count drops",
                        c.name, c.active_sessions, cap)
            else:
                self._clear_over(c.name)
            if at and c.name not in self._at_cap:
                self._at_cap.add(c.name)
                log.info("fleet.capacity: host %s is at its cap (%d/%d) — new launches "
                         "go elsewhere or queue", c.name, c.active_sessions, cap)
            elif not at and not over and c.name in self._at_cap:
                self._at_cap.discard(c.name)
                log.info("fleet.capacity: host %s has headroom again (%d/%s)",
                         c.name, c.active_sessions, cap)
        # A host that was disabled or removed is no longer this alarm's business.
        for name in set(self._over_ticks) - seen:
            self._clear_over(name)
        self._at_cap &= seen

    def _clear_over(self, name: str) -> None:
        self._over_ticks.pop(name, None)
        if name in self._over_warned:
            self._over_warned.discard(name)
            log.info("fleet.capacity: host %s is back within its cap", name)

    def _observe_fleet(self, enabled: list[HostCandidate]) -> None:
        none_up = bool(enabled) and not any(
            _dispatchable(c, ignore_capacity=True) for c in enabled)
        if not none_up:
            if self._no_host_warned:
                log.info("fleet.capacity: a dispatchable host is back")
            self._no_host_ticks, self._no_host_warned = 0, False
            return
        self._no_host_ticks += 1
        if self._no_host_ticks >= NO_HOST_ESCALATE_AFTER and not self._no_host_warned:
            self._no_host_warned = True
            log.warning(
                "fleet.capacity: no dispatchable host — every enabled host is unhealthy "
                "or unprobed, so no session can start (%d enabled)", len(enabled))
