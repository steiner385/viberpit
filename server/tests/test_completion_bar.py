from server.automations.completion_bar import (
    SELF_CERTIFIED_SOURCES,
    PrFact,
    PrFacts,
    _has_explicit_arm_step,
    forbids_opening_pr,
    has_keep_draft_instruction,
    has_no_pr_dispatch,
    has_no_arm_instruction,
    is_unverified_self_resolution,
    met_bar,
)


def _facts(prs=(), has_repo=True):
    return PrFacts(has_repo=has_repo, prs=tuple(prs))


def _pr(number=1, state="open", is_draft=False, auto_merge=False, in_merge_queue=False,
        checks="none", is_docs_only=False):
    return PrFact(number=number, state=state, is_draft=is_draft,
                  auto_merge_enabled=auto_merge, in_merge_queue=in_merge_queue,
                  url=f"http://x/{number}", checks=checks, is_docs_only=is_docs_only)


def test_merged_pr_is_met_high():
    v = met_bar("fix ci", "explicit", _facts([_pr(state="merged")]))
    assert v.met and v.confidence == "high"


def test_armed_pr_is_met_high():
    v = met_bar("fix ci", "explicit", _facts([_pr(state="open", auto_merge=True)]))
    assert v.met and v.confidence == "high"


def test_open_unarmed_pr_is_not_met_high_with_remedy():
    v = met_bar("fix ci", "explicit", _facts([_pr(state="open", auto_merge=False)]))
    assert not v.met and v.confidence == "high" and v.remedy


def test_draft_pr_is_not_met_high():
    v = met_bar("fix ci", "explicit", _facts([_pr(state="draft", is_draft=True, auto_merge=True)]))
    assert not v.met and v.confidence == "high"


def test_code_goal_no_pr_is_not_met_low():
    v = met_bar("fix the cicd doctor", "explicit", _facts([], has_repo=True))
    assert not v.met and v.confidence == "low"


def test_research_goal_no_pr_is_met_low():
    v = met_bar("investigate why deploys are slow", "explicit", _facts([], has_repo=True))
    assert v.met and v.confidence == "low"


def test_no_repo_no_pr_is_met_low():
    v = met_bar("anything", "initial_prompt", _facts([], has_repo=False))
    assert v.met and v.confidence == "low"


# --- merge-queue enrollment tests ---

def test_queued_pr_is_armed_and_met_high():
    """A PR in merge queue (in_merge_queue=True) should be treated as armed."""
    pr = _pr(state="open", is_draft=False, auto_merge=False, in_merge_queue=True)
    facts = _facts([pr])
    assert facts.armed_pr is pr
    assert facts.open_unarmed_pr is None
    v = met_bar("fix ci", "explicit", facts)
    assert v.met and v.confidence == "high"


def test_classic_auto_merge_pr_still_armed():
    """Regression: classic auto_merge_enabled=True, in_merge_queue=False still armed."""
    pr = _pr(state="open", is_draft=False, auto_merge=True, in_merge_queue=False)
    facts = _facts([pr])
    assert facts.armed_pr is pr
    assert facts.open_unarmed_pr is None
    v = met_bar("fix ci", "explicit", facts)
    assert v.met and v.confidence == "high"


def test_neither_armed_nor_queued_is_open_unarmed():
    """A plain open PR with neither auto_merge nor merge queue → open_unarmed."""
    pr = _pr(state="open", is_draft=False, auto_merge=False, in_merge_queue=False)
    facts = _facts([pr])
    assert facts.armed_pr is None
    assert facts.open_unarmed_pr is pr
    v = met_bar("fix ci", "explicit", facts)
    assert not v.met and v.confidence == "high"


def test_queued_pr_not_in_open_unarmed():
    """A queued PR must NOT appear as open_unarmed (single is_armed definition)."""
    pr = _pr(state="open", is_draft=False, auto_merge=False, in_merge_queue=True)
    facts = _facts([pr])
    assert facts.open_unarmed_pr is None


def test_draft_queued_pr_is_not_armed():
    """A draft PR is not armed even if in merge queue."""
    pr = _pr(state="draft", is_draft=True, auto_merge=False, in_merge_queue=True)
    facts = _facts([pr])
    assert facts.armed_pr is None


