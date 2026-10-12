import pytest

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


@pytest.mark.parametrize("builder", [build_pr_fix_prompt, build_conflict_fix_prompt])
def test_checkout_survives_the_head_branch_being_held_by_another_worktree(builder):
    """#2995: a bare ``gh pr checkout N`` names the local branch after the PR head,
    and git refuses it when the original session's worktree still holds that branch.
    The prompt checks out under its own local name and pushes to the head by an
    explicit refspec, so the fix lands on the existing PR, not a new branch."""
    text = builder("o/r", _pr(number=2938, head_ref="fix/assistant-drawer"))
    assert "gh pr checkout 2938 --branch pr-2938" in text
    assert "git push origin HEAD:fix/assistant-drawer" in text
    assert "never open a new PR" in text


def test_checkout_step_names_where_to_read_an_unknown_head_branch():
    """Cached info without ``head_ref`` still yields a usable push target."""
    text = build_pr_fix_prompt("o/r", _pr(number=5))
    assert "HEAD:<head branch from `gh pr view 5 --json headRefName`>" in text


def test_conflict_prompt_distinct_from_ci_prompt():
    pr = _pr(number=7)
    assert build_conflict_fix_prompt("o/r", pr) != build_pr_fix_prompt("o/r", pr)
