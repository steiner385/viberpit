from server.automations.completion_bar import PrFact, PrFacts, met_bar


def _facts(prs=(), has_repo=True):
    return PrFacts(has_repo=has_repo, prs=tuple(prs))


def _pr(number=1, state="open", is_draft=False, auto_merge=False, in_merge_queue=False,
        checks="none"):
    return PrFact(number=number, state=state, is_draft=is_draft,
                  auto_merge_enabled=auto_merge, in_merge_queue=in_merge_queue,
                  url=f"http://x/{number}", checks=checks)


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
