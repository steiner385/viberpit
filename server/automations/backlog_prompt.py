"""Pure prompt builders for the Backlog Runner's spawned sessions.

``build_implement_prompt`` constructs the seed prompt that is passed to a
spawned session instructing it to implement a batch of issues in one PR,
following the repository's completion workflow. ``build_investigate_prompt``
constructs the seed prompt for a batch whose primary issue is an evidence-held
auto-filed error report: investigate, do not implement (spec
2026-09-18-hold-ladder-design.md §5). Both are PURE — no I/O, no model calls,
no side effects — so they can be unit-tested without any infrastructure.

Engine-neutral by construction (Phase 7): every instruction is a git/gh fact
that holds for any native agent. The one engine-specific detail in the implement
prompt, the commit attribution trailer, comes from
``server.sessions.completion.commit_trailer`` — a Codex session must not be told
to co-author its commits as Claude. The investigate prompt has its own
engine-specific detail: finishing without a PR goes through the ``mark_no_pr``
signalling tool on Claude (``AgentCapabilities.durable_tool_surface``), but
Codex has no signalling tools at all — it keeps the ``NO_PR_MARKER`` HTML
comment (``server.sessions.completion``), same as every other Codex completion
signal.
"""
from __future__ import annotations

from collections.abc import Callable

from viberpit.engines import AgentEngine

from server.sessions.completion import NO_PR_MARKER, commit_trailer

from .backlog_selection import Batch

CONVERTED_NOTE_PREFIX = "converted to a fix"
"""Shared literal for the investigate prompt's 'convert to a fix' exit note
and the Backlog Runner's hold-clearing match on it (Task 4) — one spelling,
never re-typed at either site."""


def _repo_clause(repo: str) -> str:
    """The backtick-quoted ``in repository `owner/repo`` `` clause shared by every
    seed prompt's opening line."""
    return f"in repository `{repo}`"


def _issue_ref_lines(issues: list[str], line: Callable[[str, str], str]) -> str:
    """Render one bullet per issue ref via ``line(ref, number)``.

    ``ref`` is the full ``"owner/repo#123"`` key; ``number`` is the bare issue
    number parsed out of it. Shared by both prompt builders so the ref-list
    formatting cannot drift between them.
    """
    rendered: list[str] = []
    for ref in issues:
        _, _, number = ref.partition("#")
        rendered.append(line(ref, number))
    return "\n".join(rendered)


DECISIONS_HEADING = "## Decisions already taken"
"""The implement prompt's accepted-decision block header (spec 2026-09-18 §6).
One literal, shared with the tests that assert the block is (and is not) there."""


def _decisions_block(batch: Batch) -> str:
    """The "## Decisions already taken" section for *batch*, or ``""`` when the
    batch has none (spec 2026-09-18 §6).

    Each line quotes the accepted decision VERBATIM as ``server/pm/decisions.py``
    rendered it — this module never re-words or re-formats a decision, because the
    whole point of §6 is that what the person picked in the inbox is what the next
    run implements. The ref is prefixed so a multi-issue batch says which issue
    each decision belongs to.

    Refs are listed in ``batch.issues`` order (the order the prompt already lists
    them in); a decision keyed by a ref that is not in the batch still renders,
    sorted, rather than being silently dropped.

    An empty map answers the empty string, and the caller concatenates it as-is —
    so a batch with nothing decided produces a byte-identical prompt to the one
    this builder produced before decisions existed.
    """
    decisions = batch.decisions or {}
    if not decisions:
        return ""
    in_batch = set(batch.issues)
    ordered = [r for r in batch.issues if r in decisions]
    ordered += sorted(r for r in decisions if r not in in_batch)
    lines = "\n".join(f"- {ref}: {decisions[ref]}" for ref in ordered)
    return (
        f"{DECISIONS_HEADING}\n\n"
        "The owner has already decided the following. Implement them as written; "
        "do not re-open or re-litigate them.\n\n"
        f"{lines}\n\n"
    )


def _is_codex(engine: AgentEngine | str) -> bool:
    """Whether *engine* is Codex — the one engine with no signalling-tool surface
    (``AgentCapabilities.durable_tool_surface`` is False), so it finishes through
    the HTML-comment markers in ``server.sessions.completion`` instead of a tool
    call."""
    return str(getattr(engine, "value", engine)) == AgentEngine.CODEX.value


