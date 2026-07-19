"""ActionProvider implementations — the per-automation manual-action surface.

Each provider wraps the orchestrator's existing recovery machinery and returns
the uniform ``ActionableItem`` / ``TriggerResult`` DTOs. No new recovery logic
lives here — providers only enumerate items and map status strings to
user-facing level/message pairs.
"""
from __future__ import annotations

from server.automations import pr_doctor_engine as engine
from server.automations.base import ActionableItem, ActionSpec, TriggerResult

# --- PR Doctor -------------------------------------------------------------

_PR_KIND_LABEL = {
    "failing_ci": "Fix CI",
    "merge_conflict": "Resolve conflicts",
    "out_of_date": "Update branch",
    "stuck_enqueue": "Re-enqueue",
}
_PR_RISKY = {"merge_conflict"}

# status → (level, message). Statuses come from the pr-doctor session/mechanical executors:
#   session executor: sent | spawned | ceiling | transient | clone_failed | spawn_failed
#   mechanical:       updated | failed
#   gates:            no_pr | not_actionable
_PR_STATUS_MAP = {
    "spawned": ("ok", "Spawned fix session"),
    "sent": ("ok", "Sent fix prompt to live session"),
    "updated": ("ok", "Branch updated — CI will re-run"),
    "ceiling": ("warn", "All accounts at/above usage ceiling — try later"),
    "transient": ("warn", "Session busy — try again shortly"),
    "not_actionable": ("warn", "PR is no longer actionable"),
    "no_pr": ("warn", "PR not found in the poll cache"),
    "clone_failed": ("error", "Clone failed — see the automation feed"),
    "spawn_failed": ("error", "Spawn failed — see the automation feed"),
    "failed": ("error", "Branch update failed — see the automation feed"),
}


class PrDoctorActionProvider:
    valid_kinds = frozenset(_PR_KIND_LABEL)

    def __init__(self, orch) -> None:
        self._orch = orch

    async def actionable(self) -> list[ActionableItem]:
        by_number: dict[tuple[str, int], ActionableItem] = {}
        for repo, branch in await self._orch._pr_doctor_scope_pairs():
            info = self._orch.pr_for(repo, branch) or self._orch._pr_population_pr(repo, branch)
            if info is None:
                continue
            # Reconcile: per-branch cache can be stale after a merge; if population
            # says the PR is terminal while per-branch still shows it open, skip it.
            if info.state not in ("merged", "closed"):
                pop = self._orch._pr_population_pr(repo, branch)
                if pop is not None and pop.state in ("merged", "closed"):
                    continue
            kind = engine.classify(info)
            if kind not in _PR_KIND_LABEL:
                continue
            key = (repo, info.number)
            if key in by_number:
                continue
            by_number[key] = ActionableItem(
                id=f"{repo}#{info.number}", title=info.title, url=info.url,
                actions=[ActionSpec(
                    kind=kind, label=_PR_KIND_LABEL[kind],
                    style="risky" if kind in _PR_RISKY else "default")])
        return [by_number[k] for k in sorted(by_number)]

    async def trigger(self, item_id: str, kind: str) -> TriggerResult:
        repo, _, num = item_id.rpartition("#")
        try:
            number = int(num)
        except ValueError:
            return _pr_result("no_pr")
        info = branch = None
        for r, br in await self._orch._pr_doctor_scope_pairs():
            if r != repo:
                continue
            cached = self._orch.pr_for(repo, br) or self._orch._pr_population_pr(repo, br)
            if cached is not None and cached.number == number:
                info, branch = cached, br
                break
        if info is None or branch is None:
            return _pr_result("no_pr")
        if info.state not in ("open", "draft"):
            return _pr_result("not_actionable")
        if kind == "out_of_date":
            ok = await self._orch._pr_doctor_mechanical(repo, info, kind)
            return _pr_result("updated" if ok else "failed")
        rec = self._orch._pr_doctor_owning_rec(repo, branch)
        status, _sid = await self._orch._pr_doctor_session_fix(repo, branch, info, kind, rec)
        return _pr_result(status)

    async def consolidate(self, repo: str, numbers: list[int]) -> TriggerResult:
        status, _sid = await self._orch._pr_doctor_consolidate(repo, numbers)
        return _consolidate_result(status)


