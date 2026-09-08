"""Single source of truth for the session-completion marker.

The same literal is appended to every session's system prompt (so the agent
knows how to signal completion) and scanned for by the Translator (so the
dashboard can show 'Complete'). Keeping both in one module prevents drift.

Emit one spelling, accept two: the ``_RE`` matchers also read the pre-rebrand
``<!-- ccswitch:… -->`` form, permanently. Stored transcripts are re-parsed on
replay and a session that was mid-flight across the flip already wrote the old
form, so a parser that stopped accepting it would strand those sessions in
'working' forever. This is not Phase-7 scaffolding — it never comes out.
"""
from __future__ import annotations
import re

from ccswitch import brand

COMPLETION_MARKER = brand.marker("complete")
COMPLETION_MARKER_RE = brand.marker_re("complete")

COMPLETION_INSTRUCTION = (
    "When you have fully accomplished the objective of this session, end your "
    f"final message with `{COMPLETION_MARKER}` on its own line. Emit it ONLY "
    "when the work is genuinely complete — never for an interim step, and never "
    "while you are still working or waiting on something. Never emit it in a "
    "message that asks the user a question: if you need an answer, ask and "
    "withhold the marker; phrase optional follow-ups as statements ('I can also "
    "do X if wanted'), not questions."
)

NO_PR_MARKER = brand.marker("no-pr")
NO_PR_MARKER_RE = brand.marker_re("no-pr")

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


def has_completion_marker(text: str | None) -> bool:
    """Whether *text* carries the completion marker in EITHER brand spelling.

    Callers used to write ``COMPLETION_MARKER in text``; that substring test is
    what would have silently stopped matching pre-rebrand transcripts, so the
    membership check lives here instead of at each translator.
    """
    return bool(text) and COMPLETION_MARKER_RE.search(text) is not None


def marker_instructions(*, self_model_switch: bool) -> str:
    """Compose every marker instruction into one system-prompt append.

    Engine-neutral on purpose. The markers are OUR contract, not a Claude
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
    for pattern in (COMPLETION_MARKER_RE, NO_PR_MARKER_RE, MODEL_REQUEST_RE):
        text = pattern.sub("", text)
    return text.rstrip()


def resolution_from_text(text: str) -> str | None:
    """Classify a final-turn text carrying NO_PR_MARKER into a resolution token.

    Returns None when the marker is absent. When present, 'cancelled' if the text
    reads as an explicit cancellation/abandonment, else 'not-needed'."""
    if not text or NO_PR_MARKER_RE.search(text) is None:
        return None
    return "cancelled" if _CANCEL_RE.search(text) else "not-needed"


#: The two resolution tokens a no-PR completion can carry. Identical to what
#: ``resolution_from_text`` infers from prose, so the tool path and the marker
#: fallback produce values that are comparable rather than merely similar.
NO_PR_RESOLUTIONS: tuple[str, ...] = ("not-needed", "cancelled")


def parse_mark_no_pr(args: dict) -> dict:
    """Validate ``mark_no_pr`` arguments into ``{"resolution", "note"}``.

    THE parser for both halves of the tool: the handler calls it to reject a bad
    argument locally (``ToolArgError`` → a corrective message to the model, no side
    effect), and the Translator calls it on the observed tool-use block. A second
    copy on either side is how the model's acknowledgement and the harness's
    recorded resolution would come to disagree.
    """
    from server.mcp_spec import ToolArgError
    resolution = (args or {}).get("resolution")
    if not isinstance(resolution, str) or resolution not in NO_PR_RESOLUTIONS:
        raise ToolArgError(
            f"resolution must be one of {', '.join(NO_PR_RESOLUTIONS)} — "
            "'not-needed' when the objective is already satisfied without a code "
            "change, 'cancelled' when the work was called off.")
    note = (args or {}).get("note")
    return {"resolution": resolution, "note": note if isinstance(note, str) and note else None}
