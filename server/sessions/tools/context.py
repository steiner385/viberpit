from __future__ import annotations

from collections.abc import Callable
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
    }