def _pr_result(status: str) -> TriggerResult:
    level, message = _PR_STATUS_MAP.get(status, ("error", "Action failed — see the feed"))
    return TriggerResult(status=status, level=level, message=message)


# Only the statuses the synchronous fast path can answer: the clone/model/spawn
# tail runs in a background task and reports its outcome (spawned, ceiling
# deferral, clone/spawn failure) through the automation feed instead (issue #567).
_CONSOLIDATE_STATUS_MAP = {
    "spawning": ("ok", "Spawning consolidation session — follow the automation feed"),
    "duplicate": ("warn", "Already consolidating this set"),
    "too_few": ("warn", "Select at least 2 PRs"),
    "not_actionable": ("warn", "One or more PRs are no longer open"),
}


def _consolidate_result(status: str) -> TriggerResult:
    level, message = _CONSOLIDATE_STATUS_MAP.get(
        status, ("error", "Consolidation failed — see the feed"))
    return TriggerResult(status=status, level=level, message=message)


# --- Backlog Runner --------------------------------------------------------

_BACKLOG_STATUS_MAP = {
    "spawned": ("ok", "Spawned implementation session"),
    "exists": ("warn", "Already running"),
    "claim_failed": ("error", "Could not claim the issue — see the feed"),
    "no_account": ("warn", "All accounts at/above usage ceiling — try later"),
    "unknown": ("warn", "No longer a current candidate"),
}


class BacklogRunnerActionProvider:
    valid_kinds = frozenset({"implement"})

    def __init__(self, orch) -> None:
        self._orch = orch

    async def actionable(self) -> list[ActionableItem]:
        items: list[ActionableItem] = []
        for c in await self._orch.backlog_candidates():
            items.append(ActionableItem(
                id=c["ref"], title=c.get("title") or c["ref"], subtitle=c.get("repo"),
                url=c.get("url"),
                actions=[ActionSpec(kind="implement", label="Implement", style="default")]))
        return items

    async def trigger(self, item_id: str, kind: str) -> TriggerResult:
        status = await self._orch.backlog_runner_implement_core(item_id)
        level, message = _BACKLOG_STATUS_MAP.get(status, ("error", "Action failed — see the feed"))
        return TriggerResult(status=status, level=level, message=message)


# --- CI/CD Doctor ----------------------------------------------------------

_CICD_STATUS_MAP = {
    "filed": ("ok", "Filed ci-doctor issue"),
    "exists": ("warn", "An issue is already filed for this finding"),
    "unknown": ("warn", "Finding is no longer open"),
    "file_failed": ("error", "Could not file the issue — see the automation feed"),
}


class CicdDoctorActionProvider:
    """Surface open CI/CD Doctor findings that still need a GitHub issue filed.

    The loop auto-files *actionable* findings on its tick; this lets a human file
    one immediately (or file a graduation candidate the loop deliberately skips)
    without waiting. Reuses the loop's issue-filing machinery — no new recovery
    logic. Findings that already link an issue expose no action.
    """

    valid_kinds = frozenset({"file_issue"})

    def __init__(self, orch) -> None:
        self._orch = orch

    async def actionable(self) -> list[ActionableItem]:
        items: list[ActionableItem] = []
        for f in await self._orch.cicd_actionable_findings():
            items.append(ActionableItem(
                id=f["id"], title=f.get("title") or f["id"], subtitle=f.get("repo"),
                url=f.get("url"),
                actions=[ActionSpec(kind="file_issue", label="File issue", style="default")]))
        return items

    async def trigger(self, item_id: str, kind: str) -> TriggerResult:
        status = await self._orch.cicd_file_issue_now(item_id)
        level, message = _CICD_STATUS_MAP.get(status, ("error", "Action failed — see the feed"))
        return TriggerResult(status=status, level=level, message=message)
