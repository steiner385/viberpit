"""Pure helpers for the PR auto-fix feature: the policy-mode vocabulary and the
branch-aware fix prompt. No I/O — kept pure so it's trivially testable and so the
orchestrator method that wires it stays small."""
from __future__ import annotations

from server.github.pr_status import PullRequestInfo

# The single source of truth for the pr_autofix_mode vocabulary. The settings
# API validates against the same set (kept in sync there for the HTTP layer).
VALID_AUTOFIX_MODES: frozenset[str] = frozenset({"off", "propose", "auto"})


def build_pr_fix_prompt(repo: str, pr: PullRequestInfo) -> str:
    """A system-originated prompt instructing the owning session to restore a
    failing PR.

    Branch-aware on purpose: ccswitch's completion workflow moves the worktree
    onto a fresh feat/* branch after opening a PR, so by the time an older PR's
    CI fails the worktree HEAD is usually NOT on the PR's branch. The prompt
    therefore tells the agent to check the PR branch out FIRST rather than
    assuming the current diff is the PR.
    """
    return (
        f"Your pull request {repo}#{pr.number} (\"{pr.title}\") has failing CI checks. "
        f"Investigate and restore it.\n\n"
        f"Steps:\n"
        f"1. Check out the PR branch first — your worktree HEAD may have moved on: "
        f"`gh pr checkout {pr.number}` (then `git pull` to get the latest).\n"
        f"2. Inspect what's red: `gh pr checks {pr.number}` and reproduce the failing "
        f"job locally (run the relevant tests/lint/build).\n"
        f"3. Fix the root cause, commit, and push so CI re-runs.\n"
        f"4. Briefly report what was broken and what you changed.\n\n"
        f"PR: {pr.url}"
    )
