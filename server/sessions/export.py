"""Transcript export formatters (Task 4).

transcript_markdown() — render redacted wire-event dicts as a human-readable
Markdown document, suitable for the /export.md download endpoint.

normalized_ndjson() — render the same wire events as newline-delimited JSON for
the /export.jsonl download, used by every engine that has no native transcript
file of its own to stream (Codex).

safe_filename() — sanitise a string into a filesystem-safe filename stem.
"""
from __future__ import annotations

import json
from typing import Any

# Version tag on the header record of a normalized NDJSON export. Bump it if the
# event wire shape changes in a way a downstream parser must notice; a consumer
# that pins v1 then fails loudly instead of silently misreading the new events.
NORMALIZED_EXPORT_FORMAT = "ccswitch.normalized-events/v1"


def _fence(text: str, lang: str = "") -> str:
    return f"```{lang}\n{text}\n```"


def transcript_markdown(events: list[dict[str, Any]], *, title: str) -> str:
    model = next((e.get("model") for e in events if e.get("type") == "sessionMeta"), None)
    lines: list[str] = [f"# {title}"]
    if model:
        lines.append(f"_Model: {model}_")
    lines.append("")
    for e in events:
        t = e.get("type")
        if t == "user":
            lines += ["## User", "", e.get("text", ""), ""]
            for img in e.get("images") or []:
                lines += [f"_[image: {img.get('name') or img.get('mime', 'image')}]_", ""]
        elif t == "turnStart":
            lines += ["## Assistant", ""]
        elif t == "textDelta":
            lines += [e.get("text", ""), ""]
        elif t == "thinkingDelta":
            lines += ["> _thinking_", "> " + e.get("text", "").replace("\n", "\n> "), ""]
        elif t == "toolCall":
            inp = json.dumps(e.get("input", {}), indent=2, ensure_ascii=False)
            lines += [f"**Tool: {e.get('name', '')}**", _fence(inp, "json"), ""]
        elif t == "toolResult":
            payload = e.get("payload", {})
            content = payload.get("content") if isinstance(payload, dict) else payload
            body = content if isinstance(content, str) else json.dumps(content, indent=2, ensure_ascii=False)
            status = "ok" if e.get("ok") else "error"
            lines += [f"**Result ({status})**", _fence(str(body)), ""]
        elif t == "question":
            lines += [f"**Question: {e.get('question', '')}**", ""]
    return "\n".join(lines) + "\n"


def normalized_ndjson(events: list[dict[str, Any]], *, session_id: str,
                      engine: str, model: str | None = None) -> str:
    """Render *events* as NDJSON, led by a record declaring what this file IS.

    The Claude ``/export.jsonl`` streams the CLI's own transcript byte-for-byte.
    Codex has no such file — its history lives inside the app-server — so this is
    what it exports instead: ccswitch's normalized replay events, one JSON object
    per line, in the same wire shape the web client already consumes.

    The header record is the point. Serving normalized events under a `.jsonl`
    name with no marker would invite a consumer to parse them as Claude-native
    transcript records, which they are not: the schemas share neither field names
    nor entry types, so it would fail in the confusing way (plausible-looking
    output built from the wrong keys) rather than the obvious one. Declaring
    ``format`` up front makes the difference machine-checkable on line 1, which is
    the honesty #1277 asks for — an export that says what it is beats one that
    pretends to be something a downstream tool already knows how to read.

    ``default=str`` makes this total. Everything here is normally JSON-native, but
    the caller assembles the header from ``getattr`` reads off a session record and
    the events come from whatever the replay adapter produced — so one unexpected
    value would otherwise raise ``TypeError`` AFTER a replay that is allowed to run
    for two minutes, turning a completed export into a 500. Stringifying the odd
    field degrades one value; raising loses the whole download.
    """
    header: dict[str, Any] = {
        "type": "ccswitchExportMeta",
        "format": NORMALIZED_EXPORT_FORMAT,
        "engine": engine,
        "sessionId": session_id,
        "note": ("Normalized ccswitch replay events — NOT a native Claude Code "
                 "transcript. See server/sessions/events.py for the event schema."),
    }
    if model:
        header["model"] = model
    lines = [json.dumps(header, ensure_ascii=False, default=str)]
    lines += [json.dumps(e, ensure_ascii=False, default=str) for e in events]
    return "\n".join(lines) + "\n"


def safe_filename(stem: str) -> str:
    keep = [c if c.isalnum() or c in "-_." else "-" for c in stem]
    return "".join(keep).strip("-") or "session"
