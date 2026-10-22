"""The single source of truth for autopilot's "is this session's goal met?"
decision. Pure — no I/O. Consumed by BOTH the live autopilot gate/decider and the
retro archive audit, so "done" is defined in exactly one place (constitution VI)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from server.pipeline.stage import is_required_ci_red

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


# Detects an explicit no-PR / investigate-only dispatch in task instructions (#2474):
# three sessions since 2026-09-24 (5a756926, ae34025a, 366e8a65) were dispatched with
# instructions forbidding implementation, yet a code-flavored goal title (e.g. "fix the
# resource leak") made `_is_code_goal` demand a PR anyway. The session cannot satisfy
# both its dispatch and the bar, and ends in a `hand_back` reporting the contradiction.
_NO_PR_DISPATCH_RE = re.compile(
    r"\bdo\s+not\s+implement\b"
    r"|\bopen\s+no\s+pr\b"
    r"|\bread[\s-]?only\b"
    r"|\binvestigate[\s-]?only\b",
    re.IGNORECASE,
)


def has_no_pr_dispatch(text: str) -> bool:
    """True when *text* (the session's initial dispatch prompt) explicitly forbids
    opening a PR — "do not implement", "open no PR", "read-only", "investigate only" —
    the phrasing observed in the #2474 hand-back trail."""
    return bool(_NO_PR_DISPATCH_RE.search(text or ""))


# Detects a dispatch that FORBIDS arming auto-merge (#2927, #2932): the map-synthesis
# prompts (``server/pm/classification.py``: the map-changes branch, the coverage
# critic, the governance doc) and the SDLC stage prompt (``server/sdlc/prompts.py``)
# keep a human — or the synthesizer loop — as the merge gate. Their finished state is
# an open, unarmed PR (ready, or a draft — see ``_KEEP_DRAFT_RE``), and the bar's
# generic "arm auto-merge" nudge contradicted them.
_NO_ARM_RE = re.compile(r"\bdo\s+not\s+arm\s+auto[\s-]?merge\b", re.IGNORECASE)


def has_no_arm_instruction(text: str | None) -> bool:
    """True when *text* (the session's initial dispatch prompt) forbids arming
    auto-merge — "Do NOT arm auto-merge"."""
    return bool(_NO_ARM_RE.search(text or ""))


# A no-arm dispatch whose human gate is the DRAFT state itself (#2925): a repo whose
# own automation arms every open non-draft PR (cairnea/KinDash's ops-auto-merge.yml)
# defeats "ready + unarmed", so the onboarding prompts in
# ``server/pm/classification.py`` open a draft and forbid readying it. Its finished
# state is an open draft — the "mark it ready" nudge would hand the PR to that sweep.
_KEEP_DRAFT_RE = re.compile(
    r"\bdo\s+not\s+mark\s+(?:it|the\s+(?:pr|pull\s+request))\s+ready\b", re.IGNORECASE)


# The draft-PR guard (``server/sessions/draft_hold_guard.py``) refuses `gh pr ready`
# on a draft the session did not open unless the user approved it; a nudge to ready
# one must say the same.
_OWN_DRAFT_ONLY = (" — only if this session opened it: a draft opened by someone else "
                   "is a hold, so leave it and hand back instead (or, if the task needs it "
                   "ready, ask the user with `request_draft_ready`)")


def has_keep_draft_instruction(text: str | None) -> bool:
    """True when *text* (the session's initial dispatch prompt) forbids marking its
    PR ready for review — "do NOT mark the PR ready for review"."""
    return bool(_KEEP_DRAFT_RE.search(text or ""))


# A dispatch that forbids opening a PR without forbidding the work itself — "DO NOT
# open a new pull request", "do NOT open another pull request" (push to an existing
# PR branch, or the deliverable is not a PR at all). Weaker than
# ``_NO_PR_DISPATCH_RE``: it does not satisfy the bar on its own, it only makes the
# session's own ``mark_no_pr`` the dispatch's answer rather than an unverified claim
# (#2909) — the claim and the instructions agree, so the "open a PR" nudge cannot be
# right.
_FORBIDS_OPENING_PR_RE = re.compile(
    r"\bdo\s+not\s+(?:open|create)\s+(?:a\s+|an\s+|another\s+|any\s+)?(?:new\s+)?"
    r"(?:pull\s+requests?|prs?)\b",
    re.IGNORECASE,
)


def forbids_opening_pr(text: str) -> bool:
    """True when *text* (the dispatch prompt) tells the session not to open a PR."""
    return bool(_FORBIDS_OPENING_PR_RE.search(text or ""))


@dataclass(frozen=True)
class PrFact:
    number: int
    state: str            # 'open' | 'draft' | 'merged' | 'closed'
    is_draft: bool
    auto_merge_enabled: bool
    url: str
    in_merge_queue: bool = False   # mergeQueueEntry != null
    checks: str = "none"           # rollup: 'passing' | 'failing' | 'pending' | 'none'
    # True when every changed file (files page-1) is under docs/ — a spec/plan-only
    # PR that merging does not mean the implementation landed (#1756).
    is_docs_only: bool = False
    # GitHub mergeStateStatus. With the rollup it tells a required red from one only
    # a non-required check earned (UNSTABLE, #3039); "" = unknown, read fail-safe.
    merge_state_status: str = ""
    # The PR carries ``hold:human`` (``pr_status.HUMAN_HOLD_LABEL``): a human owns
    # its merge, so the bar never asks a session to ready or arm it (#3072).
    human_hold: bool = False

    @classmethod
    def from_info(cls, info) -> "PrFact":
        """The bar's view of a ``PullRequestInfo`` (cached or read live) — one mapping,
        so a polled snapshot and a live re-read are judged identically."""
        return cls(number=info.number, state=info.state, is_draft=info.is_draft,
                   auto_merge_enabled=info.auto_merge_enabled,
                   in_merge_queue=info.in_merge_queue, url=info.url,
                   checks=info.checks, is_docs_only=info.docs_only,
                   merge_state_status=info.merge_state_status,
                   human_hold=bool(getattr(info, "human_hold", False)))

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
        """True when a red check blocks the merge (``stage.is_required_ci_red``). The
        rollup alone reads FAILURE for ANY red check, so an advisory one (e2e, the
        Python 3.14 run) held sessions at "fix the failing CI" on PRs auto-merge was
        about to land (#3039). 'pending' is NOT red: auto-merge will still land the PR
        once running checks pass, so it must not flip the goal-met verdict."""
        return is_required_ci_red(self.checks, self.merge_state_status)


@dataclass(frozen=True)
class PrFacts:
    has_repo: bool
    prs: tuple[PrFact, ...] = ()
    # Commits on the session's checked-out branch that its base (origin's default
    # branch) does not have; None = unknown (no worktree, or git could not say). The
    # only evidence for or against a no-PR claim that the claimant cannot write.
    commits_ahead: int | None = None

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
            "prs": [{"number": p.number, "url": p.url, "state": p.state, "is_draft": p.is_draft,
                     "auto_merge_enabled": p.auto_merge_enabled,
                     "in_merge_queue": p.in_merge_queue, "checks": p.checks,
                     "is_docs_only": p.is_docs_only, "human_hold": p.human_hold}
                    for p in self.prs],
        }


