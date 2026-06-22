from __future__ import annotations
import json, asyncio
from . import events as ev


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
    out: list = []
    meta_event = None
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
        if typ == "user":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            results = [b for b in content if b.get("type") == "tool_result"]
            imgs = []
            for b in content:
                if b.get("type") == "image":
                    src = b.get("source") or {}
                    if src.get("type") == "base64":
                        mt = src.get("media_type", "image/png")
                        imgs.append({"url": f"data:{mt};base64,{src.get('data', '')}",
                                     "mime": mt, "name": ""})
            if text or imgs:
                out.append(ev.UserMessage(turn_id=uuid, text=text, src_uuid=uuid,
                                          images=imgs or None))
            for r in results:
                out.append(ev.ToolResult(r["tool_use_id"], not r.get("is_error", False),
                                         {"content": r.get("content")}, uuid))
        elif typ == "assistant":
            # Reconstruct the model once (status strip needs it after re-attach).
            model = msg.get("model")
            if model and meta_event is None:
                # Held aside (not appended) so the trim below can't slice it away on
                # long sessions — the status strip needs the model after re-attach.
                meta_event = ev.SessionMeta(model=model, session_id=o.get("sessionId") or "")
            out.append(ev.TurnStart(turn_id=uuid, src_uuid=uuid))
            for b in content:
                t = b.get("type")
                if t == "text":
                    out.append(ev.TextDelta(uuid, b.get("text", ""), uuid))
                elif t == "thinking":
                    out.append(ev.ThinkingDelta(uuid, b.get("thinking", ""), uuid))
                elif t == "tool_use":
                    if b.get("name") == "AskUserQuestion":
                        # Mirror the live translator: AskUserQuestion is presented as
                        # the selectable QuestionCard via Question events, never a raw
                        # ToolCall (which renders the input as JSON). Reconstruct the
                        # Question event(s) from the tool_use input so a detached /
                        # reloaded session shows the question, not the raw JSON.
                        for qi, q in enumerate((b.get("input") or {}).get("questions") or []):
                            if not isinstance(q, dict):
                                continue
                            opts = [o if isinstance(o, dict) else {"label": str(o)}
                                    for o in (q.get("options") or [])]
                            out.append(ev.Question(
                                question_id=f"{b.get('id') or uuid}-{qi}",
                                turn_id=uuid,
                                header=q.get("header", ""),
                                question=q.get("question", ""),
                                options=opts,
                                multi_select=bool(q.get("multiSelect", False)),
                            ))
                    else:
                        out.append(ev.ToolCall(uuid, b.get("id", ""), b.get("name", ""),
                                               ev.tool_kind(b.get("name", "")), b.get("input", {}), uuid))
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            out.append(ev.TurnEnd(uuid, usage, None, uuid))
    # keep only the last max_turns assistant turns' worth — for the small fixture this is a no-op;
    # a turn boundary is a TurnStart. Trim from the front to the start of the (max_turns)-th-from-last turn.
    starts = [i for i, e in enumerate(out) if isinstance(e, ev.TurnStart)]
    if len(starts) > max_turns:
        out = out[starts[-max_turns]:]
    # Prepend the model meta (if any) so it always precedes the kept turns.
    if meta_event is not None:
        out.insert(0, meta_event)
    return out


async def replay_events(jsonl_path: str, *, max_turns: int = 20) -> list:
    return await asyncio.to_thread(_parse, jsonl_path, max_turns)
