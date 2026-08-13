"""One-shot LLM inference of a session's goal statement.

Mirrors title_generator.generate_title / autopilot_decider.decide: a stateless,
tool-free Haiku query with an injected ``query_fn`` so tests never call the real
model, returning ``None`` on any failure so callers keep the prior value (never
clobber a good goal with a failure). The result feeds Session.inferred_goal — the
middle tier of effective_goal()."""
from __future__ import annotations

import logging
import os

from .helper_call import run_helper_call

log = logging.getLogger(__name__)

_MAX_TAIL_ENTRIES = 12
_MAX_GOAL_CHARS = 200

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
    tail_lines = "\n".join(
        f"[{e.get('role', '?')}] {e.get('text', '')}"
        for e in transcript_tail[-_MAX_TAIL_ENTRIES:]
    )
    body = (f"Session title: {name}\n\nTranscript excerpt:\n---\n{tail_lines}\n---\n\n"
            f"{_INSTRUCTION}")
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
                env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
                allowed_tools=[], setting_sources=[])
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
    goal = " ".join("".join(parts).split()).strip().strip('"')
    if not goal:
        return None
    return goal[:_MAX_GOAL_CHARS]
