"""The pr-doctor reference automation — the registry home for the existing
pr_autofix behavior. 'pr-doctor' is the permanent slug; the richer recovery
behavior (archived sessions, merge conflicts, retries) lands in spec #2 under
this same id. Behavior currently lives in the orchestrator's _on_pr_change
(see Task 9); this class supplies identity, modes, config schema, and status."""
from __future__ import annotations

from server.sessions.pr_fix import VALID_AUTOFIX_MODES

from .base import AutomationStatus, ConfigField


def _ordered_modes() -> tuple[str, ...]:
    # off first (dropdown default-most-conservative), then the rest, stable.
    rest = sorted(m for m in VALID_AUTOFIX_MODES if m != "off")
    return ("off", *rest)


class PrDoctor:
    id = "pr-doctor"
    name = "PR Doctor"
    description = "Watches CI on PRs created via ccswitch and restores failing ones."
    modes = _ordered_modes()
    config_schema = [
        ConfigField(key="enabled_repos", label="Repositories",
                    type="repo-multiselect", default=[],
                    help="Limit to these repos (empty = all ccswitch-managed repos)."),
        ConfigField(key="max_attempts", label="Max fix attempts", type="int", default=3,
                    help="Session-fix attempts per PR before giving up (0 = notify only)."),
        ConfigField(key="mechanical_attempts", label="Max branch-update attempts",
                    type="int", default=2, help="Mechanical (update-branch) attempts per PR."),
        ConfigField(key="auto_update_out_of_date", label="Auto-update behind branches",
                    type="bool", default=True, help="Mechanical, safe."),
        ConfigField(key="auto_fix_failing_ci", label="Auto-fix failing CI",
                    type="bool", default=True, help="Spawns a session to fix failing CI jobs."),
        ConfigField(key="auto_resolve_conflicts", label="Auto-resolve merge conflicts",
                    type="bool", default=False, help="Risky — spawns a session to rebase/resolve."),
    ]

    def status(self, ctx) -> AutomationStatus:
        # Cheap: the orchestrator bumps a live in-memory summary it injects here in
        # Task 9. With no live data (ctx is None / no counts), report idle.
        counts = getattr(ctx, "pr_doctor_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        failing = counts.get("failing", 0)
        return AutomationStatus(
            text=f"watching {failing} failing PR(s)" if failing else "all PRs green",
            level="active" if failing else "idle", counts=counts)

    async def on_state_change(self, **kw) -> None:  # noqa: D401
        # No-op: satisfies the Automation Protocol; orchestrator wiring arrives in Task 9.
        ...
