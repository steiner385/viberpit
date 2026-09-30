"""ActionProvider implementations — the per-automation manual-action surface.

Each provider wraps the orchestrator's existing recovery machinery and returns
the uniform ``ActionableItem`` / ``TriggerResult`` DTOs. No new recovery logic
lives here — providers only enumerate items and map status strings to
user-facing level/message pairs.
"""
from __future__ import annotations

from typing import Any

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
    "refused": ("warn", "Launch refused by engine policy — see the automation feed"),
}


def foreign_holder_message(op) -> str:
    """Human-readable identity of an ``AutomationOperation`` blocking a PR
    Doctor claim on a foreign target (typically a Backlog Runner close-out
    still holding a PR's linked issue) — for the actionable-items status row
    and the ``ownership_wait`` refusal reason alike (#2203), instead of the
    generic "target already has an operation"."""
    from server.automations.registry import registry
    owner_name = registry.get(op.owner).name
    label = "close-out" if op.source == "terminal-closeout" else op.source
    runner_target = op.provenance.get("origin_ref") or ", ".join(sorted(op.display_refs.values()))
    if op.session_id or op.reserved_session_id or op.phase not in ("completed", "released", "transferred"):
        return f"Held by {owner_name} {label} of {runner_target} — in progress, try again shortly"
    return f"Held by {owner_name} {label} of {runner_target} (needs human)"


class PrDoctorActionProvider:
    valid_kinds = frozenset(_PR_KIND_LABEL)

    def __init__(self, orch) -> None:
        self._orch = orch

    async def actionable(self) -> list[ActionableItem]:
        by_number: dict[tuple[str, int], ActionableItem] = {}
        for repo, branch in await self._orch._pr_doctor_scope_pairs():
            info = self._orch._pr_doctor_info_for(repo, branch)
            if info is None or info.state in ("merged", "closed"):
                continue
            kind = engine.classify(info)
            if kind not in _PR_KIND_LABEL:
                continue
            key = (repo, info.number)
            if key in by_number:
                continue
            item_id = f"{repo}#{info.number}"
            # A live predecessor makes the click a guaranteed refusal, so show why
            # as plain text instead of offering an inert button.
            busy, sid = await self._orch._pr_doctor_busy_predecessor(repo, branch, item_id)
            if busy:
                by_number[key] = ActionableItem(
                    id=item_id, title=info.title, url=info.url,
                    status=f"Being worked by session {sid}" if sid else "Being worked by a prior session")
                continue
            # A foreign operation (e.g. a Backlog Runner close-out) can still
            # hold a PR's LINKED ISSUE even when pr-doctor's own claim on the
            # PR itself is free — the busy check above only mirrors pr-doctor's
            # own ownership token. Without this, the button renders live but
            # every click is a guaranteed OwnershipConflict bounce (#2203).
            blocker = await self._orch._pr_doctor_foreign_operation_cached(repo, info.number)
            if blocker is not None:
                by_number[key] = ActionableItem(
                    id=item_id, title=info.title, url=info.url,
                    status=foreign_holder_message(blocker))
                continue
            by_number[key] = ActionableItem(
                id=item_id, title=info.title, url=info.url,
                actions=[ActionSpec(
                    kind=kind, label=_PR_KIND_LABEL[kind],
                    style="risky" if kind in _PR_RISKY else "default")])
        return [by_number[k] for k in sorted(by_number)]

    async def trigger(self, item_id: str, kind: str, **_kw: Any) -> TriggerResult:
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
        # failing_ci / merge_conflict → the clone→assess→spawn pipeline is 12–41 s,
        # too long to block the edge→daemon RPC (issue #668). Hand it to a
        # background job and answer immediately with the id the UI polls.
        rec = self._orch._pr_doctor_owning_rec(repo, branch)
        job_id = await self._orch._pr_doctor_trigger_async(repo, branch, info, kind, rec)
        return _pr_queued_result(job_id)

    async def consolidate(self, repo: str, numbers: list[int]) -> TriggerResult:
        status, _sid = await self._orch._pr_doctor_consolidate(repo, numbers)
        return _consolidate_result(status)


def _pr_result(status: str) -> TriggerResult:
    level, message = _PR_STATUS_MAP.get(status, ("error", "Action failed — see the feed"))
    return TriggerResult(status=status, level=level, message=message)