# ---------------------------------------------------------------------------
# pr_resolution precedence tests
# ---------------------------------------------------------------------------

def test_pr_resolution_short_circuits_no_pr_branch():
    v = met_bar("fix ci", "explicit", _facts([], has_repo=True), pr_resolution="not-needed")
    assert v.met is True and v.confidence == "high"


def test_pr_resolution_ignored_when_open_unarmed_pr_exists():
    facts = _facts([_pr(state="open", auto_merge=False)])
    v = met_bar("fix ci", "explicit", facts, pr_resolution="cancelled")
    assert v.met is False  # real PR facts win — still nudge to arm it


def test_pr_resolution_ignored_when_merged():
    facts = _facts([_pr(state="merged")])
    v = met_bar("fix ci", "explicit", facts, pr_resolution="cancelled")
    assert v.met is True and "merged" in v.reason


def test_no_resolution_unchanged():
    v = met_bar("fix ci", "explicit", _facts([], has_repo=True))
    assert v.met is False and v.reason == "code goal but no PR opened"


# --- armed-but-red-CI tests (issue #635) ---

def test_armed_pr_with_failing_ci_is_not_met_high():
    """Regression #635: an armed PR whose required CI is red cannot merge, so the
    goal is NOT met — the session must stay alive to fix the failing checks."""
    v = met_bar("fix ci", "explicit",
                _facts([_pr(state="open", auto_merge=True, checks="failing")]))
    assert not v.met and v.confidence == "high" and v.remedy


def test_queued_pr_with_failing_ci_is_not_met_high():
    """Merge-queue enrollment does not rescue a red PR — a failing rollup still
    blocks the merge."""
    v = met_bar("fix ci", "explicit",
                _facts([_pr(state="open", in_merge_queue=True, checks="failing")]))
    assert not v.met and v.confidence == "high" and v.remedy


def test_armed_pr_with_pending_ci_is_still_met_high():
    """A PR with CI still running is legitimately armed — auto-merge will land it
    once checks pass, so 'pending' must NOT flip the verdict."""
    v = met_bar("fix ci", "explicit",
                _facts([_pr(state="open", auto_merge=True, checks="pending")]))
    assert v.met and v.confidence == "high"


def test_armed_pr_with_passing_ci_is_met_high():
    v = met_bar("fix ci", "explicit",
                _facts([_pr(state="open", auto_merge=True, checks="passing")]))
    assert v.met and v.confidence == "high"


def test_merged_pr_ignores_stale_failing_checks():
    """Merged wins over checks: a merged PR is done even if a stale rollup reads
    'failing' (merge precedence is evaluated before the armed CI gate)."""
    v = met_bar("fix ci", "explicit",
                _facts([_pr(state="merged", checks="failing")]))
    assert v.met and v.confidence == "high"


# ---------------------------------------------------------------------------
# docs-only merged PR must not satisfy a code goal (#1756)
# ---------------------------------------------------------------------------

def test_docs_only_merged_pr_does_not_satisfy_code_goal():
    """A spec/plan-only PR (every changed file under docs/) merging does not mean
    the implementation landed — 70904123 was auto-archived on this false premise
    (PR #1644, docs-only, merged) while the code branch was never pushed."""
    v = met_bar("implement automatic roadmap execution", "explicit",
                _facts([_pr(state="merged", is_docs_only=True)]))
    assert not v.met


def test_docs_only_merged_pr_still_meets_research_goal():
    """A docs-only merge is exactly what a research/Q&A goal should produce — the
    disqualification only applies to code goals."""
    v = met_bar("investigate the flaky test", "explicit",
                _facts([_pr(state="merged", is_docs_only=True)]))
    assert v.met


def test_non_docs_only_merged_pr_still_satisfies_code_goal():
    """Regression guard: an ordinary merged PR (not docs-only) is unaffected."""
    v = met_bar("fix ci", "explicit",
                _facts([_pr(state="merged", is_docs_only=False)]))
    assert v.met and v.confidence == "high"


# ---------------------------------------------------------------------------
# explicit arm-step (--squash --auto) in instructions tests
# ---------------------------------------------------------------------------

