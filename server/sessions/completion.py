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

#: What "finished" means when the last step is a machine's, not the session's. Shared
#: verbatim by the completion marker instruction and the ``mark_complete`` tool
#: description (``tools/tools.py``) so the two worlds cannot disagree. Prod 2026-09-25
#: (session 2971d6a6): one PR merged, the follow-up ready with auto-merge armed and CI
#: still running, and the session ended with a hand-back whose reason began "Status
#: only, no action needed". The completion bar (``completion_bar.met_bar``) already
#: reads an armed PR with no red required check as met, but the instruction said "never
#: while … waiting on something", so a session waiting on CI had no finishing signal it
#: was allowed to use and fell through to the only ending left.
FINISHED_STATE_CLAUSE = (
    "A pull request that is open, marked ready, with auto-merge armed and no failing "
    "required check IS the finished state: do not wait for CI or for the merge itself, "
    "and do not hand back a status report about it."
)

#: The other half of the same incident, shared by the hand-back marker instruction and
#: the ``hand_back`` tool description: a hand-back is an ASK, never a progress report.
STATUS_ONLY_CLAUSE = (
    "A hand-back must ask for something. 'Status only, nothing needed from you' is not "
    "a hand-back: if the objective is met, finish with the completion signal; if it is "
    "not, keep working."
)

COMPLETION_INSTRUCTION = (
    "When you have fully accomplished the objective of this session, end your "
    f"final message with `{COMPLETION_MARKER}` on its own line. Emit it ONLY "
    "when the work is genuinely complete — never for an interim step, and never "
    f"while you are still working. {FINISHED_STATE_CLAUSE} Never emit it in a "
    "message that asks the user a question: if you need an answer, ask and emit "
    f'the hand-back marker (`<!-- {brand.SLUG}:hand-back reason="…" -->`) instead; '
    "phrase optional follow-ups as statements ('I can also do X if wanted'), not "
    "questions."
)

NO_PR_MARKER = brand.marker("no-pr")
NO_PR_MARKER_RE = brand.marker_re("no-pr")

#: "I proposed a decision and the item is parked on it" (spec 2026-09-18 §6) — a
#: token rather than a bare literal because three halves must agree on it: the
#: ``mark_no_pr`` tool enum, the marker contract below, and the Backlog Runner
#: branch that reads ``Session.pr_resolution`` back and settles such an ending as
#: neither a bounce nor a hand-off.
AWAITING_DECISION = "awaiting-decision"

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
    "applies: never combine this marker with a question to the user. "
    "One exception to 'genuinely finished': if you have PROPOSED A DECISION on this "
    f"issue and it is now parked on the person's answer, write the exact word "
    f"`{AWAITING_DECISION}` in that final message before the marker. Only that exact "
    "word counts — it is what records the work as parked on a question rather than "
    "finished, and it is not a hand-back: the proposal already carries the question."
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
    f"{_THREE_WAYS_MARKER} {STATUS_ONLY_CLAUSE} Never combine the hand-back marker with "
    "either finishing marker."
)

_CANCEL_RE = re.compile(
    r"\b(?:cancel(?:led|ling)?|abandon(?:ed|ing)?|won'?t\s+open|not\s+opening|no\s+longer)\b|"
    r"\b(?:drop(?:ped|ping)?|revert(?:ed|ing)?)\s+(?:it\b|(?:the\s+|this\s+|that\s+|my\s+)?"
    r"(?:PR|pull\s+request|change|branch|work|task|issue|effort)\b)",
    re.IGNORECASE,
)
# "drop"/"revert" are ordinary verbs about other things ("the Prisma model drop", #1011), so
# they only count as cancellation when their object is the PR/work itself.