@dataclass(frozen=True)
class BarVerdict:
    met: bool
    reason: str
    remedy: str | None     # None on a not-met verdict = nothing to nudge toward
    confidence: str        # 'high' | 'low'


def _is_code_goal(goal: str) -> bool:
    """A repo-attached goal is code work unless it clearly reads as research/Q&A."""
    return not _RESEARCH_RE.search(goal or "")


# The ``pr_resolution_source`` values under which a stored resolution is the session's
# OWN claim about its own work — whether it arrived as a ``<!-- viberpit:no-pr -->``
# marker or as a ``mark_no_pr`` tool call (see ``SessionOrchestrator._persist_status``).
# Every other source has already been judged (``classifier``) or is not a completion
# claim at all (``queue-drain``: the session never ran). A set, not a constant: the
# transport changed once and will change again, and a single-value comparison silently
# promotes a new transport's claim to a verdict.
SELF_CERTIFIED_SOURCES = frozenset({"marker", "tool"})

#: The source for a no-PR resolution the HARNESS verified by observing the deliverable,
#: written by ``SessionOrchestrator._certify_harvested_complete``. Deliberately NOT in
#: ``SELF_CERTIFIED_SOURCES``, and the assertion that it is not lives in
#: ``test_pm_orchestrator_seams.py``: a one-shot session whose whole objective is to
#: emit a fenced JSON block cannot open a PR, and telling it to call ``mark_no_pr``
#: instead would land on source ``tool`` and trip the unverified-self-claim branch. The
#: harvest — proposal rows actually written from its output — is verification by
#: something other than the claimant, which is the entire distinction this set encodes.
HARVEST_RESOLUTION_SOURCE = "harvest"

