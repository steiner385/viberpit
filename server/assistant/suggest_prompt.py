"""Frozen follow-up suggestion prompt for the assistant kind (spec v2 §10.3).

The coding suggester (``server/sessions/suggestions.py``) proposes imperative next
prompts for a coding agent ("Run the tests"). The drawer's conversation is Q&A about
the fleet, so its chips are the operator's likely next QUESTIONS instead. Same
one-shot helper path and parser; only the wording and the cap differ.
"""
from __future__ import annotations

ASSISTANT_SUGGEST_LIMIT = 3

ASSISTANT_SUGGEST_SYSTEM_PROMPT = (
    "Given this Q&A about a fleet of coding sessions, propose up to 3 short follow-up "
    "questions the operator would ask next. Reply with ONLY a JSON array of strings "
    "(each under 60 characters), no prose, no code fences. "
    'Example: ["Which one has waited longest?", "What is blocking it?"]'
)


def build_assistant_suggest_prompt(context: str) -> str:
    return (
        "Here is the latest exchange between an operator and the ViberPit assistant:\n\n"
        f"{context}\n\n"
        "Propose up to 3 short follow-up questions the operator would ask next. Reply "
        "with a JSON array of strings only."
    )
