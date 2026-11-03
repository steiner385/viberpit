"""Fork from here (D-8): the request vocabulary shared by the orchestrator, the daemon
RPC, ``RemoteOrchestrator`` and the API route.

A fork refusal raises :class:`ForkError` with one of the codes below; a failure
making the child's worktree raises ``viberpit.worktrees.ForkWorktreeError`` with one
of ``FORK_WORKTREE_CODES``. Both carry ``error_data`` so the code survives the RPC
envelope and the daemon logs the refusal at INFO.
"""
from __future__ import annotations

import uuid

#: Fork from an assistant reply: ``atTurnId`` is that reply's turn.
MODE_REPLY = "reply"
#: Fork from the user's message: ``atTurnId`` is the PREVIOUS reply's turn (or None
#: for the first message) and ``prefill`` is the message text for the child composer.
MODE_MESSAGE = "message"
MODES = frozenset({MODE_REPLY, MODE_MESSAGE})

#: The kill switch is off, or the session's engine (or kind) cannot fork.
FORK_UNAVAILABLE = "FORK_UNAVAILABLE"
#: The turn has no anchor: unknown, still in flight, or ended without a settled
#: assistant entry.
TURN_NOT_FORKABLE = "TURN_NOT_FORKABLE"
#: The message-mode prefill is longer than ``PREFILL_MAX_CHARS``.
PREFILL_TOO_LONG = "PREFILL_TOO_LONG"
#: Launch admission would queue the child (global or product cap). A fork never
#: queues: a queued row cannot carry its fork worktree.
FORK_AT_CAPACITY = "FORK_AT_CAPACITY"
#: The source's model is gated by ``require_approval``; a fork would launch it again.
#: (A ``block``ed model raises ``ModelBlockedError``, as ``create_session`` does.)
FORK_NEEDS_APPROVAL = "FORK_NEEDS_APPROVAL"
#: A live automation operation owns the source's lineage. A fork never joins one,
#: so it waits until the run finishes (released, or transferred to a released end).
FORK_OWNED_BY_AUTOMATION = "FORK_OWNED_BY_AUTOMATION"
#: An operation went live on the lineage between admission and the child driver's
#: ownership fence (a race). Expected and coded, never a 500.
FORK_OWNERSHIP_STALE = "FORK_OWNERSHIP_STALE"
FORK_CODES = frozenset({FORK_UNAVAILABLE, TURN_NOT_FORKABLE, PREFILL_TOO_LONG,
                        FORK_AT_CAPACITY, FORK_NEEDS_APPROVAL, FORK_OWNED_BY_AUTOMATION,
                        FORK_OWNERSHIP_STALE})

PREFILL_MAX_CHARS = 10_000


class ForkError(Exception):
    """An expected fork refusal; ``code`` is one of ``FORK_CODES``."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.error_data = {"code": code}


#: Namespace for ``fork_child_id`` (a fixed, arbitrary uuid4).
_CHILD_ID_NS = uuid.UUID("5f0c6b1e-8d2a-4c47-9a3e-2b7d4e9f1a60")


def fork_child_id(source_session_id: str, request_id: str) -> str:
    """The child session id for fork request *request_id* of the source — the same on
    every retry of that request, so a repeated request finds the child it made."""
    return str(uuid.uuid5(_CHILD_ID_NS, f"{source_session_id}:{request_id}"))


def fork_title(source_name: str | None) -> str:
    """The child session's title."""
    return f"Fork of {source_name or 'session'}"