def _pr_queued_result(job_id: str) -> TriggerResult:
    """The immediate answer for an async session-fix (issue #668): the job is
    queued; the UI polls ``pr_doctor_job`` for the live phase and final outcome."""
    return TriggerResult(status="queued", level="ok",
                         message="Queued — cloning, assessing, then spawning…",
                         job_id=job_id, phase="queued")


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
    "busy": ("warn", "Waiting for existing work on this batch — see the feed"),
    "capacity": ("warn", "Implementation capacity is full — wait for a slot"),
    "budget": ("warn", "Daily implementation budget reached — try later"),
    "uncertain": ("warn", "Launch outcome unknown; targets remain reserved — see the feed"),
    "no_account": ("warn", "All accounts at/above usage ceiling — try later"),
    "unknown": ("warn", "No longer a current candidate"),
    # The batch failed the live pre-spawn re-check (#1523) — closed, assigned or
    # newly blocked since it was proposed, or GitHub could not be asked and the
    # check failed closed. One status for both: the feed event carries which.
    "not_ready": ("warn", "Not ready to implement right now — see the feed"),
    # The launch was declined before a session existed — an engine the origin is
    # not audited for, or an engine/permission-mode pair that could not open the
    # PR the run exists to produce. The claim was returned to the pool; the
    # automation feed carries the specific reason.
    "refused": ("error", "Launch refused for this origin — see the feed"),
}


class BacklogRunnerActionProvider:
    valid_kinds = frozenset({"implement"})

    def __init__(self, orch) -> None:
        self._orch = orch

    async def actionable(self) -> list[ActionableItem]:
        items: list[ActionableItem] = []
        for c in await self._orch.backlog_candidates(include_pending=True):
            items.append(ActionableItem(
                id=c["ref"], title=c.get("title") or c["ref"], subtitle=c.get("repo"),
                url=c.get("url"),
                actions=[ActionSpec(kind="implement",
                                    label="Retry implementation" if c.get("waiting_account") else "Implement",
                                    style="default")]))
        return items

    async def trigger(self, item_id: str, kind: str, **_kw: Any) -> TriggerResult:
        status = await self._orch.backlog_runner_implement_core(item_id)
        level, message = _BACKLOG_STATUS_MAP.get(status, ("error", "Action failed — see the feed"))
        return TriggerResult(status=status, level=level, message=message)


# --- Archive Audit ---------------------------------------------------------

_ARCHIVE_AUDIT_STATUS_MAP = {
    "retained": ("ok", "Kept the completed state"),
    "reopened": ("ok", "Reopened — the session is idle; no turn was started"),
    "already_decided": ("warn", "This finding was already decided"),
    "not_found": ("warn", "Finding no longer exists"),
    "unarchive_failed": ("error", "Could not reopen the session — see the automation feed"),
}


class ArchiveAuditActionProvider:
    """Open Archive Audit review findings: an archived session that no longer meets
    the completion bar, waiting for a human to retain it or reopen it (#1611).

    Reopening only unarchives — it never starts a turn — and the decision is durable:
    a later tick will not re-flag a decided finding."""

    valid_kinds = frozenset({"retain", "reopen"})

    def __init__(self, orch) -> None:
        self._orch = orch

    async def actionable(self) -> list[ActionableItem]:
        return [ActionableItem(
            id=f["id"], title=f.get("goal") or f["session_id"] or f["id"],
            subtitle=f.get("repo"), status=None,
            actions=[ActionSpec(kind="retain", label="Retain completed", style="default"),
                     ActionSpec(kind="reopen", label="Reopen", style="risky")])
            for f in await self._orch.archive_audit_open_findings()]

    async def trigger(self, item_id: str, kind: str, **_kw: Any) -> TriggerResult:
        status = await self._orch.archive_audit_decide_finding(item_id, kind)
        level, message = _ARCHIVE_AUDIT_STATUS_MAP.get(status, ("error", "Action failed — see the feed"))
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

    async def trigger(self, item_id: str, kind: str, **_kw: Any) -> TriggerResult:
        status = await self._orch.cicd_file_issue_now(item_id)
        level, message = _CICD_STATUS_MAP.get(status, ("error", "Action failed — see the feed"))
        return TriggerResult(status=status, level=level, message=message)
