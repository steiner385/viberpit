"""Type a queued session's start failure (spec 002 G6 / FR-303, #947).

The queue drain used to end a session that failed to start with nothing but a log
line: the card just said "Ended". :func:`classify` turns the exception into a
:class:`SpawnFailure` — a short reason token, whether one more try can plausibly
succeed, and a human sentence — that the drain records on the timeline
(``provenance.SPAWN_FAILED``) and on ``sessions.last_spawn_failure``.

Pure and side-effect-free so the table is exhaustively testable. ``transient`` is
deliberately narrow: only a failure that says nothing about the session itself (the
network was down, the CLI was slow to come up) earns the one re-enqueue; a bad
branch, a refused sandbox or a dead credential would fail the same way again.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from server.sessions.failure import classify as classify_turn
from viberpit import gh_credential

#: Re-enqueues a transient failure gets before the drain ends the session.
MAX_TRANSIENT_RETRIES = 1

#: The queue-entry params key carrying how many transient retries were spent. Popped
#: by the drain with ``queue_reason`` before the params reach ``_spawn_session``.
ATTEMPT_PARAM = "spawn_attempt"

_MESSAGE_CAP = 300


@dataclass(frozen=True)
class SpawnFailure:
    reason: str
    transient: bool
    message: str

    def to_detail(self, attempt: int, *, requeued: bool) -> dict[str, Any]:
        return {"reason": self.reason, "transient": self.transient, "attempt": attempt,
                "requeued": requeued, "message": self.message}


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text.splitlines()[0][:_MESSAGE_CAP]


def classify(exc: BaseException, *, sandbox_error: type[BaseException] | None = None,
             start_timeout: type[BaseException] | None = None,
             obsolete_pr: int | None = None) -> SpawnFailure:
    """The typed failure for *exc*. The orchestrator passes its own exception
    classes (``SandboxError``, ``DriverStartTimeout``) rather than this module
    importing the orchestrator, and ``obsolete_pr`` when it has already proved the
    branch was auto-deleted by its PR merging (``_queued_spawn_merged_pr``)."""
    if obsolete_pr is not None:
        return SpawnFailure("obsolete", False, f"PR #{obsolete_pr} merged before start")
    if sandbox_error is not None and isinstance(exc, sandbox_error):
        return SpawnFailure("sandbox_refused", False, _first_line(exc))
    if start_timeout is not None and isinstance(exc, start_timeout):
        return SpawnFailure("start_timeout", True, _first_line(exc))
    message = _first_line(exc)
    if gh_credential.classify_gh_failure(str(exc)) == gh_credential.UNREACHABLE:
        return SpawnFailure("network", True, message)
    # failure.py is the live-turn taxonomy; reusing its text inference keeps a
    # start-time 401/429 classified the same as an in-turn one.
    kind = classify_turn(None, str(exc)).kind
    if kind in ("auth_401", "auth_forbidden", "auth_stale"):
        return SpawnFailure("auth", False, message)
    if kind == "rate_limit":
        return SpawnFailure("rate_limited", False, message)
    return SpawnFailure("start_error", False, message)
