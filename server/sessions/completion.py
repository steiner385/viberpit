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
    "If the work is genuinely finished but NO pull request is appropriate — the "
    "objective is already satisfied without a code change, or the user has cancelled "
    "the work / you have deliberately decided not to open a PR — end your final message "
    f"with `{NO_PR_MARKER}` on its own line (optionally preceded by a short reason). "
    f"Use this INSTEAD of `{COMPLETION_MARKER}` when finishing without a PR; never emit "
    "it while a PR is still expected or you are still working. An objective that asks "
    "for a code change is not finished by investigating it: discovering that the work "
    "is larger than expected, recommending a spec or next step, or offering to "
    "continue is not a reason for this marker — it is a hand-back. State what you "
    "found, ask the user how to proceed, and emit no marker at all. The same "
    "no-question rule applies: never combine this marker with a question to the user."
)

_CANCEL_RE = re.compile(
    r"\b(cancel(?:led|ling)?|abandon(?:ed|ing)?|drop(?:ped|ping)?|"
    r"revert(?:ed|ing)?|won'?t\s+open|not\s+opening|no\s+longer)\b",
    re.IGNORECASE,
)


# Commit attribution for an agent-authored commit, per engine.
#
# THE source: this literal was copy-pasted into four seed prompts
# (backlog_prompt, sdlc/prompts, and twice in pm/classification), so a Codex
# automation would have instructed its session to co-author every commit as
# Claude. These are ccswitch's OWN attribution strings — the Claude line matches
# the trailer the Claude Code harness already asks for; the Codex line is a plain
# git trailer ccswitch chose, not a claim about an OpenAI convention.
_COMMIT_TRAILERS: dict[str, str] = {
    "claude_code": "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>",
    "codex": "Co-Authored-By: Codex <noreply@openai.com>",
}


def commit_trailer(engine: object = None) -> str:
    """The ``Co-Authored-By`` trailer a session spawned on *engine* should use.

    *engine* accepts an ``AgentEngine`` or its string value; anything unknown or
    absent gets the Claude trailer, which is the pre-Phase-7 behavior every seed
    prompt had hardcoded.
    """
    key = getattr(engine, "value", engine)
    return _COMMIT_TRAILERS.get(str(key), _COMMIT_TRAILERS["claude_code"])


def marker_instructions(*, self_model_switch: bool) -> str:
    """Compose every ccswitch marker instruction into one system-prompt append.

    Engine-neutral on purpose. The markers are a ccswitch contract, not a Claude
    one: a Codex thread is told about them through ``thread/start``'s
    ``developerInstructions`` exactly as a Claude session is told through the
    SDK's system-prompt append, and both halves of each marker (the instruction
    the agent reads and the literal the translator scans for) already live in
    this module. Composing the append here too means an engine added later
    cannot ship a session that is scanned for a marker it was never told about.

    ``self_model_switch`` is a parameter rather than a settings read because it
    is BOTH a deployment flag and an engine capability: the caller is the only
    one that knows whether this engine's driver can act on a model request at
    all (``AgentCapabilities.self_model_switch``). Asking an agent to emit a
    marker nothing will honour is worse than silence — it burns turns on a
    request that is dropped without explanation.
    """
    from .model_request import MODEL_REQUEST_INSTRUCTION

    parts = [COMPLETION_INSTRUCTION, NO_PR_INSTRUCTION]
    if self_model_switch:
        parts.append(MODEL_REQUEST_INSTRUCTION)
    return "\n\n".join(parts)


def strip_markers(text: str) -> str:
    """Remove the completion / no-PR / model-request marker from *text* so prose heuristics judge
    the real tail. The marker trails the message, so an unanswered closing question
    sits BEFORE it — without stripping, a trailing-"?" check can never fire on a
    completion turn."""
    if not text:
        return text
    from .model_request import MODEL_REQUEST_RE
    text = text.replace(COMPLETION_MARKER, "").replace(NO_PR_MARKER, "")
    return MODEL_REQUEST_RE.sub("", text).rstrip()


def resolution_from_text(text: str) -> str | None:
    """Classify a final-turn text carrying NO_PR_MARKER into a resolution token.

    Returns None when the marker is absent. When present, 'cancelled' if the text
    reads as an explicit cancellation/abandonment, else 'not-needed'."""
    if text is None or NO_PR_MARKER not in text:
        return None
    return "cancelled" if _CANCEL_RE.search(text) else "not-needed"
