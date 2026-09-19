"""Capacity Governor — derives runner concurrency and per-product in-flight caps
from real headroom (spec 2026-09-19 §6). Two objects, the control-band mold:
``CapacityGovernor`` (the Automation surface) and ``CapacityGovernorLoop`` (the
supervised loop, every I/O injected). Phase 1: monitor computes and shows; act is
wired but the runner does not read the plan until phase 2."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .base import AutomationStatus, ConfigField
from .capacity_plan import PUSH_LEVELS, plan_capacity, plan_from_dict
from .capacity_readout import readout_to_inputs
from .context import observation_failed, publish_observation
from .engine_policy import EnginePolicy

log = logging.getLogger(__name__)


class CapacityGovernor:
    id = "capacity-governor"
    name = "Capacity Governor"
    work_class = "maintenance"
    description = ("Reads account quota, host capacity and the runner's own ledger every tick, "
                   "and derives how many sessions the fleet and each product should run. "
                   "monitor shows the plan; act applies it (phase 2).")
    modes: tuple[str, ...] = ("off", "monitor", "act")
    engine_policy: EnginePolicy = EnginePolicy.CLAUDE_ONLY  # spawns nothing; fail closed
    config_schema = [
        ConfigField("push_level", "Push level", "enum", "steady", choices=list(PUSH_LEVELS),
                    help="How much weekly quota to keep in reserve: conserve 40%, steady 20%, max 5%."),
        ConfigField("sessions_per_account", "Sessions per account", "int", 2, min=1,
                    help="Concurrent runner sessions one account can carry before its quota "
                         "burns faster than it resets."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "capacity_counts", None) if ctx else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        text = counts.get("binding_text") or f"fleet {counts.get('fleet', 0)} · no binding constraint"
        level = "warn" if counts.get("defects") else "active"
        return AutomationStatus(text=text, level=level,
                                counts={k: v for k, v in counts.items() if isinstance(v, int)})

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        return None


ReadConfig = Callable[[], Awaitable[tuple[str, dict]]]
ReadReadout = Callable[[], Awaitable[dict]]
ReadPlan = Callable[[], Awaitable[dict | None]]
WritePlan = Callable[[dict], Awaitable[None]]


class CapacityGovernorLoop:
    def __init__(self, ctx: Any, *, read_config: ReadConfig, read_readout: ReadReadout,
                 read_plan: ReadPlan, write_plan: WritePlan, kill_switch: Callable[[], bool]) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._read_readout = read_readout
        self._read_plan = read_plan
        self._write_plan = write_plan
        self._kill_switch = kill_switch
        self._stale_emitted = False
        self._defects_emitted: set[str] = set()

    async def tick(self) -> None:
        try:
            await self._run()
        except Exception:  # pragma: no cover — mirrors the mold
            observation_failed(self._ctx)
            log.warning("capacity_governor.tick_error", exc_info=True)

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        try:
            readout = await self._read_readout()
        except Exception as exc:
            observation_failed(self._ctx)
            if not self._stale_emitted:
                self._stale_emitted = True
                await self._ctx.emit("warn", f"capacity readout failed: {exc}; keeping the previous plan",
                                     kind="plan_stale")
            return
        previous = plan_from_dict(await self._read_plan())
        plan = plan_capacity(readout_to_inputs(readout), push_level=config.get("push_level", "steady"),
                             sessions_per_account=int(config.get("sessions_per_account") or 2),
                             previous=previous)
        applied = mode == "act" and bool(self._kill_switch())
        try:
            await self._write_plan({
                "pushLevel": plan.push_level, "applied": applied,
                "fleetConcurrency": plan.fleet_concurrency, "perProduct": plan.per_product,
                "pending": plan.pending, "budget": {**plan.budget, "limiting": plan.limiting},
                "binding": readout.get("binding"), "reasons": plan.reasons,
                "defects": readout.get("defects", []),
            })
        except Exception as exc:
            # spec §6.4: a write failure is exactly as stale as a readout failure
            # — the previous plan is still the last one anyone saw, so it must
            # not be papered over by a half-written or missing row, and nothing
            # below (cap_changed / defect events, the fresh counts observation)
            # may claim a plan that never made it to the store.
            observation_failed(self._ctx)
            if not self._stale_emitted:
                self._stale_emitted = True
                await self._ctx.emit("warn", f"plan write failed: {exc}; keeping the previous plan",
                                     kind="plan_stale")
            return
        # Only a tick where BOTH the readout and the write succeeded clears the
        # streak — a later failure (of either) must be loud again, not silently
        # assumed to be the same one already reported.
        self._stale_emitted = False
        prev_caps = previous.per_product if previous else {}
        for slug, cap in plan.per_product.items():
            if slug in prev_caps and prev_caps[slug] != cap:
                reason = next((r for r in plan.reasons if r.startswith(f"{slug}:")), "")
                mode_text = "applied" if applied else "monitor only"
                suffix = f": {reason}" if reason else ""
                await self._ctx.emit(
                    "info", f"{slug} in-flight cap {prev_caps[slug]} → {cap} ({mode_text}{suffix})",
                    kind="cap_changed", target=slug)
        current = {d["target"] for d in readout.get("defects", [])}
        for d in readout.get("defects", []):
            if d["target"] not in self._defects_emitted:
                await self._ctx.emit("warn", d["text"], kind="capacity_defect", target=d["target"],
                                     detail={"sessionId": d.get("sessionId"), "product": d.get("product")})
        self._defects_emitted = current
        binding = readout.get("binding") or {}
        publish_observation(self._ctx, "capacity_counts", {
            "fleet": plan.fleet_concurrency, "defects": len(current),
            "accounts_available": readout.get("accounts", {}).get("available", 0),
            "products": len(plan.per_product), "binding_text": binding.get("text"),
        })
