from __future__ import annotations
import json, asyncio
from . import events as ev


def _parse(path: str, max_turns: int) -> list:
    lines = [json.loads(l) for l in open(path, errors="ignore") if l.strip()]
    out: list = []
    for o in lines:
        typ = o.get("type")
        msg = o.get("message") or {}
        content = msg.get("content") or []
        uuid = o.get("uuid")
        if typ == "user":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            results = [b for b in content if b.get("type") == "tool_result"]
            if text:
                out.append(ev.UserMessage(turn_id=uuid, text=text, src_uuid=uuid))
            for r in results:
                out.append(ev.ToolResult(r["tool_use_id"], not r.get("is_error", False),
                                         {"content": r.get("content")}, uuid))
        elif typ == "assistant":
            out.append(ev.TurnStart(turn_id=uuid, src_uuid=uuid))
            for b in content:
                t = b.get("type")
                if t == "text":
                    out.append(ev.TextDelta(uuid, b["text"], uuid))
                elif t == "thinking":
                    out.append(ev.ThinkingDelta(uuid, b.get("thinking", ""), uuid))
                elif t == "tool_use":
                    out.append(ev.ToolCall(uuid, b["id"], b["name"], ev.tool_kind(b["name"]),
                                           b.get("input", {}), uuid))
            out.append(ev.TurnEnd(uuid, None, uuid))
    # keep only the last max_turns assistant turns' worth — for the small fixture this is a no-op;
    # a turn boundary is a TurnStart. Trim from the front to the start of the (max_turns)-th-from-last turn.
    starts = [i for i, e in enumerate(out) if isinstance(e, ev.TurnStart)]
    if len(starts) > max_turns:
        out = out[starts[-max_turns]:]
    return out


async def replay_events(jsonl_path: str, *, max_turns: int = 20) -> list:
    return await asyncio.to_thread(_parse, jsonl_path, max_turns)