#: The source for a no-PR resolution the HARNESS verified by observing the REPOSITORY,
#: written by ``SessionOrchestrator._pm_onboard_observe_map_diff_not_needed``. A
#: map-diff session's objective is conditional — a feature doc is owed only when the
#: cluster is unmapped — so ``mark_no_pr`` (not-needed) is a first-class outcome there,
#: not a shortfall. It is still the claimant's word until something else checks it: the
#: harness reads the feature docs on ``origin/<default>`` and confirms the driving
#: items are already cited (or the suggested doc already exists). Same shape as
#: ``harvest``, and for the same reason NOT a member of ``SELF_CERTIFIED_SOURCES``.
OBSERVED_RESOLUTION_SOURCE = "observed"


def is_unverified_self_resolution(goal: str, facts: PrFacts,
                                  pr_resolution: str | None,
                                  pr_resolution_source: str | None) -> bool:
    """True when *pr_resolution* is the session's own ``not-needed`` claim — by
    marker OR by ``mark_no_pr`` tool call — on a repo-attached code goal with no PR
    behind it: the one shape where the claim contradicts the goal unless something
    other than the claimant verifies it.

    2026-09-06 (session fdc6b27a): an "implement X with persistent storage" session
    researched, wrote no code, recommended a spec, offered to draft it and emitted
    the no-PR marker; the marker's token then satisfied the bar with high
    confidence. The agent's claim was its own verification. Which transport carried
    the claim is irrelevant to that — hence ``SELF_CERTIFIED_SOURCES``, not a single
    value. ``cancelled`` is a decision rather than a completion claim and stays
    trusted; a research/Q&A goal or a repo-less session is where a bare no-PR is met
    anyway, so nothing to verify."""
    return (pr_resolution == "not-needed"
            and pr_resolution_source in SELF_CERTIFIED_SOURCES
            and facts.has_repo and _is_code_goal(goal))


