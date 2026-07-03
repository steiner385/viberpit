"""The autopilot automation: keeps governed sessions moving through their stuck
states until their effective goal is met. Metadata + config here; the control loop
(AutopilotLoop) is added in a later task."""
from __future__ import annotations

from .base import AutomationStatus, ConfigField


class Autopilot:
    id = "autopilot"
    name = "Autopilot"
    description = "Continuously advances every governed session toward its goal."
    modes: tuple[str, ...] = ("off", "propose", "auto")
    config_schema = [
        ConfigField(key="allow_tool_approvals", label="Approve tool permissions", type="bool",
                    default=True, help="If off, autopilot never approves permissions (only "
                                       "denies / answers / nudges)."),
        ConfigField(key="max_actions_per_session_per_hour", label="Max actions/session/hour",
                    type="int", default=12, help="Per-session circuit-breaker."),
        ConfigField(key="max_actions_per_day", label="Max actions/day (global)", type="int",
                    default=300, help="Global rolling-24h circuit-breaker."),
        ConfigField(key="recovery_max_attempts", label="Max recovery attempts", type="int",
                    default=2, help="Recovery nudges before handing a failed session to a human."),
        ConfigField(key="loop_repeat_limit", label="Loop repeat limit", type="int", default=3,
                    help="Identical stuck states before escalating to a human."),
        ConfigField(key="human_defer_seconds", label="Human deference (seconds)", type="int",
                    default=120, help="Skip a session a human touched this recently."),
        ConfigField(key="extra_deny_patterns", label="Extra deny patterns", type="string",
                    default="", help="Comma-separated patterns that EXTEND (never shrink) the "
                                     "built-in destructive-command deny-list."),
        ConfigField(key="enabled_repos", label="Repositories", type="repo-multiselect",
                    default=[], help="Limit governance to these repos (empty = all)."),
    ]

    def status(self, ctx) -> AutomationStatus:
        counts = getattr(ctx, "autopilot_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        advancing = counts.get("advancing", 0)
        escalated = counts.get("escalated", 0)
        return AutomationStatus(
            text=f"{advancing} advancing" + (f", {escalated} escalated" if escalated else ""),
            level="active" if advancing else ("warn" if escalated else "idle"),
            counts=counts)

    async def on_state_change(self, *, enabled, mode, config, ctx) -> None:
        # The orchestrator's loop reads mode/config each tick; nothing to start here.
        ...
