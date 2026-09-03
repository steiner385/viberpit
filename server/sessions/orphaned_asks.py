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

# Pre-restart statuses that mean a human was being asked something. Deliberately
# the exact complement of RESUME_ELIGIBLE_STATUSES (server/sessions/types.py):
# #938 resumes dropped WORK and explicitly excludes these three; this module owns
# them. If one set grows, the other must be reconsidered.
ORPHANABLE_ASK_STATUSES: frozenset[str] = frozenset({
    "waiting_question", "waiting_input", "ready_review",
})

_KIND_BY_STATUS: dict[str, str] = {
    "waiting_question": "question",
    "waiting_input": "permission",
    "ready_review": "plan",
}


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


def orphaned_answer_prompt(selected: list[str]) -> str:
    """The prompt that delivers a user's answer to an orphaned question.

    Two jobs. First, carry the selection. Second — and this is load-bearing —
    disown the tool_result the CLI synthesises for the dangling ``tool_use`` when
    it resumes the transcript: "The user doesn't want to proceed with this tool
    use. The tool use was rejected... STOP what you are doing." The assistant
    reads that immediately before this prompt, so without an explicit correction
    it stops instead of acting on the answer.
    """
    answer = "; ".join(s for s in selected if s) or "(no option selected)"
    return (
        "[Answering the question that a daemon restart interrupted]\n\n"
        f"My answer: {answer}\n\n"
        "Note: that question's tool call was closed out automatically when the "
        "daemon restarted, and the transcript may show it as rejected or "
        "interrupted. I did not reject it — the restart did. Please treat the "
        "answer above as my response and continue."
    )
