from server.github.pr_status import PullRequestInfo
from server.sessions.pr_fix import build_pr_fix_prompt, VALID_AUTOFIX_MODES


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
