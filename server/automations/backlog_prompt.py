"""Pure prompt builder for the Backlog Runner implementation sessions.

``build_implement_prompt`` constructs the seed prompt that is passed to a
spawned session instructing it to implement a batch of issues in one PR,
following the repository's completion workflow.  It is PURE — no I/O, no
model calls, no side effects — so it can be unit-tested without any
infrastructure.

Engine-neutral by construction (Phase 7): every instruction is a git/gh fact
that holds for any native agent.  The one engine-specific detail, the commit
attribution trailer, comes from ``server.sessions.completion.commit_trailer`` —
a Codex session must not be told to co-author its commits as Claude.
"""
from __future__ import annotations

from ccswitch.engines import AgentEngine

from server.sessions.completion import commit_trailer

from .backlog_selection import Batch


def build_implement_prompt(batch: Batch, default_branch: str = "master", *,
                           engine: AgentEngine | str = AgentEngine.CLAUDE_CODE) -> str:
    """Return the seed prompt for a session that implements ``batch``.

    The prompt instructs the agent to:
    - Implement ALL issues listed in the batch inside a single PR.
    - Close each issue with a ``Fixes #<n>`` keyword in the PR body.
    - Follow this repository's completion workflow: open the PR (non-draft),
      mark it ready for review, and arm auto-merge (``--squash --auto``).

    ``default_branch`` is the target repo's actual integration branch (e.g.
    ``"main"`` or ``"master"``).  Callers must resolve it from the remote before
    invoking — do NOT hardcode ``"master"`` (many repos use ``"main"``).

    Pure: no I/O, no model calls.  The returned string may be passed directly
    as the first user prompt of a spawned session on any engine.
    """
    # Build a numbered list of issue refs and their human-readable numbers.
    issue_lines: list[str] = []
    for ref in batch.issues:
        # ref format: "owner/repo#123"
        _, _, number = ref.partition("#")
        issue_lines.append(f"- {ref}  (close with `Fixes #{number}` in the PR body)")

    issues_block = "\n".join(issue_lines)

    return (
        f"Implement the following GitHub issue(s) in repository `{batch.repo}` "
        f"and land them in a single pull request:\n\n"
        f"{issues_block}\n\n"
        "Requirements — follow the repository's completion workflow exactly:\n"
        "1. Implement all issues listed above.\n"
        "2. Open ONE pull request that covers all of them (never a draft).\n"
        "3. In the PR body include a closing keyword for EVERY issue:\n"
        "   `Fixes #<n>` (one per issue) so GitHub auto-closes them on merge.\n"
        "4. Mark the PR ready for review immediately after opening.\n"
        "5. Arm auto-merge: `gh pr merge <#> --squash --auto`.\n"
        "   Both steps 4 and 5 are required — a ready PR with auto-merge armed\n"
        "   is the finished state.\n"
        f"6. Branch off `origin/{default_branch}` (never local `{default_branch}`).\n"
        "7. End every commit message with:\n"
        f"   `{commit_trailer(engine)}`\n"
    )
