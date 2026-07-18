"""Pure Backlog Runner helpers (no I/O)."""
from __future__ import annotations

from dataclasses import dataclass, field

from server.github.pr_status import PullRequestInfo


@dataclass(frozen=True)
class Batch:
    """One unit of work the runner spawns a session for: a primary issue key
    (``"owner/repo#123"``) plus the issues bundled into its PR and the repo
    they belong to.  This is the contract the injected ``select_batch`` seam
    returns; the loop reads ``batch.primary`` / ``batch.issues``."""

    primary: str
    issues: list[str] = field(default_factory=list)
    repo: str = ""
    # ref → issue title for the batched issues; feeds the "auto" model
    # assessor's job summary (refs alone carry no complexity signal).
    titles: dict[str, str] = field(default_factory=dict)


def should_free_slot(pr_info: PullRequestInfo, pr_doctor_active: bool) -> bool:
    """A runner slot frees when the work item's PR has reached the point where
    something else carries it: PR Doctor keeps CI green toward auto-merge, so if
    PR Doctor covers the repo, free once the PR is open AND auto-merge is armed;
    otherwise (no safety net) hold until the PR actually merges."""
    if pr_info.state == "merged":
        return True
    if pr_doctor_active:
        return pr_info.state in ("open", "draft") and pr_info.auto_merge_enabled
    return False
