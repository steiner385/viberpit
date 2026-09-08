"""The pr-doctor reference automation — the registry home for the existing
pr_autofix behavior. 'pr-doctor' is the permanent slug; the richer recovery
behavior (archived sessions, merge conflicts, retries) lands in spec #2 under
this same id. Behavior currently lives in the orchestrator's _on_pr_change
(see Task 9); this class supplies identity, modes, config schema, and status."""
from __future__ import annotations

from server.sessions.pr_fix import VALID_AUTOFIX_MODES

from .base import AutomationStatus, ConfigField
from .model_pick import AUTOMATION_MODEL_CHOICES
from .engine_policy import EnginePolicy


def _ordered_modes() -> tuple[str, ...]:
    # off first (dropdown default-most-conservative), then the rest, stable.
    rest = sorted(m for m in VALID_AUTOFIX_MODES if m != "off")
    return ("off", *rest)


class PrDoctor:
    id = "pr-doctor"
    name = "PR Doctor"
    description = "Watches CI on PRs created via ccswitch and restores failing ones."
    modes = _ordered_modes()
    # Audited engine ceiling (Phase 7, Task 7.1; widened #1267). The offline
    # acceptance suite (server/tests/test_pr_doctor_codex_acceptance.py) passes:
    # prompt contract (incl. the commit-trailer wiring in server/sessions/pr_fix.py),
    # launch preconditions, both spawn sites (session-fix, consolidation) resolving
    # engine before the clone/model-assessment tail, and injection into an
    # already-live session (which reuses the shared, already engine-neutral
    # AgentDriver.send_prompt contract and needs no changes here). The suite's own
    # last item — a live Codex session actually restoring or consolidating a real
    # PR — is explicitly NOT covered here; the owner reviewed that gap and accepted
    # it to widen this ceiling now rather than block on it (#1267), the same
    # decision already made for backlog-runner (#1264) and the SDLC/PM origins.
    # Three independent gates still stand between this and a live Codex launch:
    # CCSWITCH_CODEX_AUTOMATIONS_ENABLED (default false, global kill switch), this
    # automation's own `engine` config opt-in (default "claude"), and
    # CCSWITCH_CODEX_UNATTENDED_NETWORK_OPT_IN (default false, required for an
    # unattended Codex session to reach the network at all).
    engine_policy: EnginePolicy = EnginePolicy.CODEX_ALLOWED
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
        ConfigField(key="auto_fix_stuck_enqueue", label="Auto-re-enqueue stuck PRs",
                    type="bool", default=True,
                    help="Mechanical, safe — update-branch triggers fresh CI so GitHub "
                         "re-enqueues an armed PR that was silently dropped from the queue."),
        ConfigField(key="auto_fix_failing_ci", label="Auto-fix failing CI",
                    type="bool", default=True, help="Spawns a session to fix failing CI jobs."),
        ConfigField(key="auto_resolve_conflicts", label="Auto-resolve merge conflicts",
                    type="bool", default=False, help="Risky — spawns a session to rebase/resolve."),
        ConfigField(key="auto_fix_comment_commands", label="Auto-fix @claude review comments",
                    type="bool", default=True,
                    help="Spawns a session to address @claude fix directives on Initiative PRs "
                         "(REVIEW.md policy, Phase 4 #806). The fix agent cannot approve the PR "
                         "— separation of duties applies."),
        ConfigField(key="default_model", label="Session model", type="enum",
                    default="auto", choices=AUTOMATION_MODEL_CHOICES,
                    help="Model for spawned fix/consolidation sessions. 'auto' "
                         "assesses each job's complexity and picks a tier; "
                         "'default' uses the account's default model."),
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