_ARM_PROMPT = "5. Arm auto-merge: `gh pr merge <#> --squash --auto`."


def test_has_explicit_arm_step_detects_squash_auto():
    assert _has_explicit_arm_step(_ARM_PROMPT) is True


def test_has_explicit_arm_step_false_without_pattern():
    assert _has_explicit_arm_step("Implement the issue and open a PR.") is False


def test_has_explicit_arm_step_empty():
    assert _has_explicit_arm_step("") is False


def test_instructions_with_arm_step_blocks_research_escape_no_pr():
    """A goal that looks like research but whose instructions demand --squash --auto
    must NOT be treated as met when no PR is armed — the arm step wins."""
    v = met_bar("review the consolidation approach", "inferred",
                _facts([], has_repo=True), instructions=_ARM_PROMPT)
    assert not v.met and v.confidence == "high"


def test_instructions_with_arm_step_blocks_no_repo_escape():
    """Even with has_repo=False, explicit arm step blocks the 'no PR expected' escape."""
    v = met_bar("anything", "initial_prompt",
                _facts([], has_repo=False), instructions=_ARM_PROMPT)
    assert not v.met and v.confidence == "high"


def test_instructions_arm_step_does_not_block_when_armed():
    """An armed PR satisfies the bar even when instructions have --squash --auto."""
    v = met_bar("review approach", "inferred",
                _facts([_pr(auto_merge=True)]), instructions=_ARM_PROMPT)
    assert v.met and v.confidence == "high"


def test_instructions_arm_step_does_not_block_when_merged():
    """A merged PR satisfies the bar even when instructions have --squash --auto."""
    v = met_bar("review approach", "inferred",
                _facts([_pr(state="merged")]), instructions=_ARM_PROMPT)
    assert v.met and v.confidence == "high"


def test_instructions_arm_step_open_unarmed_still_not_met():
    """Open-unarmed PR stays not-met with or without arm-step instructions."""
    v = met_bar("fix ci", "explicit",
                _facts([_pr(state="open", auto_merge=False)]), instructions=_ARM_PROMPT)
    assert not v.met and v.confidence == "high"


def test_instructions_none_preserves_existing_research_escape():
    """Without instructions, a research goal with no PR is still met (unchanged behaviour)."""
    v = met_bar("review the consolidation approach", "inferred",
                _facts([], has_repo=True))
    assert v.met and v.confidence == "low"


# ---------------------------------------------------------------------------
# Self-certified (marker-sourced) resolution on a code goal
#
# Prod 2026-09-06 (session fdc6b27a, goal "Ship PR adding persistent color field
# per family member …"): the agent researched, wrote no code, recommended a spec,
# offered to draft it, and ended with `<!-- ccswitch:no-pr -->`. The marker's own
# `not-needed` token then satisfied the bar with HIGH confidence — the session's
# claim was its own verification. A marker-sourced not-needed on a repo-attached
# code goal is a claim to verify (classifier salvage), never a verdict.
# ---------------------------------------------------------------------------

_CODE_GOAL = ("Ship PR adding persistent color field per family member with schema "
              "migration and UI controls.")


def test_marker_not_needed_on_code_goal_is_not_met():
    v = met_bar(_CODE_GOAL, "inferred", _facts([], has_repo=True),
                pr_resolution="not-needed", pr_resolution_source="marker")
    assert v.met is False and v.confidence == "low"
    assert "self-declared" in v.reason
    assert v.remedy and "ask" in v.remedy.lower()


def test_marker_not_needed_on_research_goal_is_met():
    v = met_bar("investigate the flaky ci job", "explicit", _facts([], has_repo=True),
                pr_resolution="not-needed", pr_resolution_source="marker")
    assert v.met is True


def test_marker_not_needed_without_repo_is_met():
    v = met_bar(_CODE_GOAL, "inferred", _facts([], has_repo=False),
                pr_resolution="not-needed", pr_resolution_source="marker")
    assert v.met is True


def test_marker_cancelled_on_code_goal_keeps_precedence():
    """An explicit cancellation is a decision, not a claim of completion."""
    v = met_bar(_CODE_GOAL, "inferred", _facts([], has_repo=True),
                pr_resolution="cancelled", pr_resolution_source="marker")
    assert v.met is True and v.confidence == "high"


