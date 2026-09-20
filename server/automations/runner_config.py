"""The ONE read of the Backlog Runner's (mode, config) with the capacity plan
overlaid (spec 2026-09-20 D11). Before this module the orchestrator's gate and
admission's snapshot each did their own read + overlay — two paths that could
straddle a governor write.

``apply_plan=False`` returns the operator's STORED ceiling: the governor's own
planner reads through here to learn it and must derive the next plan from that,
never from its own prior output (feeding the overlaid value back in would
ratchet ``fleetConcurrency`` down every tick). ``plan=`` lets a caller that has
already read the plan once (admission's snapshot) reuse it instead of reading
again — but ``apply_fleet_plan`` itself is pure and does not gate on liveness
(``capacity_apply``'s own docstring: every seam calls ``plan_is_live`` before
overlaying), so a given plan is re-checked here exactly like ``live_plan()``
checks a freshly read one; admission's plan already passed that check, so this
is a no-op for it, and a caller that hands in something else (e.g. a stale or
hand-built plan) cannot bypass liveness.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from server.config import get_settings

from .capacity_apply import apply_fleet_plan, plan_is_live
from .capacity_store import live_plan
from .config_store import read_automation_config

RUNNER_MODES: tuple[str, ...] = ("off", "on", "propose", "auto")

# Sentinel: "no plan given, read the live one" — None is a real value (no plan).
UNSET: Any = object()


async def read_runner_config(*, plan: dict | None | Any = UNSET,
                             apply_plan: bool = True) -> tuple[str, dict]:
    from .backlog_runner import BacklogRunner

    mode, config = await read_automation_config(
        BacklogRunner, modes=RUNNER_MODES, default_mode="on", log_key="backlog_runner")
    if apply_plan:
        if plan is UNSET:
            effective_plan = await live_plan()
        else:
            enabled = bool(get_settings().capacity_governor_enabled)
            live = plan_is_live(plan, now=datetime.now(timezone.utc), enabled=enabled)
            effective_plan = plan if live else None
        config = apply_fleet_plan(config, effective_plan)
    return mode, config
