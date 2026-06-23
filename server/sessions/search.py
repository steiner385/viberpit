"""Pure text-extraction + matching helpers for session list search.

`transcript_text` reads a Claude transcript JSONL and returns ONLY the
human-readable text (user message text, assistant `text` blocks, `thinking`
blocks). Tool-call/result JSON, base64 images, and `isSidechain` entries are
excluded so a substring match means something real. No SQLAlchemy / SDK imports
— this stays cheap and trivially unit-testable.
"""
from __future__ import annotations

import json


def _blocks(content) -> list[dict]:
    # Claude `content` is either a plain string or a list of block dicts.
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def transcript_text(jsonl_path: str, *, max_bytes: int = 8 * 1024 * 1024) -> str:
    parts: list[str] = []
    try:
        with open(jsonl_path, errors="ignore") as f:
            read = 0
            for raw in f:
                read += len(raw)
                if read > max_bytes:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    o = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if o.get("isSidechain"):
                    continue
                typ = o.get("type")
                msg = o.get("message") or {}
                for b in _blocks(msg.get("content")):
                    bt = b.get("type")
                    if typ == "user" and bt == "text":
                        parts.append(b.get("text", ""))
                    elif typ == "assistant" and bt == "text":
                        parts.append(b.get("text", ""))
                    elif typ == "assistant" and bt == "thinking":
                        parts.append(b.get("thinking", ""))
    except OSError:
        return ""
    return "\n".join(parts)


def parse_terms(query: str) -> list[str]:
    return [t for t in query.lower().split() if t]


def session_matches(terms: list[str], name: str, text: str) -> bool:
    haystack = f"{name}\n{text}".lower()
    return all(t in haystack for t in terms)