#: The DECLARED form of ``AWAITING_DECISION`` on the marker path (spec 2026-09-18 §6,
#: review R1 F2): the exact hyphenated token and nothing else.
#:
#: An engine without the durable tool surface (Codex) has no ``mark_no_pr`` argument
#: to declare a resolution in, so ``NO_PR_INSTRUCTION`` asks it to write the token
#: itself — the one thing on this path that is a DECLARATION rather than a guess,
#: which is why it outranks the cancellation heuristic below.
#:
#: Deliberately NOT a prose pattern. A first cut matched "awaiting a decision"
#: anywhere in the turn, which is ordinary English in a cancelled or handed-back
#: ending ("dropping this — awaiting a decision from you on X") and silently
#: re-labelled those endings as a decision park: the Backlog Runner's hold check
#: catches the consequence, but ``Session.pr_resolution`` is also read by the UI and
#: by humans, and the ``no_pr_resolution_disagreement`` log never fires on a
#: pure-marker turn. Word-bounded on both sides so the token is the whole word,
#: quoted or bare, and built from the constant so the text the instruction asks for
#: and the text this matches cannot drift.
_AWAITING_DECISION_RE = re.compile(rf"\b{re.escape(AWAITING_DECISION)}\b", re.IGNORECASE)


# Commit attribution for an agent-authored commit, per engine.
#
# THE source: this literal was copy-pasted into four seed prompts
# (backlog_prompt, sdlc/prompts, and twice in pm/classification), so a Codex
# automation would have instructed its session to co-author every commit as
# Claude. These are ViberPit's OWN attribution strings — the Claude line matches
# the trailer the Claude Code harness already asks for; the Codex line is a plain
# git trailer ViberPit chose, not a claim about an OpenAI convention.
#
# The Claude line names no model VERSION, and must not regain one. It read
# "Claude Opus 4.8" until Opus 5 shipped, after which every automation-seeded
# commit was misattributed — the drift is silent because nothing downstream
# validates the string. No builder can fix that by looking the version up:
# seed prompts are composed BEFORE the session launches, and the only model
# ViberPit persists is a tier alias ("opus", `Session.model`) which carries no
# version at all. The resolved id (`claude-opus-5`) exists solely as a live
# event — the SDK's `init` message and every `AssistantMessage.model`, which
# `sessions/translator.py` turns into SessionMeta/TurnModel for the UI and
# never stores. So the trailer delegates to the one party that always knows:
# the running session substitutes its own model. `AGENTS.md` states the same
# rule for humans and agents reading the repo directly.
_COMMIT_TRAILERS: dict[str, str] = {
    "claude_code": "Co-Authored-By: Claude <your model> <noreply@anthropic.com>",
    "codex": "Co-Authored-By: Codex <noreply@openai.com>",
}


def commit_trailer(engine: object = None) -> str:
    """The ``Co-Authored-By`` trailer a session spawned on *engine* should use.

    *engine* accepts an ``AgentEngine`` or its string value; anything unknown or
    absent gets the Claude trailer, which is the pre-Phase-7 behavior every seed
    prompt had hardcoded.

    The Claude trailer is a TEMPLATE, not a literal: ``<your model>`` is a
    placeholder the session replaces with the model it is actually running on.
    Callers interpolate this inside backticks after "End the commit message
    with:", where a placeholder in angle brackets reads as one. Do not
    substitute a version here — see the note above ``_COMMIT_TRAILERS``.
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

    Returns None when the marker is absent. When present, ``awaiting-decision`` if
    the text carries that exact token — the one DECLARATION available on this path
    (``NO_PR_INSTRUCTION`` asks for it by name), so it outranks the two heuristics
    below exactly as a tool call outranks a marker in the Translator; then
    'cancelled' if the text reads as an explicit cancellation/abandonment, else
    'not-needed'.

    Prose about awaiting someone's decision is NOT the token (review R1 F2): it is
    ordinary English in a cancelled or handed-back ending, and reading it as a
    decision park would mislabel those endings silently."""
    if not text or NO_PR_MARKER_RE.search(text) is None:
        return None
    if _AWAITING_DECISION_RE.search(text):
        return AWAITING_DECISION
    return "cancelled" if _CANCEL_RE.search(text) else "not-needed"


#: The resolution tokens a no-PR completion can carry. Identical to what
#: ``resolution_from_text`` answers, so the tool path and the marker fallback
#: produce values that are comparable rather than merely similar.
NO_PR_RESOLUTIONS: tuple[str, ...] = ("not-needed", "cancelled", AWAITING_DECISION)


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
            "change, 'cancelled' when the work was called off, "
            f"'{AWAITING_DECISION}' when you called propose_decision and the item "
            "is parked on the person's answer.")
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
