"""PM Onboarding — the constitution-proposal origin (issue #2259).

Registered for the same reason ``pm-onboard`` is (see that module's docstring):
the registry is where an origin's audited engine ceiling is *declared* and
where its operator opt-in is *stored*. A separate id from ``pm-onboard``
because offering a constitution proposal is a genuinely independent
parallel session-spawn subsystem — its own pure prompt builder
(``build_constitution_proposal_prompt``), its own orchestrator spawn seam
(``pm_onboard_spawn_constitution``), and its own dedupe key and account
resolution — even though both are surfaced from the same Projects onboarding
wizard (design 2026-08-31 §5, issue #2165 item 2).

**There is no control loop here**, for the same reason ``pm-onboard`` has
none: the session is spawned by an operator tap
(``POST /projects/{slug}/propose-constitution``), not a tick. An origin with
no loop declares ``modes = ()``.

``engine_policy`` stays ``CLAUDE_ONLY``: unlike ``pm-onboard``'s synthesis and
critic prompts (widened by #1268 after a dedicated Codex acceptance suite),
this prompt has none yet, so it fails closed until one exists.
"""
from __future__ import annotations

from typing import Any

from .base import AutomationStatus, ConfigField
from .engine_policy import EnginePolicy
from .model_pick import AUTOMATION_MODEL_CHOICES


class PmOnboardConstitution:
    id = "pm-onboard-constitution"
    name = "PM Onboarding — Constitution Proposal"
    work_class = "maintenance"
    description = (
        "Drafts a repository constitution from the project's ADRs, AGENTS.md, "
        "and any existing spec-kit templates. Propose-only: the session lands "
        "on ONE non-draft PR and the human merge is the gate. Offered from the "
        "Projects onboarding wizard once the map is merged, when the repo's "
        "governance document is missing, still a template, or missing its "
        "version footer — not on a schedule.")
    #: No control loop — see the module docstring.
    modes: tuple[str, ...] = ()
    engine_policy: EnginePolicy = EnginePolicy.CLAUDE_ONLY
    #: Mirrors pm-onboard's single setting for its spawned sessions.
    config_schema: list = [
        ConfigField("default_model", "Session model", "enum", "default",
                    choices=list(AUTOMATION_MODEL_CHOICES),
                    help="Model for the spawned constitution-proposal session. "
                         "'default' uses the account's default model; 'auto' "
                         "assesses the job's complexity and picks a tier."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        return AutomationStatus(text="operator-triggered", level="idle")

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        return None
