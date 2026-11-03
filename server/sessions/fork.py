"""Fork from here (D-8): the request vocabulary shared by the orchestrator, the daemon
RPC, ``RemoteOrchestrator`` and the API route.

A fork refusal raises :class:`ForkError` with one of the codes below; a failure
making the child's worktree raises ``viberpit.worktrees.ForkWorktreeError`` with one
of ``FORK_WORKTREE_CODES``. Both carry ``error_data`` so the code survives the RPC
envelope and the daemon logs the refusal at INFO.
"""
from __future__ import annotations

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
#: The source's model is blocked by policy; a fork would launch it again.
MODEL_BLOCKED = "MODEL_BLOCKED"
FORK_CODES = frozenset({FORK_UNAVAILABLE, TURN_NOT_FORKABLE, PREFILL_TOO_LONG, MODEL_BLOCKED})

PREFILL_MAX_CHARS = 10_000


class ForkError(Exception):
    """An expected fork refusal; ``code`` is one of ``FORK_CODES``."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.error_data = {"code": code}


def fork_title(source_name: str | None) -> str:
    """The child session's title."""
    return f"Fork of {source_name or 'session'}"