def met_bar(goal: str, goal_source: str, facts: PrFacts,
            pr_resolution: str | None = None, *,
            pr_resolution_source: str | None = None,
            instructions: str | None = None,
            pr_resolution_note: str | None = None,
            claim_confirmed: bool = False) -> BarVerdict:
    """Is the session's goal genuinely met? Precedence: merged > armed >
    open-unarmed > no-PR-dispatch(instructions) > pr_resolution(no-PR, verified) >
    pr_resolution(no-PR, self-claimed) > instructions-arm-required > no-PR-nudge.
    Only high-confidence verdicts are safe
    to auto-act on. A real PR fact always overrides a pr_resolution (a stale 'no PR'
    claim never masks an actual PR).

    ``pr_resolution_source`` decides whether a resolution is a verdict or a claim:
    a self-certified ``not-needed`` (``marker`` or ``tool``) on a repo-attached code goal
    (``is_unverified_self_resolution``) does NOT short-circuit — it lands on the
    low-confidence code-goal branch with a remedy that tells the session to finish
    or ask, so the caller's classifier salvage judges the claim against the goal.
    When ``facts.commits_ahead`` is 0 that verdict carries NO remedy: a not-met
    verdict with ``remedy=None`` means there is nothing to nudge toward, and callers
    must not nudge. New commits after the claim restore the remedy.
    ``None`` (callers without provenance) keeps the resolution trusted, and so does
    a self-certified one whose ``instructions`` themselves forbid opening a PR
    (``forbids_opening_pr``, #2909).

    A self-certified no-PR claim is ACCEPTED rather than nudged when it is reasoned
    or confirmed (#2915/#2917/#2918): ``claim_confirmed`` (a human steered the
    session after its last completion nudge — the user answered the very question
    the nudge told it to ask) makes it a high-confidence verdict; a
    ``pr_resolution_note`` (``mark_no_pr``'s stated reason) makes it a met verdict at
    LOW confidence, which archives without another nudge while the retro audit still
    reports it for review. A bare claim (the marker path carries no reason) keeps the
    unverified branch below. Re-sending the same "open a PR" demand to a session that
    has answered it with a reason or with the user's word cannot be right; at worst
    the claim is wrong and the audit says so.
    ``instructions`` is the session's initial prompt (or equivalent task text).
    When it contains an explicit ``--squash --auto`` arming step, the bar
    requires an armed or merged PR regardless of how the goal text is worded —
    overriding the non-code/research escape hatch that would otherwise return
    ``met=True`` with no PR in the cache.

    When ``instructions`` instead explicitly forbids opening a PR (#2474: "do not
    implement", "open no PR", "read-only", "investigate only"), the bar treats that
    as the resolution — ahead of ``pr_resolution`` and the arm-step check — so a
    code-flavored goal title cannot out-vote the session's own dispatch and push it
    into a self-contradictory nudge.

    When ``instructions`` forbid arming auto-merge (``has_no_arm_instruction``), an
    open, ready, non-red PR is met: the dispatch owns a human merge gate, so the
    open-unarmed nudge to arm would contradict it (#2927, #2932). When they also
    forbid readying it (``has_keep_draft_instruction``), an open, non-red DRAFT is
    met instead (#2925)."""
    merged = facts.merged_pr
    if merged is not None and not (merged.is_docs_only and _is_code_goal(goal)):
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
    if unarmed is not None and unarmed.human_hold:
        # A held PR's merge belongs to a human whatever the dispatch says (#3072):
        # the generic "ready it and arm auto-merge" nudge below re-armed
        # cairnea/KinDash#19061 and sent it into the merge queue unreviewed.
        if unarmed.ci_is_red:
            return BarVerdict(
                False, f"PR #{unarmed.number} is open but a required check is failing",
                f"fix the failing CI on PR #{unarmed.number} (do not arm auto-merge or "
                "mark it ready — it carries `hold:human`)", "high")
        return BarVerdict(
            True, f"PR #{unarmed.number} open and held for a human (`hold:human`); "
            "its merge is theirs", None, "high")
    if unarmed is not None and has_no_arm_instruction(instructions):
        # The dispatch keeps the merge for a human: ready and green is finished —
        # or draft and green, when the draft state is the gate (#2925).
        is_draft = unarmed.is_draft or unarmed.state == "draft"
        keep_draft = is_draft and has_keep_draft_instruction(instructions)
        if is_draft and not keep_draft:
            return BarVerdict(
                False, f"PR #{unarmed.number} is still a draft",
                f"mark it ready for review: `gh pr ready {unarmed.number}` "
                "(do not arm auto-merge — the dispatch forbids it)" + _OWN_DRAFT_ONLY,
                "high")
        if unarmed.ci_is_red:
            return BarVerdict(
                False, f"PR #{unarmed.number} is open but a required check is failing",
                f"fix the failing CI on PR #{unarmed.number} (do not arm auto-merge — "
                "the dispatch forbids it)", "high")
        if keep_draft:
            return BarVerdict(
                True, f"PR #{unarmed.number} open as a draft; the dispatch keeps it one "
                "(human merge gate)", None, "high")
        return BarVerdict(
            True, f"PR #{unarmed.number} open and ready; the dispatch forbids arming "
            "auto-merge (human merge gate)", None, "high")
    if unarmed is not None:
        return BarVerdict(
            False, f"PR #{unarmed.number} is open but not armed",
            f"ready the PR and arm auto-merge: `gh pr ready {unarmed.number}` then "
            f"`gh pr merge {unarmed.number} --squash --auto`" + _OWN_DRAFT_ONLY, "high")
    if instructions and has_no_pr_dispatch(instructions):
        return BarVerdict(
            True, "dispatch instructions explicitly forbid opening a PR "
            "(investigate-only/read-only)", None, "high")
    # A mark_no_pr the dispatch itself asked for is the dispatch's answer, not an
    # unverified claim to nudge against (#2909).
    self_claim = (is_unverified_self_resolution(goal, facts, pr_resolution,
                                                pr_resolution_source)
                  and not (instructions and forbids_opening_pr(instructions)))
    if pr_resolution and not self_claim:
        return BarVerdict(True, f"session resolved no PR needed ({pr_resolution})",
                          None, "high")
    if self_claim and claim_confirmed:
        return BarVerdict(True, f"session resolved no PR needed ({pr_resolution}), "
                          "confirmed by the user after the completion nudge", None, "high")
    note = (pr_resolution_note or "").strip()
    if self_claim and note:
        return BarVerdict(True, f"session resolved no PR needed ({pr_resolution}): {note}",
                          None, "low")
    # A self-declared no-PR is judged BEFORE the arm step below, and always at low
    # confidence: low is what lets the caller's salvage classifier verify the claim.
    # Prod 2026-10-02 (session a782f302, cairnea/KinDash#17544): the backlog prompt's
    # `--squash --auto` step answered three acknowledged `mark_no_pr` calls with a
    # high-confidence "open a PR then arm auto-merge", four nudges in all, and the
    # classifier never ran.
    if self_claim:
        if facts.commits_ahead == 0:
            # The branch carries nothing a PR could hold, so there is nothing to
            # nudge toward: no remedy. Still not met — whether the work was owed
            # at all (fdc6b27a) is the classifier's call, not the claimant's.
            return BarVerdict(
                False, "session declared no PR needed and its branch has no commits "
                "ahead of its base (unverified)", None, "low")
        return BarVerdict(
            False, "code goal but no PR opened — session self-declared no PR needed "
            "(unverified)",
            "the objective is a code change, but no PR exists and the transcript does "
            "not show the goal is already met. Either finish the work (open a PR and arm "
            "auto-merge), or — if you need a decision from the user — ask it as a direct "
            "question and do not signal completion until you have an answer.", "low")
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
