"""Pure helpers for the PR auto-fix feature: the policy-mode vocabulary and the
branch-aware fix prompt. No I/O — kept pure so it's trivially testable and so the
orchestrator method that wires it stays small.

Engine-neutral by construction (Phase 7, #1267): every instruction is a git/gh
fact that holds for any native agent. The one engine-specific detail, the commit
attribution trailer, comes from ``server.sessions.completion.commit_trailer`` —
a Codex session must not be told to co-author its commits as Claude.
"""
from __future__ import annotations

from ccswitch.engines import AgentEngine

from server.github.pr_status import PullRequestInfo
from server.sessions.completion import commit_trailer

# The single source of truth for the pr-doctor mode vocabulary. The orchestrator
# validates the pr-doctor automation registry row's `mode` against this set.
VALID_AUTOFIX_MODES: frozenset[str] = frozenset({"off", "propose", "auto"})


def build_pr_fix_prompt(
    repo: str, pr: PullRequestInfo, *,
    engine: AgentEngine | str = AgentEngine.CLAUDE_CODE,
) -> str:
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
        f"3. Fix the root cause, commit, and push so CI re-runs. End the commit "
        f"message with:\n"
        f"   `{commit_trailer(engine)}`\n"
        f"4. Briefly report what was broken and what you changed.\n\n"
        f"PR: {pr.url}"
    )


def build_conflict_fix_prompt(
    repo: str, pr: PullRequestInfo, *,
    engine: AgentEngine | str = AgentEngine.CLAUDE_CODE,
) -> str:
    """A system-originated prompt instructing the owning session to resolve a
    merge conflict on a PR.

    Branch-aware, mirroring ``build_pr_fix_prompt``: ccswitch's completion
    workflow moves the worktree onto a fresh feat/* branch after opening a PR, so
    the worktree HEAD is usually NOT on the PR's branch by the time a conflict is
    detected. The prompt therefore tells the agent to check the PR branch out
    FIRST, then rebase onto (or merge) the base branch, resolve the conflicts,
    run the tests, commit, and push. Pure (no I/O), returns a string.
    """
    return (
        f"Your pull request {repo}#{pr.number} (\"{pr.title}\") has a MERGE CONFLICT "
        f"with its base branch and cannot be merged. Resolve it.\n\n"
        f"Steps:\n"
        f"1. Check out the PR branch first — your worktree HEAD may have moved on: "
        f"`gh pr checkout {pr.number}`.\n"
        f"2. Bring in the base branch and rebase onto it (prefer rebase; fall back to "
        f"merge if a rebase is unwieldy): `git fetch origin` then "
        f"`git rebase origin/<base>` (or `git merge origin/<base>`).\n"
        f"3. Resolve every conflict by hand — reconcile both sides, don't blindly "
        f"discard either. Remove all conflict markers.\n"
        f"4. Run the tests/lint/build to confirm the resolution is correct.\n"
        f"5. Commit the resolution and push (a rebase needs "
        f"`git push --force-with-lease`; a merge is a normal `git push`) so the PR "
        f"becomes mergeable and CI re-runs. End the commit message with:\n"
        f"   `{commit_trailer(engine)}`\n"
        f"6. Briefly report which files conflicted and how you reconciled them.\n\n"
        f"PR: {pr.url}"
    )


def build_consolidation_prompt(
    repo: str, base: str, prs: list[PullRequestInfo], *,
    engine: AgentEngine | str = AgentEngine.CLAUDE_CODE,
) -> str:
    """A system-originated prompt instructing a fresh session to consolidate
    several open PRs into ONE new PR (saving CI). Pure (no I/O).

    Mirrors build_pr_fix_prompt/build_conflict_fix_prompt: explicit, branch-aware,
    and self-contained because no human will attach to steer the session."""
    nums = ", ".join(f"#{p.number}" for p in prs)
    listing = "\n".join(f"  - #{p.number} \"{p.title}\" ({p.url})" for p in prs)
    closes = " ".join(f"Closes #{p.number}" for p in prs)
    branch = f"consolidate/{'-'.join(str(p.number) for p in prs)}"
    return (
        f"Consolidate {len(prs)} open pull requests in {repo} into ONE new PR to "
        f"save CI. The PRs to combine ({nums}):\n{listing}\n\n"
        f"Steps:\n"
        f"1. `git fetch origin`, then create a fresh branch off the base: "
        f"`git checkout -b {branch} origin/{base}`.\n"
        f"2. Bring each PR's changes onto this branch — `gh pr checkout <n>` to "
        f"inspect, then cherry-pick or merge its commits. Resolve conflicts by "
        f"reconciling BOTH sides (never blindly discard). Run the tests/lint/build "
        f"after each so you know which PR introduced any breakage. End every commit "
        f"message with:\n"
        f"   `{commit_trailer(engine)}`\n"
        f"3. Push the branch and open ONE consolidated PR whose body summarizes the "
        f"combined change and lists every original with a closing keyword: {closes}\n"
        f"4. Close each original PR with a linking comment — for each number run "
        f"`gh pr close <n> --comment \"Superseded by the consolidated PR.\"`.\n"
        f"5. Follow the project completion workflow: the branch is already off "
        f"origin/{base}; open the PR non-draft and arm auto-merge "
        f"(`gh pr merge --squash --auto`).\n"
        f"6. Report the consolidated PR number and which originals you closed."
    )
