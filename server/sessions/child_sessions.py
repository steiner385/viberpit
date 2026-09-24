"""Pure policy for child sessions — limits, notify decisions, the parent notice.

Spec: docs/superpowers/specs/2026-09-11-child-session-dispatch-design.md §4.3, §4.4.
The orchestrator owns the state (records, drivers); this module owns the decisions so
they are unit-testable without one.
"""
from __future__ import annotations

#: A child's stop transitions the parent may be told about (§4.4). Busy/idle
#: transitions are not stops and never notify. ``archived``/``ended`` come from
#: archive_session / close_session, which write their status directly rather than
#: through the driver's status callback.
CHILD_NOTIFY_STATUSES: frozenset[str] = frozenset(
    {"complete", "done", "waiting_question", "handed_back", "ended", "archived"})

#: The stops the user ANSWERS, after which the child continues from where it stopped —
#: so the guard clears and the child's next stop is reported again. Both spellings of
#: "this child is waiting on a human": a structured AskUserQuestion card
#: (``waiting_question``) and a bare hand-back (``handed_back``, #2321). Before the
#: split these were one status and this was an equality check against it.
CHILD_ANSWERABLE_STOPS: frozenset[str] = frozenset({"waiting_question", "handed_back"})

#: A child in one of these no longer counts against the live-children cap.
LIVE_CHILD_EXCLUDED: frozenset[str] = frozenset({"complete", "done", "ended", "archived"})

SUMMARY_LIMIT = 800


class ChildSessionLimit(Exception):
    """A dispatch the policy refuses; ``str(exc)`` is the model-readable reason."""


def check_limits(*, enabled: bool, parent_depth: int, live_children: int, cap: int,
                 max_depth: int) -> None:
    if not enabled:
        raise ChildSessionLimit("child sessions are disabled on this host "
                                "(VIBERPIT_CHILD_SESSIONS_ENABLED=false)")
    if parent_depth + 1 > max_depth:
        raise ChildSessionLimit(f"children may not spawn grandchildren beyond depth {max_depth}; "
                                "do this work yourself or hand it back")
    if live_children >= cap:
        raise ChildSessionLimit(f"this session already has {live_children} live children "
                                f"(cap {cap}); wait for one to finish before dispatching another")


def notify_decision(status: str, notified: str | None) -> tuple[bool, str | None]:
    """Whether a child's new *status* is reported to the parent, and the guard value to
    persist afterwards (``Session.parent_notified_status``).

    ``done`` is what every ended turn produces, not a finished-work signal: once the
    user starts steering the child, each of its turns ends in ``done``. So ``done``
    notifies only while nothing has been reported yet (the child's FIRST stop), and it
    latches. ``complete``, ``waiting_question``, ``handed_back``, ``ended`` and
    ``archived`` notify on every change of status. A non-stop status (the child running
    again) clears the guard only after one of ``CHILD_ANSWERABLE_STOPS`` — the stops the
    user answers and the child continues from — so the child's next stop is reported
    again.
    """
    if status not in CHILD_NOTIFY_STATUSES:
        return False, (None if notified in CHILD_ANSWERABLE_STOPS else notified)
    if status == "done":
        return (notified is None), (notified or "done")
    return (notified != status), status


def truncate(text: str, limit: int = SUMMARY_LIMIT) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


def _escape(text: str) -> str:
    """Model-authored text goes inside a pseudo-XML block another model reads as
    harness framing; a stray ``</summary>`` must not close the block early."""
    return (text or "").replace("<", "&lt;").replace(">", "&gt;")


def build_child_notice(*, session_id: str, name: str, status: str, last_message: str,
                       hand_back_reason: str | None = None,
                       pr_resolution: str | None = None) -> str:
    """The prompt a parent receives when a child stops (§4.4) — the same shape as the
    CLI's own <task-notification>, so the parent's model reads it the way it already
    reads a finished background task."""
    lines = ["<child-session-notification>",
             f"<session-id>{session_id}</session-id>",
             f"<name>{_escape(name)}</name>",
             f"<status>{status}</status>"]
    if hand_back_reason:
        lines.append(f"<hand-back-reason>{_escape(hand_back_reason)}</hand-back-reason>")
    if pr_resolution:
        lines.append(f"<pr-resolution>{pr_resolution}</pr-resolution>")
    lines.append(f"<summary>{_escape(truncate(last_message))}</summary>")
    lines.append(f"<url>/sessions/{session_id}</url>")
    lines.append("</child-session-notification>")
    return "\n".join(lines)
