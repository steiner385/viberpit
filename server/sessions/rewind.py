"""Rewind to here (D-9): the refusal vocabulary and the CLI control-request adapter
shared by the Claude driver, the orchestrator, the daemon RPC and the API route.

The CLI surfaces are internal (constitution I) and verified against claude 2.1.317 in
``docs/superpowers/specs/2026-11-03-d9-rewind-verification.md``:

- ``rewind_files {user_message_id, dry_run?}`` answers ``{canRewind, error?,
  filesChanged?, insertions?, deletions?, skippedLinks?}``. A ``canRewind:false``
  without ``dry_run`` comes back as a control *error* carrying the same text.
- ``rewind_conversation {target_message_uuid, last_seen_user_message_uuid?}`` answers
  ``{rewound:true, targetMessageUuid, prefillText, precedingAssistantUuid}`` or
  ``{rewound:false, error, reason}``.

Every refusal raises :class:`RewindError` with one of ``REWIND_CODES``; it carries
``error_data`` (as D-8's ``ForkError`` does) so the code survives the RPC envelope
and the daemon logs the refusal at INFO. ``server/tests/test_rewind_cli_contract.py``
re-checks the wire against the installed binary.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: The kill switch is off, the engine cannot rewind, or the session has no live
#: client (or the SDK client lacks the control channel).
REWIND_UNAVAILABLE = "REWIND_UNAVAILABLE"
#: A turn is running, a permission card or question is pending, or the CLI has
#: prompts queued.
REWIND_BUSY = "REWIND_BUSY"
#: The prompt has no file checkpoint (sent before checkpointing was on, or the
#: process runs without it). The conversation can still be rewound.
REWIND_NO_CHECKPOINT = "REWIND_NO_CHECKPOINT"
#: Later turns exist that the caller has not seen, or the session changed under
#: the rewind.
REWIND_STALE_TARGET = "REWIND_STALE_TARGET"
#: The target is not on the session's active conversation chain.
REWIND_TARGET_NOT_FOUND = "REWIND_TARGET_NOT_FOUND"
#: Cutting at the target would split a tool call from its result.
REWIND_SPLITS_TOOL_CALL = "REWIND_SPLITS_TOOL_CALL"
#: The target is not a user prompt.
REWIND_NOT_USER_MESSAGE = "REWIND_NOT_USER_MESSAGE"
#: The undo backup would exceed its size cap.
REWIND_TOO_LARGE = "REWIND_TOO_LARGE"
#: The rewind is no longer undoable (a newer rewind, or a prompt since).
REWIND_UNDO_EXPIRED = "REWIND_UNDO_EXPIRED"
#: Anything else the CLI refused or failed with.
REWIND_FAILED = "REWIND_FAILED"
REWIND_CODES = frozenset({REWIND_UNAVAILABLE, REWIND_BUSY, REWIND_NO_CHECKPOINT,
                          REWIND_STALE_TARGET, REWIND_TARGET_NOT_FOUND,
                          REWIND_SPLITS_TOOL_CALL, REWIND_NOT_USER_MESSAGE,
                          REWIND_TOO_LARGE, REWIND_UNDO_EXPIRED, REWIND_FAILED})

#: Every ``reason`` the 2.1.317 ``rewind_conversation`` handler can send (its ``bw``
#: table) → our code. ``expires_at`` and ``refuse_if_usage_limited`` are never sent,
#: so ``expired`` / ``usage_limited`` are unreachable; they map to FAILED.
CLI_REASON_CODES: dict[str, str] = {
    "turn_running": REWIND_BUSY,
    "commands_queued": REWIND_BUSY,
    "prompt_pending": REWIND_BUSY,
    "stale_target": REWIND_STALE_TARGET,
    "unseen_later_turn": REWIND_STALE_TARGET,
    "state_changed": REWIND_STALE_TARGET,
    "target_not_found": REWIND_TARGET_NOT_FOUND,
    "target_splits_tool_call": REWIND_SPLITS_TOOL_CALL,
    "delivered_poll_events_in_range": REWIND_SPLITS_TOOL_CALL,
    "poll_tool_result_target": REWIND_NOT_USER_MESSAGE,
    "persist_failed": REWIND_FAILED,
    "usage_limited": REWIND_FAILED,
    "expired": REWIND_FAILED,
}

#: ``rewind_files`` error text → code. "Not enabled" means the process was spawned
#: without checkpointing, so for the caller it is the same as no checkpoint: the
#: rewind is offered as conversation-only.
_ERROR_TEXT_CODES: tuple[tuple[str, str], ...] = (
    ("No file checkpoint found", REWIND_NO_CHECKPOINT),
    ("File rewinding is not enabled", REWIND_NO_CHECKPOINT),
    ("is not a user message", REWIND_NOT_USER_MESSAGE),
)

#: Seconds to wait for one rewind control reply (the SDK's own default).
REWIND_CONTROL_TIMEOUT_S = 60.0


class RewindError(Exception):
    """An expected rewind refusal; ``code`` is one of ``REWIND_CODES``."""

    def __init__(self, code: str, message: str | None = None, *,
                 reason: str | None = None) -> None:
        if code not in REWIND_CODES:
            raise ValueError(f"unknown rewind code {code!r}")
        super().__init__(message or code)
        self.code = code
        self.reason = reason
        self.error_data: dict[str, str] = {"code": code}
        if reason is not None:
            self.error_data["reason"] = reason


def map_cli_error(resp: Mapping[str, Any]) -> RewindError:
    """The :class:`RewindError` for a CLI refusal: a ``rewind_conversation`` reply with
    ``rewound:false``, a ``rewind_files`` reply with ``canRewind:false``, or
    ``{"error": text}`` for a control error. Unknown shapes are ``REWIND_FAILED``."""
    reason = resp.get("reason")
    error = resp.get("error")
    text = error if isinstance(error, str) else ""
    if isinstance(reason, str) and reason:
        return RewindError(CLI_REASON_CODES.get(reason, REWIND_FAILED), text or reason,
                           reason=reason)
    for needle, code in _ERROR_TEXT_CODES:
        if needle in text:
            return RewindError(code, text)
    return RewindError(REWIND_FAILED, text or None)


def files_request(user_message_id: str, *, dry_run: bool) -> dict[str, Any]:
    """The ``rewind_files`` control request. The real run is byte-for-byte the SDK's
    own ``rewind_files()`` payload; only the dry run adds ``dry_run``."""
    req: dict[str, Any] = {"subtype": "rewind_files", "user_message_id": user_message_id}
    if dry_run:
        req["dry_run"] = True
    return req


def conversation_request(target_uuid: str, *, last_seen: str | None) -> dict[str, Any]:
    """The ``rewind_conversation`` control request. Without ``last_seen`` the CLI
    refuses any target that has later turns (``stale_target``)."""
    req: dict[str, Any] = {"subtype": "rewind_conversation", "target_message_uuid": target_uuid}
    if last_seen is not None:
        req["last_seen_user_message_uuid"] = last_seen
    return req


@dataclass(frozen=True)
class RewindPreview:
    """A ``rewind_files`` dry run. ``files`` are as the CLI reports them (absolute);
    the CLI gives only aggregate line counts."""
    files: tuple[str, ...]
    insertions: int
    deletions: int
    checkpoint_available: bool

    @classmethod
    def from_cli(cls, resp: Mapping[str, Any]) -> RewindPreview:
        files = resp.get("filesChanged") or ()
        return cls(files=tuple(str(f) for f in files),
                   insertions=int(resp.get("insertions") or 0),
                   deletions=int(resp.get("deletions") or 0),
                   checkpoint_available=bool(resp.get("canRewind")))

    @classmethod
    def no_checkpoint(cls) -> RewindPreview:
        return cls(files=(), insertions=0, deletions=0, checkpoint_available=False)


@dataclass(frozen=True)
class ConversationRewind:
    """A successful ``rewind_conversation``: the cut prompt's text (for the composer)
    and the assistant entry the chain now ends at (``None`` at the first prompt)."""
    prefill: str | None
    preceding_uuid: str | None

    @classmethod
    def from_cli(cls, resp: Mapping[str, Any]) -> ConversationRewind:
        prefill = resp.get("prefillText")
        preceding = resp.get("precedingAssistantUuid")
        return cls(prefill=prefill if isinstance(prefill, str) else None,
                   preceding_uuid=preceding if isinstance(preceding, str) else None)
