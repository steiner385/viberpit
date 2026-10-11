"""Interactive requests whose waiter died with the daemon.

A pending AskUserQuestion / permission / plan approval is NOT data: it is a live
rendezvous between the daemon's ``asyncio.Future`` and the SDK subprocess blocked
in its ``can_use_tool`` callback. A restart destroys both, so such a request can
never be RESOLVED afterwards — only RE-ASKED, with the user's choice delivered as
the next turn's prompt.

This module is the single definition of that condition. Kept dependency-free and
side-effect-free so it is exhaustively table-testable, mirroring ``status.py``.
"""
from __future__ import annotations

from viberpit import brand

#: ``AgentDriver.aclose(reason=...)`` for a daemon shutdown/drain (orchestrator
#: ``stop()``). A driver closed for this reason settles its pending asks with
#: :data:`RESTART_ASK_MESSAGE` instead of a user refusal (#2922).
CLOSE_REASON_RESTART = "restart"

#: ``QuestionResolved.mode`` for a question a restart cut off. Not "dismissed": the
#: user did nothing, and the card is re-offered after the restart.
QUESTION_MODE_INTERRUPTED = "interrupted"

#: What the agent reads as the result of an ask a restart cut off. Load-bearing
#: wording: the old "user did not answer" / "denied by user" made the model act on a
#: refusal nobody gave.
RESTART_ASK_MESSAGE = (
    f"Interrupted by a {brand.NAME} daemon restart: the user did NOT answer or reject "
    "this. It will be re-presented to them; wait for their answer and do not assume a "
    "refusal."
)

#: ``permId`` prefix of a permission / plan card rebuilt from the transcript after a
#: restart orphaned it (``replay.last_orphaned_permission``). The suffix is the gated
#: tool_use id, so a re-offer is stable across reattaches. No live registry ever
#: mints one, so a decision on it can only take the orphaned-answer fallback.
ORPHAN_PERM_PREFIX = "orphan:"

# Pre-restart statuses that mean a human was being asked something, mapped to the
# kind of ask that was lost.
_KIND_BY_STATUS: dict[str, str] = {
    "waiting_question": "question",
    "waiting_input": "permission",
    "ready_review": "plan",
}

# Derived from the mapping above, never re-listed: two hand-maintained copies of
# one set is exactly the drift shape this repo bans.
#
# These are the CONTRAST PAIR that server/sessions/types.py:22-25 draws against
# RESUME_ELIGIBLE_STATUSES — not its complement (most statuses, e.g. "idle" or
# "done", are in neither set). #938 resumes dropped WORK and explicitly excludes
# these three awaiting-human states; this module owns them. If either set grows,
# the other must be reconsidered.
ORPHANABLE_ASK_STATUSES: frozenset[str] = frozenset(_KIND_BY_STATUS)


def orphaned_ask_kind(interrupted_by_restart: bool,
                      interrupted_from_status: str | None) -> str | None:
    """The kind of human-facing ask a restart orphaned, or None.

    Both inputs come from the SessionRecord flags ``rehydrate()`` sets and
    ``_persist_status`` clears together on any non-idle status — so this
    self-clears the moment the session does anything again, with no new column.
    """
    if not interrupted_by_restart:
        return None
    return _KIND_BY_STATUS.get(interrupted_from_status or "")


_DISOWN_NOTE = (
    "Note: that request's tool call was closed out automatically when the daemon "
    "restarted, and the transcript may show it as rejected or interrupted. I did not "
    "reject it — the restart did. Please treat the decision above as my response and "
    "continue."
)

_CONTEXT_HEADER = "While that was pending, the restart also affected your background work:"


def _question_body(answer: object) -> str:
    entries = [str(s) for s in (answer if isinstance(answer, list) else [answer]) if s]
    if not entries:
        body = "My answer: (no option selected)"
    elif len(entries) == 1:
        body = f"My answer: {entries[0]}"
    else:
        body = "My answers:\n" + "\n".join(f"- {e}" for e in entries)
    return "[Answering the question that a daemon restart interrupted]\n\n" + body


def _allowed(decision: object) -> bool:
    # "always" can't persist a grant for a request whose waiter is gone; it is an
    # allow of this one call, exactly as the live registry treats a non-grantable one.
    return str(decision) in ("allow", "always")


def _permission_body(decision: object, tool: str | None) -> str:
    name = tool or "tool"
    if _allowed(decision):
        return (f"[Re: the {name} permission request a daemon restart interrupted: the "
                "user ALLOWED it. Re-run the tool call.]")
    return (f"[Re: the {name} permission request a daemon restart interrupted: the "
            "user DENIED it. Do not run it; continue without it.]")


def _plan_body(decision: object) -> str:
    if _allowed(decision):
        return ("[Re: the plan approval a daemon restart interrupted: the user APPROVED "
                "the plan. Re-run ExitPlanMode to leave plan mode, then implement it.]")
    return ("[Re: the plan approval a daemon restart interrupted: the user REJECTED the "
            "plan. Stay in plan mode and keep planning; do not start implementing.]")


def orphaned_answer_prompt(kind: str, answer: list[str] | str, *,
                           tool: str | None = None, context: str | None = None) -> str:
    """The prompt that delivers a user's response to an ask a restart orphaned.

    ``kind`` is :func:`orphaned_ask_kind`'s answer. ``answer`` is the selection for a
    ``question`` and the card decision (``allow`` / ``always`` / ``deny``) for a
    ``permission`` or ``plan``; ``tool`` names the permission's tool.

    Two jobs. First, carry the response. Second — and this is load-bearing —
    disown the tool_result the CLI synthesises for the dangling ``tool_use`` when
    it resumes the transcript: "The user doesn't want to proceed with this tool
    use. The tool use was rejected... STOP what you are doing." The assistant
    reads that immediately before this prompt, so without an explicit correction
    it stops instead of acting on the answer.

    ``context`` is the restart's deferred background-work report
    (``background_tasks.build_rearm_prompt``): while an ask is orphaned the rearm
    turn is held back so it cannot consume the card (#2922), and is folded in here
    so the user's answer is the one turn that carries both.

    A question's ``answer`` comes straight off client JSON (server/bridge/ws.py
    takes it from the frame unvalidated), so every entry is coerced with ``str()``
    — a non-string entry used to raise TypeError out of the WS message loop and
    kill the socket. Multiple entries render one per line: a group of orphaned
    questions is delivered as ONE prompt carrying "<header>: <label>" per
    question, and a "; " join makes that mapping unreadable.
    """
    if kind == "question":
        body = _question_body(answer)
    elif kind == "permission":
        body = _permission_body(answer, tool)
    elif kind == "plan":
        body = _plan_body(answer)
    else:
        raise ValueError(f"unknown orphaned ask kind {kind!r}")
    parts = [body, _DISOWN_NOTE]
    if context and context.strip():
        parts.append(f"{_CONTEXT_HEADER}\n{context.strip()}")
    return "\n\n".join(parts)