def test_classifier_not_needed_on_code_goal_is_met_high():
    """A classifier-verified resolution is the verification — it still short-circuits."""
    v = met_bar(_CODE_GOAL, "inferred", _facts([], has_repo=True),
                pr_resolution="not-needed", pr_resolution_source="classifier")
    assert v.met is True and v.confidence == "high"


# ---------------------------------------------------------------------------
# The transport changed (marker → `mark_no_pr` tool call); the trust boundary
# did not. A session's own claim is a claim regardless of which wire carried it.
# ---------------------------------------------------------------------------


def test_a_tool_sourced_no_pr_claim_is_still_unverified():
    """Session fdc6b27a (2026-09-06) researched, wrote no code, recommended a
    spec and self-certified past the bar. Moving that claim from a marker to a tool
    does not make the session a neutral judge of its own work."""
    assert is_unverified_self_resolution(_CODE_GOAL, _facts([], has_repo=True),
                                         "not-needed", "tool") is True


def test_a_marker_sourced_claim_is_unchanged():
    assert is_unverified_self_resolution(_CODE_GOAL, _facts([], has_repo=True),
                                         "not-needed", "marker") is True


def test_a_classifier_sourced_resolution_is_still_trusted():
    """Something other than the claimant judged it, which is the whole point."""
    assert is_unverified_self_resolution(_CODE_GOAL, _facts([], has_repo=True),
                                         "not-needed", "classifier") is False


def test_a_cancelled_resolution_stays_trusted_from_either_transport():
    """`cancelled` is a decision, not a completion claim."""
    for source in ("marker", "tool"):
        assert is_unverified_self_resolution(_CODE_GOAL, _facts([], has_repo=True),
                                             "cancelled", source) is False


def test_every_source_a_session_can_set_itself_is_self_certifying():
    """Enumerated so a fifth source cannot be added without deciding which side of
    this line it falls on."""
    assert SELF_CERTIFIED_SOURCES == frozenset({"marker", "tool"})


def test_a_tool_sourced_not_needed_does_not_short_circuit_met_bar():
    v = met_bar(_CODE_GOAL, "user", _facts([], has_repo=True), "not-needed",
                pr_resolution_source="tool")
    assert v.met is False
    assert v.confidence != "high"


# ---------------------------------------------------------------------------
# no-PR dispatch instructions (#2474): three sessions since 2026-09-24
# (5a756926, ae34025a, 366e8a65) were dispatched "investigate only, do not
# implement" yet their code-flavored goal title made the bar demand a PR anyway,
# forcing a `hand_back` to report the contradiction between dispatch and gate.
# ---------------------------------------------------------------------------

_INVESTIGATE_ONLY_DISPATCH = (
    "Investigate; do not implement. Read-only on code: open no PR, push nothing."
)
_OPEN_NO_PR_DISPATCH = (
    "Investigate #1978. Open no PR — leave the fix for the runner's implementation "
    "session."
)
_INVESTIGATE_ONLY_LABEL_DISPATCH = (
    "This is an investigate-only dispatch: look into the #2243 log-throttle issue "
    "and report back."
)


def test_has_no_pr_dispatch_detects_all_four_phrasings():
    assert has_no_pr_dispatch(_INVESTIGATE_ONLY_DISPATCH) is True
    assert has_no_pr_dispatch(_OPEN_NO_PR_DISPATCH) is True
    assert has_no_pr_dispatch(_INVESTIGATE_ONLY_LABEL_DISPATCH) is True
    assert has_no_pr_dispatch("read only access to the report") is True


def test_has_no_pr_dispatch_false_without_pattern():
    assert has_no_pr_dispatch("Implement the issue and open a PR.") is False


def test_has_no_pr_dispatch_empty():
    assert has_no_pr_dispatch("") is False


def test_no_pr_dispatch_overrides_code_flavored_goal_no_pr_resolution():
    """A code-flavored goal title with no PR is met=True/high when the dispatch
    instructions explicitly forbid implementation — the #2474 contradiction."""
    v = met_bar("fix the resource leak", "explicit", _facts([], has_repo=True),
                instructions=_INVESTIGATE_ONLY_DISPATCH)
    assert v.met is True and v.confidence == "high"


