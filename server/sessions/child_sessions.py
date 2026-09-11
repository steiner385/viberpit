"""Pure policy for child sessions — limits, the parent notice, nothing stateful.

Spec: docs/superpowers/specs/2026-09-11-child-session-dispatch-design.md §4.3, §4.4.
The orchestrator owns the state (records, drivers); this module owns the decisions so
they are unit-testable without one.
"""
from __future__ import annotations

#: A child's stop transitions the parent is told about (§4.4). Busy/idle transitions
#: are not stops and never notify.
CHILD_NOTIFY_STATUSES: frozenset[str] = frozenset({"complete", "done", "waiting_question", "ended"})

#: A child in one of these no longer counts against the live-children cap.
LIVE_CHILD_EXCLUDED: frozenset[str] = frozenset({"complete", "done", "ended", "archived"})

SUMMARY_LIMIT = 800


class ChildSessionLimit(Exception):
    """A dispatch the policy refuses; ``str(exc)`` is the model-readable reason."""


def check_limits(*, enabled: bool, parent_depth: int, live_children: int, cap: int,
                 max_depth: int) -> None:
    if not enabled:
        raise ChildSessionLimit("child sessions are disabled on this host "
                                "(CCSWITCH_CHILD_SESSIONS_ENABLED=false)")
    if parent_depth + 1 > max_depth:
        raise ChildSessionLimit(f"children may not spawn grandchildren beyond depth {max_depth}; "
                                "do this work yourself or hand it back")
    if live_children >= cap:
        raise ChildSessionLimit(f"this session already has {live_children} live children "
                                f"(cap {cap}); wait for one to finish before dispatching another")


def truncate(text: str, limit: int = SUMMARY_LIMIT) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


def build_child_notice(*, session_id: str, name: str, status: str, last_message: str,
                       hand_back_reason: str | None = None,
                       pr_resolution: str | None = None) -> str:
    """The prompt a parent receives when a child stops (§4.4) — the same shape as the
    CLI's own <task-notification>, so the parent's model reads it the way it already
    reads a finished background task."""
    lines = ["<child-session-notification>",
             f"<session-id>{session_id}</session-id>",
             f"<name>{name}</name>",
             f"<status>{status}</status>"]
    if hand_back_reason:
        lines.append(f"<hand-back-reason>{hand_back_reason}</hand-back-reason>")
    if pr_resolution:
        lines.append(f"<pr-resolution>{pr_resolution}</pr-resolution>")
    lines.append(f"<summary>{truncate(last_message)}</summary>")
    lines.append(f"<url>/sessions/{session_id}</url>")
    lines.append("</child-session-notification>")
    return "\n".join(lines)
