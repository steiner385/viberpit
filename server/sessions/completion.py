"""Single source of truth for the session-completion marker.

The same literal is appended to every session's system prompt (so the agent
knows how to signal completion) and scanned for by the Translator (so the
dashboard can show 'Complete'). Keeping both in one module prevents drift.
"""
from __future__ import annotations
import re

COMPLETION_MARKER = "<!-- ccswitch:complete -->"

COMPLETION_INSTRUCTION = (
    "When you have fully accomplished the objective of this session, end your "
    f"final message with `{COMPLETION_MARKER}` on its own line. Emit it ONLY "
    "when the work is genuinely complete — never for an interim step, and never "
    "while you are still working or waiting on something. Never emit it in a "
    "message that asks the user a question: if you need an answer, ask and "
    "withhold the marker; phrase optional follow-ups as statements ('I can also "
    "do X if wanted'), not questions."
)

NO_PR_MARKER = "<!-- ccswitch:no-pr -->"

NO_PR_INSTRUCTION = (
    "If the work is genuinely finished but NO pull request is appropriate — either "
    "the task needed no code change, or you have deliberately decided to cancel / not "
    f"open a PR — end your final message with `{NO_PR_MARKER}` on its own line "
    "(optionally preceded by a short reason). Use this INSTEAD of "
    f"`{COMPLETION_MARKER}` when finishing without a PR; never emit it while a PR is "
    "still expected or you are still working. The same no-question rule applies: "
    "never combine this marker with a question to the user."
)

_CANCEL_RE = re.compile(
    r"\b(cancel(?:led|ling)?|abandon(?:ed|ing)?|drop(?:ped|ping)?|"
    r"revert(?:ed|ing)?|won'?t\s+open|not\s+opening|no\s+longer)\b",
    re.IGNORECASE,
)


def strip_markers(text: str) -> str:
    """Remove the completion / no-PR marker from *text* so prose heuristics judge
    the real tail. The marker trails the message, so an unanswered closing question
    sits BEFORE it — without stripping, a trailing-"?" check can never fire on a
    completion turn."""
    if not text:
        return text
    return text.replace(COMPLETION_MARKER, "").replace(NO_PR_MARKER, "").rstrip()


def resolution_from_text(text: str) -> str | None:
    """Classify a final-turn text carrying NO_PR_MARKER into a resolution token.

    Returns None when the marker is absent. When present, 'cancelled' if the text
    reads as an explicit cancellation/abandonment, else 'not-needed'."""
    if text is None or NO_PR_MARKER not in text:
        return None
    return "cancelled" if _CANCEL_RE.search(text) else "not-needed"
