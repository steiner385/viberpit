"""The single source of truth for autopilot's "is this session's goal met?"
decision. Pure — no I/O. Consumed by BOTH the live autopilot gate/decider and the
retro archive audit, so "done" is defined in exactly one place (constitution VI)."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Research-escape signal: a goal that reads as investigation/Q&A rather than a code
# change is allowed to complete with no PR. Kept deliberately narrow — when unsure we
# treat a repo-attached session as code work (the safe direction: it only ever routes
# a no-PR session to low-confidence "report only", never to auto-unarchive).
_RESEARCH_RE = re.compile(
    r"\b(investigate|research|analy[sz]e|summari[sz]e|explain|understand|explore|"
    r"question|answer|diagnose|assess|review|audit|compare|document)\b",
    re.IGNORECASE,
)

# Detects an explicit auto-merge arming step in task instructions.  When a session's
# initial prompt contains `gh pr merge … --squash --auto` the task has a mandatory
# arming step — treat the bar as requiring an armed PR regardless of goal wording.
_SQUASH_AUTO_RE = re.compile(
    r"gh\s+pr\s+merge\b[^`\n]*--squash[^`\n]*--auto"
    r"|--squash[^`\n]*--auto",
    re.IGNORECASE,
)


def _has_explicit_arm_step(text: str) -> bool:
    """True when *text* contains an explicit `gh pr merge --squash --auto` step."""
    return bool(_SQUASH_AUTO_RE.search(text or ""))


@dataclass(frozen=True)
class PrFact:
    number: int
    state: str            # 'open' | 'draft' | 'merged' | 'closed'
    is_draft: bool
    auto_merge_enabled: bool
    url: str
    in_merge_queue: bool = False   # mergeQueueEntry != null
    checks: str = "none"           # rollup: 'passing' | 'failing' | 'pending' | 'none'

    @property
    def is_armed(self) -> bool:
        """True when this PR is queued to merge via auto-merge or the merge queue."""
        return (
            self.state == "open"
            and not self.is_draft
            and (self.auto_merge_enabled or self.in_merge_queue)
        )

    @property
    def ci_is_red(self) -> bool:
        """True when the check rollup is FAILURE/ERROR — a required check is red, so
        an armed merge is blocked. 'pending' is NOT red: auto-merge will still land
        the PR once running checks pass, so it must not flip the goal-met verdict."""
        return self.checks == "failing"


@dataclass(frozen=True)
class PrFacts:
    has_repo: bool
    prs: tuple[PrFact, ...] = ()

    @property
    def merged_pr(self) -> "PrFact | None":
        return next((p for p in self.prs if p.state == "merged"), None)

    @property
    def armed_pr(self) -> "PrFact | None":
        return next((p for p in self.prs if p.is_armed), None)

    @property
    def open_unarmed_pr(self) -> "PrFact | None":
        return next((p for p in self.prs
                     if p.state in ("open", "draft") and not p.is_armed),
                    None)

    def to_prompt(self) -> dict:
        return {
            "has_repo": self.has_repo,
            "prs": [{"number": p.number, "state": p.state, "is_draft": p.is_draft,
                     "auto_merge_enabled": p.auto_merge_enabled,
                     "in_merge_queue": p.in_merge_queue, "checks": p.checks}
                    for p in self.prs],
        }


@dataclass(frozen=True)
class BarVerdict:
    met: bool
    reason: str
    remedy: str | None
    confidence: str        # 'high' | 'low'


def _is_code_goal(goal: str) -> bool:
    """A repo-attached goal is code work unless it clearly reads as research/Q&A."""
    return not _RESEARCH_RE.search(goal or "")


def met_bar(goal: str, goal_source: str, facts: PrFacts,
            pr_resolution: str | None = None, *,
            instructions: str | None = None) -> BarVerdict:
    """Is the session's goal genuinely met? Precedence: merged > armed >
    open-unarmed > pr_resolution(no-PR) > instructions-arm-required > no-PR-nudge.
    Only high-confidence verdicts are safe to auto-act on. A real PR fact always
    overrides a pr_resolution (a stale 'no PR' claim never masks an actual PR).

    ``instructions`` is the session's initial prompt (or equivalent task text).
    When it contains an explicit ``--squash --auto`` arming step, the bar
    requires an armed or merged PR regardless of how the goal text is worded —
    overriding the non-code/research escape hatch that would otherwise return
    ``met=True`` with no PR in the cache."""
    merged = facts.merged_pr
    if merged is not None:
        return BarVerdict(True, f"code landed (PR #{merged.number} merged)", None, "high")
    armed = facts.armed_pr
    if armed is not None:
        # Issue #635: auto-merge being armed is not terminal success — a red required
        # check blocks the merge from ever executing, so the goal is not met until CI
        # goes green. Treat as in-progress (not-met/high) so the session stays alive.
        if armed.ci_is_red:
            return BarVerdict(
                False, f"PR #{armed.number} is armed but a required check is failing — "
                "auto-merge is blocked",
                f"fix the failing CI on PR #{armed.number}; auto-merge will land it once "
                "checks pass", "high")
        return BarVerdict(True, f"PR #{armed.number} open with auto-merge armed", None, "high")
    unarmed = facts.open_unarmed_pr
    if unarmed is not None:
        return BarVerdict(
            False, f"PR #{unarmed.number} is open but not armed",
            f"ready the PR and arm auto-merge: `gh pr ready {unarmed.number}` then "
            f"`gh pr merge {unarmed.number} --squash --auto`", "high")
    if pr_resolution:
        return BarVerdict(True, f"session resolved no PR needed ({pr_resolution})",
                          None, "high")
    # When the task instructions include an explicit arming step (`--squash --auto`),
    # the session is not done until that step completes — even when no PR is visible
    # in the poller cache yet (race) or the goal text looks like research work.
    if instructions and _has_explicit_arm_step(instructions):
        return BarVerdict(
            False, "task instructions require auto-merge but no armed PR found",
            "open a PR (if needed) then arm auto-merge: `gh pr merge --squash --auto`",
            "high")
    if facts.has_repo and _is_code_goal(goal):
        return BarVerdict(False, "code goal but no PR opened",
                          "open a PR and arm auto-merge before finishing", "low")
    return BarVerdict(True, "non-code goal — no PR expected", None, "low")
