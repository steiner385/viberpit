"""PM Classification — the backlog-triage proposal origin.

Registered for the reason spelled out in ``pm_onboard.py``: an unregistered
origin has no declared ceiling and no config row to hold an operator's engine
opt-in, so it is permanently ``claude_only`` by default rather than by decision.

**There is no control loop here either.** ``pm_classify_run`` is called by the
onboarding wizard and by the pm-triage loop with an injected batch; it is not
ticked. Hence ``modes = ()`` — see ``pm_onboard.py`` and the ``Automation``
protocol in ``base.py``.
"""
from __future__ import annotations

from typing import Any

from .base import AutomationStatus
from .engine_policy import EnginePolicy


class PmClassify:
    id = "pm-classify"
    name = "PM Classification"
    description = (
        "Classifies a bounded chunk of open backlog items against the project's "
        "feature map, proposing RICE-lite scores a human accepts or rejects in "
        "the Triage Inbox. Proposals only — nothing reaches a work item without "
        "a human accept. Triggered from the Projects workspace, not on a schedule.")
    #: No control loop — see the module docstring.
    modes: tuple[str, ...] = ()
    # Audited engine ceiling (#1269). Gated on
    # `server/tests/test_pm_codex_acceptance.py` (one engine resolution threaded
    # into the account gate and the session, chunk dedupe unchanged by engine, a
    # refusal that dispatches nothing and starts no ingest chain, and a harvest
    # that reads normalized `TextDelta` so a Codex reasoning summary cannot
    # corrupt the fenced JSON) and `server/tests/test_pm_codex_live.py`
    # (a real bounded batch on the real binary, opt-in).
    #
    # `codex_allowed` grants the option, not the traffic — see `pm_onboard.py`.
    engine_policy: EnginePolicy = EnginePolicy.CODEX_ALLOWED
    config_schema: list = []

    def status(self, ctx: Any) -> AutomationStatus:
        return AutomationStatus(text="operator-triggered", level="idle")

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        return None
