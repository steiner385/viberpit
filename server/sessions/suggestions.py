from __future__ import annotations
import json
import logging

log = logging.getLogger(__name__)

SUGGEST_SYSTEM_PROMPT = (
    "You suggest what the user might want to say NEXT in a live coding session with an "
    "AI agent. Reply with ONLY a JSON array of 2-4 short, imperative prompts (each under "
    '60 characters), no prose, no code fences. Example: ["Run the tests", "Commit it"]'
)


def build_prompt(context: str) -> str:
    return (
        "Here is the end of a transcript between a user and an AI coding agent:\n\n"
        f"{context}\n\n"
        "Suggest 2-4 short next prompts the user might send. Reply with a JSON array of "
        "strings only."
    )


def parse_suggestions(raw: str, limit: int = 4) -> list[str]:
    raw = raw.strip()
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out = [s.strip() for s in data if isinstance(s, str) and s.strip()]
    return out[:limit]