def test_no_pr_dispatch_overrides_self_declared_no_pr_on_code_goal():
    """A `mark_no_pr` tool-sourced claim on a code goal is normally unverified
    (low confidence, ask); an investigate-only dispatch verifies it instead."""
    v = met_bar(_CODE_GOAL, "inferred", _facts([], has_repo=True),
                pr_resolution="not-needed", pr_resolution_source="tool",
                instructions=_OPEN_NO_PR_DISPATCH)
    assert v.met is True and v.confidence == "high"


def test_no_pr_dispatch_open_no_pr_wording():
    v = met_bar("fix the #1978 bug", "explicit", _facts([], has_repo=True),
                instructions=_OPEN_NO_PR_DISPATCH)
    assert v.met is True and v.confidence == "high"


def test_no_pr_dispatch_investigate_only_label_wording():
    v = met_bar("fix the log-throttle issue", "explicit", _facts([], has_repo=True),
                instructions=_INVESTIGATE_ONLY_LABEL_DISPATCH)
    assert v.met is True and v.confidence == "high"


def test_no_pr_dispatch_does_not_override_a_real_open_unarmed_pr():
    """A real PR fact always wins — a no-PR dispatch cannot mask an actual PR."""
    v = met_bar("fix the resource leak", "explicit",
                _facts([_pr(state="open", auto_merge=False)]),
                instructions=_INVESTIGATE_ONLY_DISPATCH)
    assert v.met is False and v.confidence == "high"


def test_no_pr_dispatch_does_not_override_a_merged_pr():
    v = met_bar("fix the resource leak", "explicit",
                _facts([_pr(state="merged")]),
                instructions=_INVESTIGATE_ONLY_DISPATCH)
    assert v.met is True and "merged" in v.reason


# ---------------------------------------------------------------------------
# A recorded no-PR is a terminal claim, not an invitation to open a PR.
#
# Prod 2026-10-02 (session a782f302, cairnea/KinDash#17544, origin backlog-runner):
# the issue was a CI latch whose fix had already merged, the branch had no commits
# ahead of its base, and the session called `mark_no_pr` (not-needed) three times.
# Each call was acknowledged, and each was answered with a completion nudge "open a
# PR (if needed) then arm auto-merge" — four in all — until it handed back. The
# backlog prompt carries `gh pr merge --squash --auto`, so the arm-step branch ran
# BEFORE the self-claim branch: a high-confidence not-met verdict that skipped the
# salvage classifier and told the session to do the one thing it had declared it
# would not.
# ---------------------------------------------------------------------------

_BACKLOG_PROMPT = ("Fix #17544. Commit, push, open a PR and arm auto-merge: "
                   "`gh pr merge <#> --squash --auto`.")


def _branch(commits_ahead, prs=()):
    return PrFacts(has_repo=True, prs=tuple(prs), commits_ahead=commits_ahead)


def test_a_self_declared_no_pr_is_never_told_to_open_a_pr_by_the_arm_step():
    """The a782f302 shape: the arm step in the dispatch must not out-rank the
    session's own no-PR claim, and must not hide the claim from the salvage
    classifier (which only runs on a low-confidence verdict)."""
    for ahead in (None, 0, 2):
        v = met_bar(_CODE_GOAL, "inferred", _branch(ahead), "not-needed",
                    pr_resolution_source="tool", instructions=_BACKLOG_PROMPT)
        assert v.met is False
        assert v.confidence == "low", ahead
        assert "arm auto-merge: `gh pr merge --squash --auto`" not in (v.remedy or "")


def test_a_no_pr_claim_on_a_branch_with_nothing_ahead_has_no_remedy():
    """Nothing on the branch to open a PR from: the claim matches the repository,
    so there is nothing to nudge toward. Still unverified (low, not met) — the
    salvage classifier decides whether it archives."""
    v = met_bar(_CODE_GOAL, "inferred", _branch(0), "not-needed",
                pr_resolution_source="tool", instructions=_BACKLOG_PROMPT)
    assert v.met is False and v.confidence == "low"
    assert v.remedy is None


