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
    "message that asks the user a question: if you need an answer, ask and emit "
    f'the hand-back marker (`<!-- {brand.SLUG}:hand-back reason="…" -->`) instead; '
    "phrase optional follow-ups as statements ('I can also do X if wanted'), not "
    "questions."
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
    f"found, ask the user how to proceed, and emit the hand-back marker (`<!-- {brand.SLUG}"
    f':hand-back reason="…" -->`), never a finishing marker. The same no-question rule '
    "applies: never combine this marker with a question to the user."
)

# The explicit "it's your turn" signal (spec 2026-09-08-hand-back-signal-tool). The
# emitted form carries a reason attribute, so the regex is hand-built like
# MODEL_REQUEST_RE rather than brand.marker_re(): either slug, optional reason, and a
# reason group that cannot swallow the closing '-->'.
HAND_BACK_MARKER = brand.marker("hand-back")
HAND_BACK_MARKER_RE = re.compile(
    rf'<!--\s*{brand.EITHER_SLUG_RE}:hand-back(?:\s+reason="([^"]*)")?\s*-->'
)

#: Longest reason kept. Longer is truncated, never rejected — a verbose hand-back is
#: still a hand-back, and refusing it would strand the session at the calm `done`.
HAND_BACK_REASON_MAX = 300

_THREE_WAYS_MARKER = (
    f"Every turn ends ONE of three ways: `{COMPLETION_MARKER}` (the objective is fully "
    "done and verified; the session will be treated as finished and may be archived "
    f"without anyone reading further), `{NO_PR_MARKER}` (finished, but no pull request "
    f'is appropriate), or `<!-- {brand.SLUG}:hand-back reason="…" -->` (you need the '
    "user). Ending a message with a question and no marker is NOT a hand-back: the "
    "session reads as calmly idle and nobody is alerted."
)

HAND_BACK_INSTRUCTION = (
    "If you need something from the user before this session's objective can be "
    "finished — a decision, a review, an approval, or information you do not have — "
    f'end the message that asks with `<!-- {brand.SLUG}:hand-back reason="<one sentence: '
    'what you need>" -->` on its own line. '
    f"{_THREE_WAYS_MARKER} Never combine the hand-back marker with either finishing marker."
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
# Claude. These are ViberPit's OWN attribution strings — the Claude line matches
# the trailer the Claude Code harness already asks for; the Codex line is a plain
# git trailer ViberPit chose, not a claim about an OpenAI convention.
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

    parts = [COMPLETION_INSTRUCTION, NO_PR_INSTRUCTION, HAND_BACK_INSTRUCTION]
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
    for pattern in (COMPLETION_MARKER_RE, NO_PR_MARKER_RE, HAND_BACK_MARKER_RE, MODEL_REQUEST_RE):
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


def parse_hand_back(args: dict | None) -> dict:
    """Validate ``hand_back`` arguments into ``{"reason"}``.

    THE parser for both halves of the tool, exactly as ``parse_mark_no_pr`` is: the
    handler rejects a bad argument locally (``ToolArgError`` → corrective message, no
    side effect) and the Translator parses the observed tool-use block. A blank reason
    is rejected — the reason is the whole point of the argument — but a long one is
    truncated, because refusing a verbose hand-back would leave the session at the
    calm ``done`` it was trying to escape.
    """
    from server.mcp_spec import ToolArgError
    reason = (args or {}).get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ToolArgError(
            "reason must be one non-empty sentence naming exactly what you need from the "
            "user — a decision, a review, an approval, or information you do not have.")
    return {"reason": reason.strip()[:HAND_BACK_REASON_MAX]}


def parse_hand_back_marker(text: str | None) -> dict | None:
    """Extract the LAST hand-back marker from *text*, or None when absent.

    ``{"reason": str | None}`` — the reason attribute is optional on the wire (a model
    that forgets it still meant the hand-back). Last-wins, like ``parse_model_request``.
    Never raises; any non-str input reads as no marker.
    """
    if not isinstance(text, str) or not text:
        return None
    matches = list(HAND_BACK_MARKER_RE.finditer(text))
    if not matches:
        return None
    raw = matches[-1].group(1)
    reason = raw.strip()[:HAND_BACK_REASON_MAX] if isinstance(raw, str) else ""
    return {"reason": reason or None}
