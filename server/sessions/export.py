"""Transcript export formatters (Task 4).

transcript_markdown() — render redacted wire-event dicts as a human-readable
Markdown document, suitable for the /export.md download endpoint.

safe_filename() — sanitise a string into a filesystem-safe filename stem.
"""
from __future__ import annotations

import json
from typing import Any


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


def safe_filename(stem: str) -> str:
    keep = [c if c.isalnum() or c in "-_." else "-" for c in stem]
    return "".join(keep).strip("-") or "session"
