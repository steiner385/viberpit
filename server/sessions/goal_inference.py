"""One-shot LLM inference of a session's goal statement.

Mirrors title_generator.generate_title / autopilot_decider.decide: a stateless,
tool-free Haiku query with an injected ``query_fn`` so tests never call the real
model, returning ``None`` on any failure so callers keep the prior value (never
clobber a good goal with a failure). The result feeds Session.inferred_goal — the
middle tier of effective_goal()."""
from __future__ import annotations

import logging
import os

from .account_auth import helper_env
from .claude_bin import cli_path_kwargs
from .helper_call import run_helper_call

log = logging.getLogger(__name__)

_MAX_TAIL_ENTRIES = 12
_MAX_GOAL_CHARS = 200

# Mirrors prompt_refiner.REFINE_TIMEOUT_S — bounds the native-Codex one-shot turn
# (SessionOrchestrator.infer_goal_codex) so a wedged thread cannot hold the goal
# sweep open forever.
GOAL_INFER_TIMEOUT_S: float = float(os.environ.get("CCSWITCH_GOAL_INFER_TIMEOUT_S", "20"))

_SYSTEM_PROMPT = (
    "You distill coding sessions into goal statements. Given a session's title and a "
    "transcript excerpt, state the session's TERMINAL end state — the fully shipped "
    "outcome the work drives toward: the feature implemented and verified with a pull "
    "request opened and armed to merge. Intermediate steps — writing a design doc, "
    "spec, or plan, investigating, or recommending an approach — are means to that end, "
    "NOT the goal; project past them to the shipped result. State the destination, not "
    "the current step. Only stop short of a merged PR if the session's title/initial "
    "request explicitly scopes the work to just that artifact (e.g. 'write a design "
    "doc, do not implement')."
)

_INSTRUCTION = (
    "Reply with ONLY one sentence (under 120 characters) stating the session's terminal "
    "shipped outcome (implemented + PR armed to merge), unless the title explicitly "
    "scopes it narrower. Imperative mood. No preamble, no quotes."
)


def _default_query_fn():
    from claude_agent_sdk import query  # lazy — not available in tests
    return query


def _format_tail_body(name: str, transcript_tail: list[dict]) -> str:
    """Compose the session-title + transcript-excerpt body shared by both providers."""
    tail_lines = "\n".join(
        f"[{e.get('role', '?')}] {e.get('text', '')}"
        for e in transcript_tail[-_MAX_TAIL_ENTRIES:]
    )
    return (f"Session title: {name}\n\nTranscript excerpt:\n---\n{tail_lines}\n---\n\n"
            f"{_INSTRUCTION}")


def normalize_goal_text(text: str | None) -> str | None:
    """Collapse whitespace, strip wrapping quotes, and cap length — or ``None`` if empty.

    The single normalization both providers funnel their raw reply through, so a
    Codex-inferred goal (``SessionOrchestrator.infer_goal_codex``) reads exactly like
    a Claude-inferred one wherever ``Session.inferred_goal`` is displayed.
    """
    if not text:
        return None
    goal = " ".join(text.split()).strip().strip('"')
    return goal[:_MAX_GOAL_CHARS] if goal else None


def build_codex_goal_prompt(name: str, transcript_tail: list[dict]) -> str:
    """Compose the one-shot Codex turn text for goal inference.

    Codex threads have no separate system-prompt slot a one-shot caller can set
    (``CodexAgentDriver.start()`` bakes its own fixed developer instructions) — see
    ``server.sessions.orchestrator.SessionOrchestrator.infer_goal_codex`` — so
    ``_SYSTEM_PROMPT`` is folded into the turn text instead, exactly like
    ``prompt_refiner.build_codex_refine_prompt`` does for prompt refinement.
    """
    return f"{_SYSTEM_PROMPT}\n\n{_format_tail_body(name, transcript_tail)}"


async def infer_goal(
    name: str,
    transcript_tail: list[dict],
    *,
    config_dir: str,
    cwd: str,
    model: str = "haiku",
    query_fn=None,
) -> str | None:
    """Infer a one-line goal from the session name + a compact transcript tail.

    Returns the goal text, or ``None`` on empty output or any failure. Never raises.
    """
    body = _format_tail_body(name, transcript_tail)
    try:
        # Lazy options: construct SDK options ONLY on the real-model path, so an
        # injected query_fn (tests) never touches the SDK (same pattern as
        # autopilot_decider.decide).
        options = None
        if query_fn is None:
            from claude_agent_sdk import ClaudeAgentOptions
            query_fn = _default_query_fn()
            options = ClaudeAgentOptions(
                model=model, system_prompt=_SYSTEM_PROMPT, cwd=cwd,
                env=helper_env(config_dir),
                allowed_tools=[], setting_sources=[], **cli_path_kwargs())
    except Exception as exc:
        log.warning("infer_goal failed: %s", exc)
        return None

    outcome = await run_helper_call(
        lambda: query_fn(prompt=body, options=options),
        subsystem="infer_goal", account_key=config_dir)
    if outcome.skipped:
        return None  # account's inference lane cooled-down — keep the prior goal
    if outcome.error is not None:
        log.warning("infer_goal failed: %s", outcome.error)
        return None
    parts: list[str] = []
    for msg in outcome.messages:
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            for block in content:
                t = getattr(block, "text", None)
                if isinstance(t, str):
                    parts.append(t)
    return normalize_goal_text("".join(parts))
