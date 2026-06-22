"""Server-side sub-agent summary.

Mirrors the frontend ``selectSubAgents`` (web/src/session/selectors.ts): a
sub-agent surfaces to the parent session as a ``Task``/``Agent`` tool call, and
its status is derived from the paired tool result (present+ok → done,
present+!ok → failed, absent → running). We compute this on the backend so the
dashboard session list can show sub-agents nested under each session without the
client having to stream the whole transcript.

Built from replayed events (``replay.replay_events``). Because replay skips
``isSidechain`` entries, only top-level (main-issued) sub-agents appear here;
deep nesting is reflected live in the in-session ``state.subAgents`` map. Each
summary entry's ``agentId`` is the Task tool-use id — the same key the frontend
reducer uses for ``state.subAgents[toolId].turns`` — so the drill-down lines up.
"""
from __future__ import annotations
from . import events as ev

_SUBAGENT_TOOLS = {"Task", "Agent"}


def summarize_transcript(events: list) -> list[dict]:
    """Reduce replayed events to a per-sub-agent summary list.

    Returns ``[{agentId, parentAgentId, agentType, description, status}]`` in
    first-seen order. ``parentAgentId`` is None (top-level) for everything replay
    surfaces.
    """
    order: list[str] = []
    by_id: dict[str, dict] = {}
    results: dict[str, bool] = {}  # tool_id -> ok
    for e in events:
        if isinstance(e, ev.ToolCall) and e.name in _SUBAGENT_TOOLS:
            inp = e.input if isinstance(e.input, dict) else {}
            atype = inp.get("subagent_type")
            desc = inp.get("description")
            if e.tool_id not in by_id:
                order.append(e.tool_id)
            by_id[e.tool_id] = {
                "agentId": e.tool_id,
                "parentAgentId": None,
                "agentType": atype if isinstance(atype, str) else "agent",
                "description": desc if isinstance(desc, str) else "",
                "status": "running",
            }
        elif isinstance(e, ev.ToolResult):
            results[e.tool_id] = bool(e.ok)
    for tool_id, ok in results.items():
        if tool_id in by_id:
            by_id[tool_id]["status"] = "done" if ok else "failed"
    return [by_id[t] for t in order]