def test_a_no_pr_claim_followed_by_new_commits_is_still_nudged():
    """New work after the claim makes the claim stale: commits exist that no PR
    carries, so the session is told to finish (open the PR) or ask."""
    v = met_bar(_CODE_GOAL, "inferred", _branch(3), "not-needed",
                pr_resolution_source="tool", instructions=_BACKLOG_PROMPT)
    assert v.met is False
    assert v.remedy and "open a PR" in v.remedy


def test_an_unknown_ahead_count_keeps_the_self_claim_nudge():
    """No git evidence either way (no worktree, git failed): unchanged from the
    fdc6b27a guard — verify, and nudge to finish or ask when unconfirmed."""
    v = met_bar(_CODE_GOAL, "inferred", _branch(None), "not-needed",
                pr_resolution_source="tool")
    assert v.met is False and v.confidence == "low"
    assert v.remedy and "ask" in v.remedy.lower()


def test_an_empty_branch_without_a_no_pr_claim_is_still_nudged_by_the_arm_step():
    """The ahead count only vouches for a declared no-PR. A session that signals
    plain completion with no PR is still told to open and arm one."""
    v = met_bar(_CODE_GOAL, "inferred", _branch(0), instructions=_BACKLOG_PROMPT)
    assert v.met is False and v.confidence == "high"
    assert "arm auto-merge" in (v.remedy or "")


# A dispatch that forbids arming (the map-synthesis prompts in server/pm/classification.py,
# the SDLC stage prompt in server/sdlc/prompts.py) owns a HUMAN merge gate: an open,
# ready PR is its finished state. Nudging "arm auto-merge" there contradicts the
# dispatch the session was given (#2927, #2932).
_NO_ARM = ("4. Do NOT arm auto-merge on it: a human merges or abandons that pull "
           "request from the roadmap.")


def test_no_arm_instruction_is_recognised_in_every_dispatch_phrasing():
    for text in (_NO_ARM, "DO NOT open a new pull request. DO NOT arm auto-merge.",
                 "5. Do NOT arm auto-merge: the synthesizer loop is the single writer",
                 "do not arm auto merge"):
        assert has_no_arm_instruction(text), text
    for text in ("", None, "Arm auto-merge on the PR (`gh pr merge --auto`).",
                 "do not leave it unarmed"):
        assert not has_no_arm_instruction(text), text


def test_open_ready_pr_meets_a_dispatch_that_forbids_arming():
    v = met_bar("feature map diff", "explicit", _facts([_pr(state="open")]),
                instructions=_NO_ARM)
    assert v.met and v.confidence == "high" and v.remedy is None
    assert "arm" not in (v.remedy or "")


def test_draft_pr_under_a_no_arm_dispatch_is_told_to_ready_it_not_to_arm_it():
    v = met_bar("feature map diff", "explicit",
                _facts([_pr(state="draft", is_draft=True)]), instructions=_NO_ARM)
    assert not v.met and v.confidence == "high"
    assert "gh pr ready 1" in v.remedy and "--auto" not in v.remedy


# A dispatch whose human gate is the DRAFT state itself (#2925): in a repo whose
# own automation arms every open non-draft PR, "ready + unarmed" cannot hold, so
# the onboarding prompts open a draft and tell the session to leave it one.
_KEEP_DRAFT = ("4. Do NOT arm auto-merge and do NOT mark the PR ready for review: "
               "leave it a draft.")


def test_keep_draft_instruction_is_recognised():
    for text in (_KEEP_DRAFT, "do not mark it ready for review",
                 "Do NOT mark the pull request ready"):
        assert has_keep_draft_instruction(text), text
    for text in ("", None, _NO_ARM, "mark it ready for review: `gh pr ready 1`"):
        assert not has_keep_draft_instruction(text), text


def test_draft_pr_meets_a_dispatch_that_keeps_it_a_draft():
    v = met_bar("feature map", "explicit",
                _facts([_pr(state="draft", is_draft=True)]), instructions=_KEEP_DRAFT)
    assert v.met and v.confidence == "high" and v.remedy is None


