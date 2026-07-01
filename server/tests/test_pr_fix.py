from server.github.pr_status import PullRequestInfo
from server.sessions.pr_fix import (
    build_conflict_fix_prompt,
    build_pr_fix_prompt,
    VALID_AUTOFIX_MODES,
)


def _pr(**kw):
    base = dict(number=42, url="https://github.com/o/r/pull/42",
               state="open", checks="failing", title="Add widget")
    base.update(kw)
    return PullRequestInfo(**base)


def test_modes_constant():
    assert VALID_AUTOFIX_MODES == frozenset({"off", "propose", "auto"})


def test_prompt_names_repo_pr_and_branch_checkout():
    text = build_pr_fix_prompt("owner/repo", _pr())
    # Identifies the target PR and repo.
    assert "#42" in text
    assert "owner/repo" in text
    # Branch-aware: must instruct checking out the PR branch first, since the
    # worktree HEAD may have moved on to a newer feature branch.
    assert "gh pr checkout 42" in text
    # Directs the agent to inspect the failing checks and push a fix.
    assert "gh pr checks 42" in text
    assert "push" in text.lower()


def test_prompt_includes_title_for_context():
    text = build_pr_fix_prompt("owner/repo", _pr(title="Fix login race"))
    assert "Fix login race" in text


def test_conflict_prompt_names_repo_pr_and_checkout_resolve_push():
    text = build_conflict_fix_prompt("owner/repo", _pr(number=99))
    # Identifies the target PR and repo.
    assert "#99" in text
    assert "owner/repo" in text
    # Branch-aware: must instruct checking out the PR branch first.
    assert "gh pr checkout 99" in text
    # Must mention resolving the conflict and rebasing/merging the base branch.
    low = text.lower()
    assert "conflict" in low
    assert "rebase" in low or "merge" in low
    assert "resolve" in low
    # Directs the agent to run tests and push the resolution.
    assert "push" in low


def test_conflict_prompt_distinct_from_ci_prompt():
    pr = _pr(number=7)
    assert build_conflict_fix_prompt("o/r", pr) != build_pr_fix_prompt("o/r", pr)
