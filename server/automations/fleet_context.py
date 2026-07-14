"""Pure assembly of the cross-session "fleet" view for the autopilot decider.

Autopilot judges each stuck session in isolation; this module gives the decider a
compact picture of the REST of the fleet — sibling sessions in the same repo (full
detail), everything else active (one-liners), and recently archived sessions with
their outcomes — so nudges and completion calls account for work other sessions
own or have already landed. NO I/O here: the orchestrator collects the raw
snapshot; this module only shapes it (mirrors autopilot_policy's purity rule)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .completion_bar import PrFacts

# Prompt-budget caps: the decider is a one-shot Haiku call per stuck session per
# tick, so every list is bounded by construction.
MAX_SAME_REPO = 8
MAX_OTHER_ACTIVE = 10
MAX_ARCHIVED = 10
MAX_GOAL_CHARS = 120


@dataclass(frozen=True)
class FleetSession:
    """One active session as the fleet snapshot sees it."""
    session_id: str
    name: str
    repo: str | None
    branch: str | None
    status: str
    goal: str
    pr: str | None = None       # compact PR summary from pr_summary(), or None


@dataclass(frozen=True)
class ArchivedFleetSession:
    """One recently archived session, with its landed outcome."""
    name: str
    repo: str | None
    goal: str
    outcome: str                # e.g. "PR #591 merged" | "archived"
    hours_ago: float


def _clip(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= MAX_GOAL_CHARS else text[: MAX_GOAL_CHARS - 1] + "…"


def pr_summary(facts: "PrFacts | None") -> str | None:
    """One line of verified PR ground-truth: merged beats armed beats open-unarmed
    (the same precedence the completion bar judges by)."""
    if facts is None:
        return None
    if (p := facts.merged_pr) is not None:
        return f"PR #{p.number} merged"
    if (p := facts.armed_pr) is not None:
        return f"PR #{p.number} open (auto-merge armed)"
    if (p := facts.open_unarmed_pr) is not None:
        state = "draft" if p.is_draft else "open"
        return f"PR #{p.number} {state} (not armed)"
    return None


def build_fleet_view(session_id: str, active: list[FleetSession],
                     archived: list[ArchivedFleetSession]) -> dict | None:
    """The per-session view of the fleet snapshot: same-repo siblings in detail,
    other actives as one-liners, recently archived with outcomes. The session
    itself is excluded (its own state is already the DecisionContext). Returns
    None when there is nothing to report so the prompt stays clean."""
    me = next((s for s in active if s.session_id == session_id), None)
    repo = me.repo if me is not None else None

    same_repo: list[dict] = []
    other: list[str] = []
    for s in active:
        if s.session_id == session_id:
            continue
        if repo is not None and s.repo == repo:
            entry: dict = {"session": s.name, "branch": s.branch,
                           "status": s.status, "goal": _clip(s.goal)}
            if s.pr:
                entry["pr"] = s.pr
            same_repo.append(entry)
        else:
            line = " — ".join(x for x in (s.name, s.repo, s.status, _clip(s.goal)) if x)
            if s.pr:
                line += f" ({s.pr})"
            other.append(line)

    arch: list[dict] = []
    for a in archived[:MAX_ARCHIVED]:
        entry = {"session": a.name, "goal": _clip(a.goal),
                 "outcome": a.outcome, "hours_ago": a.hours_ago}
        if a.repo:
            entry["repo"] = a.repo
        arch.append(entry)

    view: dict = {}
    if same_repo:
        view["same_repo_sessions"] = same_repo[:MAX_SAME_REPO]
    if other:
        view["other_active_sessions"] = other[:MAX_OTHER_ACTIVE]
    if arch:
        view["recently_archived"] = arch
    return view or None
