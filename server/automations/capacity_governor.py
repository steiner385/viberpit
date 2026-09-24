"""Capacity Governor — derives runner concurrency and per-product in-flight caps
from real headroom (spec 2026-09-19 §6). Two objects, the control-band mold:
``CapacityGovernor`` (the Automation surface) and ``CapacityGovernorLoop`` (the
supervised loop, every I/O injected). Phase 1: monitor computes and shows; act is
wired but the runner does not read the plan until phase 2."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .base import AutomationStatus, ConfigField
from .capacity_plan import DEFAULT_HYSTERESIS_TICKS, PUSH_LEVELS, plan_capacity, plan_from_dict
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
                    help="How much weekly quota to keep in reserve: conserve 40%, steady 20%, max 5%.",
                    surface="capacity"),
        ConfigField("sessions_per_account", "Sessions per account", "int", 2, min=1,
                    help="Concurrent runner sessions one account can carry before its quota "
                         "burns faster than it resets.",
                    surface="capacity"),
        ConfigField("hysteresis_ticks", "Confirmation ticks", "int", DEFAULT_HYSTERESIS_TICKS, min=1,
                    help="How many consecutive ticks must propose the same new in-flight cap "
                         "before it is applied; 1 applies changes immediately.",
                    surface="capacity"),
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
        # Read→write window (#2040): the floors inside `plan_capacity` use the
        # in-flight counts of THIS readout snapshot, so a claim reserved between the
        # snapshot and `_write_plan` is not floored until the next tick. Held claims
        # are immune regardless (Ruling 20: admission never refuses a claim that
        # already holds its slot), so the only effect is one tick of a cosmetic plan
        # row; re-read the target states just before the write if that ever matters.
        previous = plan_from_dict(await self._read_plan())
        plan = plan_capacity(readout_to_inputs(readout), push_level=config.get("push_level", "steady"),
                             sessions_per_account=int(config.get("sessions_per_account") or 2),
                             hysteresis_ticks=int(config.get("hysteresis_ticks") or DEFAULT_HYSTERESIS_TICKS),
                             previous=previous)
        applied = mode == "act" and bool(self._kill_switch())
        try:
            await self._write_plan({
                "pushLevel": plan.push_level, "applied": applied,
                "fleetConcurrency": plan.fleet_concurrency, "perProduct": plan.per_product,
                "pending": plan.pending, "budget": {**plan.budget, "limiting": plan.limiting,
                           "pendingStreak": plan.pending_streak},
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
            # Two silent cases, both deliberate: the very first plan (`previous` is
            # None — nothing was shown before, so nothing "changed"), and a cap that
            # did not move. A slug missing from an EXISTING previous plan is that
            # product's first derived cap (newly added, dispatch turned on, backlog
            # refilled): announce it, or the owner never hears a product joined.
            if previous is None or prev_caps.get(slug) == cap:
                continue
            reason = next((r for r in plan.reasons if r.startswith(f"{slug}:")), "")
            mode_text = "applied" if applied else "monitor only"
            suffix = f": {reason}" if reason else ""
            change = f"{prev_caps[slug]} → {cap}" if slug in prev_caps else f"first derived: {cap}"
            await self._ctx.emit(
                "info", f"{slug} in-flight cap {change} ({mode_text}{suffix})",
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
