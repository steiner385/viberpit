from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SessionToolContext:
    """What a session tool may know and touch. Built per driver build by the
    orchestrator; ``record()`` is a live accessor, not a snapshot, so a tool called
    after a rename or model switch sees the current row."""

    session_id: str
    identity: str            # owner identity; audit attribution
    kind: str | None       # the row's session kind as the orchestrator resolved it
    engine: str
    record: Callable[[], Any]  # -> SessionRecord | None
    host: str | None = None
    #: This engine can act on a self-directed model request
    #: (``AgentCapabilities.self_model_switch``). Declaring `request_model` without
    #: it offers a tool whose result is silently dropped.
    self_model_switch: bool = False
    #: This engine gets the signalling tools rather than the marker contract
    #: (``AgentCapabilities.durable_tool_surface``). False → the markers instead.
    signalling_tools: bool = False
    #: Child sessions are enabled on this host (``VIBERPIT_CHILD_SESSIONS_ENABLED``) —
    #: with `signalling_tools`, the mount predicate for `dispatch_session`.
    child_sessions: bool = False
    #: Bound by the orchestrator to ``dispatch_child_session(parent_id, identity=…)``;
    #: the `dispatch_session` handler's ONE call. None when the surface is off.
    dispatch_child: Callable[..., Awaitable[Any]] | None = None
    #: Bound to ``reply_to_child_session(parent_id, identity=…)``; the
    #: `reply_to_child` handler's ONE call (#2911). None when the surface is off.
    reply_child: Callable[..., Awaitable[Any]] | None = None
    #: Live accessor for this session's children (records), read by `session_context`.
    children: Callable[[], list[Any]] | None = None
    #: ``signal_prose(stem) -> str | None``: the prose of the top-level message that
    #: carried this turn's ``stem`` signal call (``"hand_back"``), once the driver has
    #: read it; None when unknowable (no driver, the call never arrived). Read by
    #: `hand_back` so its acknowledgement says what the user actually sees — thinking
    #: is invisible, and a session cannot tell otherwise.
    signal_prose: Callable[[str], Awaitable[str | None]] | None = None
    #: ``note_shared_pr(repo, number)``: bound by the orchestrator so a PR the session
    #: shares via `share_artifact` counts as a PR fact for the
    #: completion bar (#2663). None when unwired (tests, Codex).
    note_shared_pr: Callable[[str, int], Awaitable[bool | None]] | None = None


def context_snapshot(ctx: SessionToolContext) -> dict[str, Any]:
    """The context block stored on a feedback row and returned by `session_context`.
    Display-safe only: account NAME, never credentials or config paths."""
    rec = ctx.record()
    if rec is None:
        return {"sessionId": ctx.session_id, "host": ctx.host, "engine": ctx.engine}
    engine = getattr(rec, "engine", ctx.engine)
    goal = getattr(rec, "active_goal", None) or getattr(rec, "inferred_goal", None)
    return {
        "sessionId": ctx.session_id,
        "name": getattr(rec, "name", None),
        "repo": getattr(rec, "repo", None),
        "branch": getattr(rec, "branch", None),
        "cwd": getattr(rec, "cwd", None),
        "host": getattr(rec, "host", None) or ctx.host,
        "engine": getattr(engine, "value", engine),
        "model": getattr(rec, "model", None),
        "account": getattr(rec, "account", None),
        "origin": getattr(rec, "origin", None),
        "originRef": getattr(rec, "origin_ref", None),
        "goal": goal,
        "permissionMode": getattr(rec, "permission_mode", None),
        # Lineage (child-session spec §4.5): how a parent waits on or reconciles its
        # children without a second tool.
        "parentSessionId": getattr(rec, "parent_session_id", None),
        "children": [{"id": c.id, "name": getattr(c, "name", None),
                      "status": getattr(c, "status", None)}
                     for c in (ctx.children() if ctx.children else [])],
    }
