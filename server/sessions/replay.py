from __future__ import annotations
import json, asyncio
from . import events as ev


def _entry_ptui(o: dict):
    """Non-null parent_tool_use_id from top-level or message, else None."""
    v = o.get("parent_tool_use_id")
    if v:
        return v
    m = o.get("message")
    if isinstance(m, dict) and m.get("parent_tool_use_id"):
        return m.get("parent_tool_use_id")
    return None


def _agent_ids(lines: list) -> dict:
    """Map each entry uuid -> owning agent_id (None = main), defensively.

    Priority per entry: (1) non-null parent_tool_use_id wins; (2) else
    isSidechain entries are grouped by parentUuid roots and associated to
    main-agent Task tool_uses in document order; (3) else None.
    """
    by_uuid = {o.get("uuid"): o for o in lines if o.get("uuid")}
    # Task tool_use ids in document order (main agent only).
    task_ids = []
    for o in lines:
        if o.get("isSidechain"):
            continue
        for b in ((o.get("message") or {}).get("content") or []):
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Task":
                task_ids.append(b.get("id"))

    def root_of(o):
        seen = set()
        cur = o
        while cur is not None and cur.get("isSidechain") and cur.get("uuid") not in seen:
            seen.add(cur.get("uuid"))
            parent = by_uuid.get(cur.get("parentUuid"))
            if parent is None or not parent.get("isSidechain"):
                return cur.get("uuid")
            cur = parent
        return o.get("uuid")

    roots = []
    for o in lines:
        if o.get("isSidechain"):
            r = root_of(o)
            if r not in roots:
                roots.append(r)
    root_to_agent = {r: (task_ids[i] if i < len(task_ids) else f"sidechain:{r}")
                     for i, r in enumerate(roots)}
    out = {}
    for o in lines:
        ptui = _entry_ptui(o)
        if ptui:                                   # (1) explicit link wins
            out[o.get("uuid")] = ptui
        elif o.get("isSidechain"):                 # (2) sidechain grouping
            out[o.get("uuid")] = root_to_agent.get(root_of(o))
        else:                                      # (3) main agent
            out[o.get("uuid")] = None
    return out


def _parse(path: str, max_turns: int) -> list:
    lines = []
    with open(path, errors="ignore") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    agents = _agent_ids(lines)
    out: list = []
    meta_emitted = False
    tasks: dict = {}
    # Map Task tool_use_id -> {"type": ..., "description": ..., "issuing_aid": ...}
    # "issuing_aid" is the agent_id of the entry that issued the Task (None = main).
    for o in lines:
        typ = o.get("type")
        msg = o.get("message") or {}
        # Claude transcripts use TWO shapes for `content`: a plain string (common for
        # user turns, e.g. "content": "tell me a lullaby") OR a list of content blocks.
        # Normalize to a list of dict blocks so the iterators below never choke on a
        # str (which would iterate characters → b.get() AttributeError → blank replay).
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            content = [b for b in content if isinstance(b, dict)]
        else:
            content = []
        uuid = o.get("uuid")
        aid = agents.get(uuid)
        if typ == "user":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            results = [b for b in content if b.get("type") == "tool_result"]
            if text:
                out.append(ev.UserMessage(turn_id=uuid, text=text, src_uuid=uuid, agent_id=aid))
            for r in results:
                ok = not r.get("is_error", False)
                out.append(ev.ToolResult(r["tool_use_id"], ok, {"content": r.get("content")}, uuid, agent_id=aid))
                task = tasks.get(r["tool_use_id"])
                if task is not None:
                    # Use the issuing_aid recorded when the Task was created so the
                    # terminal update carries the correct parent_agent_id for nested tasks.
                    out.append(ev.SubAgentUpdate(r["tool_use_id"], task["issuing_aid"],
                                                 str(task["type"]), str(task["description"]),
                                                 "done" if ok else "failed"))
        elif typ == "assistant":
            # Reconstruct the model once (status strip needs it after re-attach).
            model = msg.get("model")
            if model and not meta_emitted:
                meta_emitted = True
                out.append(ev.SessionMeta(model=model, session_id=o.get("sessionId") or ""))
            out.append(ev.TurnStart(turn_id=uuid, src_uuid=uuid, agent_id=aid))
            for b in content:
                t = b.get("type")
                if t == "text":
                    out.append(ev.TextDelta(uuid, b.get("text", ""), uuid, agent_id=aid))
                elif t == "thinking":
                    out.append(ev.ThinkingDelta(uuid, b.get("thinking", ""), uuid, agent_id=aid))
                elif t == "tool_use":
                    out.append(ev.ToolCall(uuid, b.get("id", ""), b.get("name", ""),
                                           ev.tool_kind(b.get("name", "")), b.get("input", {}), uuid, agent_id=aid))
                    if b.get("name") == "Task":
                        # Emit SubAgentUpdate(running) for EVERY Task tool_use, regardless of
                        # whether the issuing entry is from main (aid=None) or a nested sub-agent.
                        # parent_agent_id = aid (the agent_id of the issuing entry) so the tree
                        # node is placed correctly: None for main-issued Tasks, the parent sub-agent
                        # id for nested Tasks.  Track issuing_aid so the terminal update (from the
                        # tool_result in a later user entry) uses the same parent.
                        # NOTE for isSidechain (branch 2): deep (>1 level) nesting via the
                        # isSidechain fallback is flattened to the nearest resolvable Task because
                        # root_of() only climbs to the outermost sidechain root.  The authoritative
                        # nesting path is parent_tool_use_id (branch 1), which is fully correct.
                        inp = b.get("input") or {}
                        tasks[b.get("id", "")] = {"type": inp.get("subagent_type") or "agent",
                                                  "description": inp.get("description") or "",
                                                  "issuing_aid": aid}
                        out.append(ev.SubAgentUpdate(b.get("id", ""), aid,
                                                     str(inp.get("subagent_type") or "agent"),
                                                     str(inp.get("description") or ""), "running"))
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            out.append(ev.TurnEnd(uuid, usage, None, uuid, agent_id=aid))
    # keep only the last max_turns assistant turns' worth — for the small fixture this is a no-op;
    # a turn boundary is a TurnStart. Trim from the front to the start of the (max_turns)-th-from-last turn.
    starts = [i for i, e in enumerate(out) if isinstance(e, ev.TurnStart)]
    if len(starts) > max_turns:
        out = out[starts[-max_turns]:]
    return out


async def replay_events(jsonl_path: str, *, max_turns: int = 20) -> list:
    return await asyncio.to_thread(_parse, jsonl_path, max_turns)
