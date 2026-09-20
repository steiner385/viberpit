"""The ONE read of the Backlog Runner's (mode, config) with the capacity plan
overlaid (spec 2026-09-20 D11). Before this module the orchestrator's gate and
admission's snapshot each did their own read + overlay — two paths that could
straddle a governor write.

``apply_plan=False`` returns the operator's STORED ceiling: the governor's own
planner reads through here to learn it and must derive the next plan from that,
never from its own prior output (feeding the overlaid value back in would
ratchet ``fleetConcurrency`` down every tick). ``plan=`` lets a caller that has
already read the plan once (admission's snapshot) reuse it instead of reading
again — its CONTRACT is "the value a prior ``live_plan()`` call returned" (a
live plan dict, or ``None`` when there wasn't one): ``live_plan()`` is already
the one liveness check (``capacity_apply.plan_is_live``), so a given plan is
applied as-is, never re-checked here. Callers never hand in a raw stored plan.
"""
from __future__ import annotations

from typing import Any

from .capacity_apply import apply_fleet_plan
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
        config = apply_fleet_plan(config, await live_plan() if plan is UNSET else plan)
    return mode, config