def build_implement_prompt(batch: Batch, default_branch: str = "master", *,
                           engine: AgentEngine | str = AgentEngine.CLAUDE_CODE) -> str:
    """Return the seed prompt for a session that implements ``batch``.

    The prompt instructs the agent to:
    - Implement ALL issues listed in the batch inside a single PR.
    - Close each issue with a ``Fixes #<n>`` keyword in the PR body.
    - Follow this repository's completion workflow: open the PR (non-draft),
      mark it ready for review, and arm auto-merge (``--squash --auto``).

    When ``batch.decisions`` is non-empty the prompt also carries a
    "## Decisions already taken" block quoting each accepted decision verbatim
    (spec 2026-09-18 §6) — the decision the person took in the inbox is what this
    run implements. An empty map adds nothing at all.

    ``default_branch`` is the target repo's actual integration branch (e.g.
    ``"main"`` or ``"master"``).  Callers must resolve it from the remote before
    invoking — do NOT hardcode ``"master"`` (many repos use ``"main"``).

    Pure: no I/O, no model calls.  The returned string may be passed directly
    as the first user prompt of a spawned session on any engine.
    """
    issues_block = _issue_ref_lines(
        batch.issues,
        lambda ref, number: f"- {ref}  (close with `Fixes #{number}` in the PR body)",
    )

    return (
        f"Implement the following GitHub issue(s) {_repo_clause(batch.repo)} "
        f"and land them in a single pull request:\n\n"
        f"{issues_block}\n\n"
        f"{_decisions_block(batch)}"
        "Requirements — follow the repository's completion workflow exactly:\n"
        "1. Implement all issues listed above.\n"
        "2. Open ONE pull request that covers all of them (never a draft).\n"
        "3. In the PR body include a closing keyword for EVERY issue:\n"
        "   `Fixes #<n>` (one per issue) so GitHub auto-closes them on merge.\n"
        "4. If an issue's body has a `Covers: FR-…, AC-…` line naming stable\n"
        "   requirement/acceptance-criteria IDs, cite those same IDs in the PR\n"
        "   body (e.g. `Covers: FR-003, AC-012`) so requirement coverage traces\n"
        "   from the PR back to the spec.\n"
        "5. Mark the PR ready for review immediately after opening.\n"
        "6. Arm auto-merge: `gh pr merge <#> --squash --auto`.\n"
        "   Both steps 5 and 6 are required — a ready PR with auto-merge armed\n"
        "   is the finished state.\n"
        f"7. Branch off `origin/{default_branch}` (never local `{default_branch}`).\n"
        "8. End every commit message with:\n"
        f"   `{commit_trailer(engine)}`\n"
    )


def _finish_not_needed(note: str, *, codex: bool) -> str:
    """The 'finish, no PR expected' step for a non-cancel exit (close-as-noise,
    consolidate, convert-to-fix) — a tool call on Claude, the marker on Codex
    (which has no signalling tools at all; see module docstring)."""
    if codex:
        return (f'note "{note}" in your final message, then end it with '
                f'`{NO_PR_MARKER}` on its own line.')
    return f'call `mark_no_pr("not-needed", note="{note}")`.'


def _finish_cancelled(note: str, *, codex: bool) -> str:
    """The 'finish, no PR expected' step for the escalate exit. Same marker as
    ``_finish_not_needed`` on Codex, but phrased so ``resolution_from_text``
    (``server.sessions.completion``) reads it as 'cancelled' rather than
    'not-needed' — it matches on 'not opening'."""
    if codex:
        return (f"state that you are not opening a PR because {note}, then end "
                f"your final message with `{NO_PR_MARKER}` on its own line.")
    return f'call `mark_no_pr("cancelled", note="{note}")`.'


def build_investigate_prompt(batch: Batch, *,
                             engine: AgentEngine | str = AgentEngine.CLAUDE_CODE) -> str:
    """Return the seed prompt for a session that investigates ``batch``.

    Spawned instead of ``build_implement_prompt`` when the batch's primary issue
    carries the ``evidence`` hold — an auto-filed error report (see
    ``server/pm/holds.py:set_evidence_hold``). The session investigates; it does
    NOT implement, and it opens no PR and pushes nothing. It must end in exactly
    one of four exits:

    - Close as noise (not a real issue).
    - Consolidate (duplicate of another open report).
    - Convert to a fix (root cause found; the runner clears the hold and
      re-queues the issue as an implement candidate).
    - Escalate (only the owner can supply what is needed).

    A session that ends without one of these exits is handed to the owner by
    the runner (the no-exit rule; spec 2026-09-18-hold-ladder-design.md §5).

    Pure: no I/O, no model calls. The returned string may be passed directly as
    the first user prompt of a spawned session on any engine.
    """
    codex = _is_codex(engine)
    _, _, primary_number = batch.primary.partition("#")

    issues_block = _issue_ref_lines(batch.issues, lambda ref, number: f"- {ref}")

    close_finish = _finish_not_needed("closed as noise: <why>", codex=codex)
    consolidate_finish = _finish_not_needed(
        f"consolidated into #{primary_number}", codex=codex)
    convert_finish = _finish_not_needed(
        f"{CONVERTED_NOTE_PREFIX}: <one line>", codex=codex)
    escalate_finish = _finish_cancelled(
        "needs owner: <the one specific thing>", codex=codex)

    return (
        f"Investigate the following auto-filed error report(s) {_repo_clause(batch.repo)}:\n\n"
        f"{issues_block}\n\n"
        f"Primary: {batch.primary} (#{primary_number})\n\n"
        "These are auto-filed error reports. Investigate; do not implement. "
        "Read-only on code: open no PR, push nothing.\n\n"
        "End in exactly ONE of these four exits:\n\n"
        "1. **Close as noise** — the report is not a real issue. Comment the "
        "reason on the issue (`gh issue comment`), close it (`gh issue close`), "
        f"then {close_finish}\n\n"
        "2. **Consolidate** — it duplicates another open report. Pick one "
        'primary; for each sibling, comment "duplicate of #<primary>" and '
        f"close it (`gh issue comment` / `gh issue close`); leave the primary "
        f"open; then {consolidate_finish}\n\n"
        "3. **Convert to a fix** — you have found the root cause and it is "
        "ready to implement. Edit the primary issue's body "
        '(`gh issue edit --body-file`) to add a "## Findings" section '
        "(root cause, reproduction, the code path); then "
        f"{convert_finish} The runner clears the hold and re-queues the issue "
        "as an implement candidate.\n\n"
        "4. **Escalate** — only the owner can supply what is needed (access, a "
        "decision, a reproduction only they have). Write what is needed into "
        'the body\'s "## Findings" section, then '
        f"{escalate_finish} The runner hands the issue to the owner with that "
        "note as the reason.\n\n"
        "If you end without one of these exits, the runner hands the issue to "
        "the owner.\n"
    )
