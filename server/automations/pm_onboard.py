"""PM Onboarding — the feature-map synthesis + completeness-critic origin.

Registered for the same reason every other session-spawning origin is: the
registry is where an origin's audited engine ceiling is *declared* and where its
operator opt-in is *stored*. Left unregistered (as this origin was until #1268),
``AutomationContext.launch_plan`` swallowed the ``KeyError`` and answered
``claude_only`` by default, ``read_effective_config`` answered ``{}``, and the
API had no schema to render an engine selector from — so there was no reviewable
statement of what this origin may run and no way for an operator to choose.

**There is no control loop here.** The orchestrator seams
``pm_onboard_spawn_map`` / ``_pm_onboard_spawn_critic`` are driven by an operator
tap in the Projects workspace and by the pm-triage drift path, not by a tick. An
origin with no loop declares ``modes = ()``: the config surface (its engine
ceiling's fields) still applies, and the API refuses an enable/mode PATCH there
is nothing to enable. See ``server/automations/base.py``'s ``Automation``
protocol.
"""
from __future__ import annotations

from typing import Any

from .base import AutomationStatus
from .engine_policy import EnginePolicy


class PmOnboard:
    id = "pm-onboard"
    name = "PM Onboarding"
    description = (
        "Feature-map synthesis and its independent completeness critic — the "
        "product-archaeology half of project onboarding. Propose-only: both "
        "sessions land on ONE non-draft PR and the human merge is the gate. "
        "Triggered from the Projects workspace, not on a schedule.")
    #: No control loop — see the module docstring.
    modes: tuple[str, ...] = ()
    # Audited engine ceiling (#1268). Gated on
    # `server/tests/test_pm_codex_acceptance.py` (the synthesis + critic prompt
    # contracts including the attribution trailer the critic never had, one
    # engine resolution threaded into prompt/account/session, and a refusal that
    # neither clones nor advances `onboarding_state`) and
    # `server/tests/test_pm_codex_live.py` (the live boundary, opt-in).
    #
    # `codex_allowed` grants the option, not the traffic: `default_engine` still
    # answers Claude, and both `CCSWITCH_CODEX_AUTOMATIONS_ENABLED` and #1263's
    # network-egress opt-in ship off.
    engine_policy: EnginePolicy = EnginePolicy.CODEX_ALLOWED
    config_schema: list = []

    def status(self, ctx: Any) -> AutomationStatus:
        return AutomationStatus(text="operator-triggered", level="idle")

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        return None
