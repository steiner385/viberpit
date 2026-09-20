"""The effective global active-session cap (spec 2026-09-20 D3, D11).

ONE reader. The orchestrator's admission gate, the sessions list's queue-stall
reason, the settings API's effective defaults, the fleet API's queue drain and
the capacity readout all come through here; nothing else reads
``AppSettings.max_active_sessions`` as a cap (pinned by
``test_global_cap.py::test_no_other_reader_of_max_active_sessions``).

Two ceilings; the lower one wins:

* **physical** — Σ ``Host.max_sessions`` over ENABLED hosts. Unbounded (``None``)
  when there are no hosts or any enabled host is uncapped. Health is a runtime
  condition, not provisioning, so an enabled-but-down host still counts here;
  the readout's Hosts row is where health shows.
* **attention** — ``AppSettings.max_active_sessions``: "never more than N at once,
  whatever the hardware could take". ``None`` = no attention ceiling.

When both are unbounded, ``DEFAULT_MAX_ACTIVE_SESSIONS`` is the last-resort floor:
a single host with no cap must not admit an unbounded number of sessions.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import DEFAULT_MAX_ACTIVE_SESSIONS, AppSettings, Host

log = logging.getLogger(__name__)

Binding = Literal["physical", "attention", "default"]


@dataclass(frozen=True)
class GlobalCap:
    physical: int | None
    attention: int | None
    effective: int
    binding: Binding
    # (name, cap) for every ENABLED host, name order — the Limits page's derivation line.
    hosts: tuple[tuple[str, int | None], ...]

    def to_wire(self) -> dict[str, Any]:
        return {
            "physical": self.physical,
            "attention": self.attention,
            "effective": self.effective,
            "binding": self.binding,
            "hosts": [{"name": name, "cap": cap} for name, cap in self.hosts],
        }


def resolve_global_cap(*, host_caps: Iterable[tuple[str, int | None]], attention: int | None,
                       default: int = DEFAULT_MAX_ACTIVE_SESSIONS) -> GlobalCap:
    """Pure: the same arithmetic for every caller, so the gate, the API and the
    readout can never disagree on what the cap is."""
    hosts = tuple(host_caps)
    physical: int | None
    if not hosts or any(cap is None for _, cap in hosts):
        physical = None
    else:
        physical = sum(cap for _, cap in hosts if cap is not None)
    # A tie names the attention ceiling: it is the typed value, the one the
    # person will look for when asking "why 10?".
    if attention is not None and (physical is None or attention <= physical):
        return GlobalCap(physical, attention, max(1, attention), "attention", hosts)
    if physical is not None:
        return GlobalCap(physical, attention, max(1, physical), "physical", hosts)
    return GlobalCap(None, None, max(1, default), "default", hosts)


async def read_global_cap(db: AsyncSession) -> GlobalCap:
    rows = (await db.execute(
        select(Host.name, Host.max_sessions).where(Host.enabled.is_(True)).order_by(Host.name)
    )).all()
    settings_row = (await db.execute(select(AppSettings))).scalar_one_or_none()
    attention = getattr(settings_row, "max_active_sessions", None) if settings_row else None
    return resolve_global_cap(host_caps=[(name, cap) for name, cap in rows], attention=attention)


def drain_if_raised(orch: Any, before: GlobalCap, after: GlobalCap) -> bool:
    """A dial moved the EFFECTIVE cap up: fill the new slots from the queue.

    Compares effective values, so raising the attention ceiling above the
    physical one (no new slot) does not drain, and capping a host lower does not
    either. ``orch`` may be None or lack ``maybe_start_queued`` (minimal test
    apps); that is a no-op, never a crash.
    """
    if after.effective <= before.effective:
        return False
    if orch is None or not hasattr(orch, "maybe_start_queued"):
        log.debug("global_cap: orchestrator unavailable for queue drain")
        return False
    asyncio.create_task(orch.maybe_start_queued())
    return True
