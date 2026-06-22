from __future__ import annotations
import json, asyncio, base64, hashlib
from . import events as ev


def _parse(path: str, max_turns: int) -> tuple[list, dict]:
    """Parse transcript JSONL into events and a map of turn_id → decoded image bytes.

    Returns (events, image_data) where image_data maps each user turn_id to a list
    of raw bytes for each inline base64 image block found.  The async caller uses
    image_data to look up Attachment rows and patch UserMessage.attachments.
    """
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
    # turn_id → list of decoded bytes for each inline base64 image block
    image_data: dict[str, list[bytes]] = {}
    meta_emitted = False
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
            # Collect inline base64 image blocks; guard decode errors gracefully.
            images = [b for b in content
                      if b.get("type") == "image"
                      and b.get("source", {}).get("type") == "base64"]
            decoded: list[bytes] = []
            for img in images:
                try:
                    decoded.append(base64.b64decode(img["source"]["data"]))
                except Exception:
                    pass  # malformed/large block — skip silently
            if decoded:
                image_data[uuid] = decoded
            if text or decoded:
                out.append(ev.UserMessage(turn_id=uuid, text=text, src_uuid=uuid))
            for r in results:
                out.append(ev.ToolResult(r["tool_use_id"], not r.get("is_error", False),
                                         {"content": r.get("content")}, uuid))
        elif typ == "assistant":
            # Reconstruct the model once (status strip needs it after re-attach).
            model = msg.get("model")
            if model and not meta_emitted:
                meta_emitted = True
                out.append(ev.SessionMeta(model=model, session_id=o.get("sessionId") or ""))
            out.append(ev.TurnStart(turn_id=uuid, src_uuid=uuid))
            for b in content:
                t = b.get("type")
                if t == "text":
                    out.append(ev.TextDelta(uuid, b.get("text", ""), uuid))
                elif t == "thinking":
                    out.append(ev.ThinkingDelta(uuid, b.get("thinking", ""), uuid))
                elif t == "tool_use":
                    out.append(ev.ToolCall(uuid, b.get("id", ""), b.get("name", ""), ev.tool_kind(b.get("name", "")),
                                           b.get("input", {}), uuid))
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            out.append(ev.TurnEnd(uuid, usage, None, uuid))
    # keep only the last max_turns assistant turns' worth — for the small fixture this is a no-op;
    # a turn boundary is a TurnStart. Trim from the front to the start of the (max_turns)-th-from-last turn.
    starts = [i for i, e in enumerate(out) if isinstance(e, ev.TurnStart)]
    if len(starts) > max_turns:
        out = out[starts[-max_turns]:]
    return out, image_data


async def replay_events(jsonl_path: str, *, max_turns: int = 20,
                        session_id: str | None = None) -> list:
    """Replay a claude transcript JSONL as a list of events.

    When *session_id* is provided, inline base64 image blocks found in user turns
    are hashed and matched against Attachment rows in the DB so that a reloaded
    page can re-render thumbnails.  If session_id is None, or no matching row is
    found, the UserMessage is emitted text-only (graceful degradation, no crash).
    """
    out, image_data = await asyncio.to_thread(_parse, jsonl_path, max_turns)

    if not session_id or not image_data:
        return out

    # Patch UserMessage events: look up Attachment by sha256 scoped to session_id.
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Attachment

    async with db_session() as db:
        for event in out:
            if not isinstance(event, ev.UserMessage):
                continue
            raw_images = image_data.get(event.turn_id)
            if not raw_images:
                continue
            attachments: list[dict] = []
            for data in raw_images:
                sha = hashlib.sha256(data).hexdigest()
                try:
                    row = (await db.execute(
                        select(Attachment).where(
                            Attachment.session_id == session_id,
                            Attachment.sha256 == sha,
                        )
                    )).scalars().first()
                except Exception:
                    row = None
                if row:
                    attachments.append({
                        "id": row.id,
                        "mediaType": row.media_type,
                        "filename": row.filename,
                    })
            if attachments:
                event.attachments = attachments

    return out