def test_red_draft_under_a_keep_draft_dispatch_is_told_to_fix_ci_not_ready_it():
    v = met_bar("feature map", "explicit",
                _facts([_pr(state="draft", is_draft=True, checks="failing")]),
                instructions=_KEEP_DRAFT)
    assert not v.met and "gh pr ready" not in v.remedy and "--auto" not in v.remedy


def test_failing_ci_under_a_no_arm_dispatch_is_not_met():
    v = met_bar("feature map diff", "explicit",
                _facts([_pr(state="open", checks="failing")]), instructions=_NO_ARM)
    assert not v.met and v.confidence == "high" and "--auto" not in (v.remedy or "")


def test_open_unarmed_pr_without_a_no_arm_dispatch_still_nudges_to_arm():
    v = met_bar("fix ci", "explicit", _facts([_pr(state="open")]),
                instructions="Arm auto-merge on the PR (`gh pr merge --auto`).")
    assert not v.met and "--squash --auto" in v.remedy


# ---------------------------------------------------------------------------
# #2909: a mark_no_pr the dispatch itself asked for ("DO NOT open a new pull
# request") is the dispatch's answer, not an unverified claim to nudge against.
# ---------------------------------------------------------------------------

def test_forbids_opening_pr_phrasings():
    assert forbids_opening_pr("DO NOT open a new pull request. DO NOT arm auto-merge.")
    assert forbids_opening_pr("do NOT open another pull request.")
    assert forbids_opening_pr("do not commit, and do not open a pull request")
    assert forbids_opening_pr("Do not create a PR for this.")
    assert not forbids_opening_pr("Open a PR and arm auto-merge.")
    assert not forbids_opening_pr("")


def test_mark_no_pr_under_a_forbidding_prompt_is_met():
    v = met_bar(_CODE_GOAL, "user", _facts([], has_repo=True), "not-needed",
                pr_resolution_source="tool",
                instructions="Push to the existing branch. DO NOT open a new pull request.")
    assert v.met and v.confidence == "high"


def test_forbidding_prompt_alone_does_not_satisfy_the_bar():
    """Without the session's own mark_no_pr the prompt is not a no-PR dispatch."""
    v = met_bar(_CODE_GOAL, "user", _facts([], has_repo=True), None,
                instructions="Push to the existing branch. DO NOT open a new pull request.")
    assert not v.met


def test_forbidding_prompt_never_masks_an_unarmed_pr():
    v = met_bar(_CODE_GOAL, "user", _facts([_pr(state="open")]), "not-needed",
                pr_resolution_source="tool",
                instructions="DO NOT open a new pull request.")
    assert not v.met and v.confidence == "high"


# ---------------------------------------------------------------------------
# #2915 / #2917 / #2918: a reasoned or user-confirmed mark_no_pr satisfies the
# bar; the byte-identical "open a PR" demand must never answer it again.
# ---------------------------------------------------------------------------

def test_reasoned_tool_no_pr_is_met_at_low_confidence():
    v = met_bar(_CODE_GOAL, "user", _facts([], has_repo=True), "not-needed",
                pr_resolution_source="tool",
                pr_resolution_note="PR #15621 already fixes #15619 and is armed.")
    assert v.met and v.remedy is None and v.confidence == "low"
    assert "15621" in v.reason


def test_user_confirmed_no_pr_is_met_at_high_confidence():
    """A bare marker claim (no reason) made after the user answered the nudge."""
    v = met_bar(_CODE_GOAL, "user", _facts([], has_repo=True), "not-needed",
                pr_resolution_source="marker", claim_confirmed=True)
    assert v.met and v.confidence == "high"


def test_a_blank_note_is_not_a_reason():
    v = met_bar(_CODE_GOAL, "user", _facts([], has_repo=True), "not-needed",
                pr_resolution_source="tool", pr_resolution_note="   ")
    assert not v.met


def test_a_reason_never_masks_an_open_unarmed_pr():
    v = met_bar(_CODE_GOAL, "user", _facts([_pr(state="open")]), "not-needed",
                pr_resolution_source="tool", pr_resolution_note="nothing to do",
                claim_confirmed=True)
    assert not v.met and v.confidence == "high"
