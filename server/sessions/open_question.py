"""Gated Haiku classifier: does a completed session's final message still await
the user's answer?

Sessions sometimes emit the completion marker in the same message that poses a
question ("Want me to take on Phase 2 next, or pause here? <marker>") — prod
auto-archived two such sessions on 2026-08-19, burying the questions. The status
pipeline already outranks ``complete`` with *structured* pending questions
(``derive_status``); this module covers the prose case at the one decision point
where it matters: the completion auto-archive gate.

Mirrors ``automations.pr_intent`` — injected ``query_fn`` so the real model is
NEVER called in tests; routed through ``helper_call.run_helper_call`` (backoff +
per-account breaker). Degrades to the shared prose heuristic
``background_tasks.text_awaits_user`` whenever the helper lane is unavailable
(breaker open, no usable account, SDK/model failure, unparseable output) — the
heuristic is deliberately broad, so during a helper outage a polite closing may
linger as "Answer needed" rather than a real question being archived away.
"""
from __future__ import annotations

import json
import logging
import os

from .account_auth import helper_env
from .claude_bin import cli_path_kwargs
from .helper_call import pick_helper_config_dir, run_helper_call

log = logging.getLogger(__name__)

_SYSTEM = (
    "You judge whether a coding assistant's FINAL message leaves an open question "
    "that awaits the human user's answer. Open: direct questions and decision "
    "hand-backs ('which option do you want?', 'want me to do X next, or pause?') — "
    "even when phrased as an optional offer, the user is being asked to reply. NOT "
    "open: polite closings with no question ('let me know if anything looks off'), "
    "rhetorical or already-answered questions, and plain statements of completion. "
    'Reply with ONLY a JSON object {"open_question": bool} — no prose, no code '
    "fences."
)

# Only the closing of the message decides — a completion summary can be long, and
# the ask (if any) sits at the end. Matches the heuristic's tail-scan philosophy.
_TAIL_CHARS = 2000


def _extract_verdict(raw: str) -> bool | None:
    """First JSON object in *raw* carrying an ``open_question`` key, else None."""
    dec = json.JSONDecoder()
    idx = 0
    while True:
        start = raw.find("{", idx)
        if start == -1:
            return None
        try:
            obj, _end = dec.raw_decode(raw, start)
        except ValueError:
            idx = start + 1
            continue
        if isinstance(obj, dict) and "open_question" in obj:
            return bool(obj["open_question"])
        idx = start + 1


def _build_prompt(text: str) -> str:
    return (
        "Assistant's final message (completion markers already removed):\n---\n"
        f"{text[-_TAIL_CHARS:]}\n---\n\n"
        "Does this message leave an open question awaiting the user's answer? "
        'Reply with ONLY the JSON object {"open_question": bool}.'
    )


async def completion_awaits_user(
    text: str, *, query_fn=None, config_dir: str | None = None,
    cwd: str | None = None,
) -> bool:
    """True when *text* (a marker-stripped final assistant message) still awaits
    the user. LLM verdict when the helper lane is usable; the shared prose
    heuristic otherwise. Never raises."""
    from .background_tasks import text_awaits_user
    if not (text or "").strip():
        return False
    try:
        # Lazy options: construct SDK options ONLY on the real-model path, so an
        # injected query_fn (tests) never touches the SDK (pr_intent pattern).
        options = None
        if query_fn is None:
            cfg = config_dir if config_dir is not None else (
                os.environ.get("CLAUDE_CONFIG_DIR") or pick_helper_config_dir())
            if not cfg:
                return text_awaits_user(text)  # no usable account — degrade
            config_dir = cfg
            from claude_agent_sdk import ClaudeAgentOptions, query
            query_fn = query
            options = ClaudeAgentOptions(
                model="haiku", system_prompt=_SYSTEM, cwd=cwd or os.getcwd(),
                env=helper_env(config_dir),
                allowed_tools=[], setting_sources=[], **cli_path_kwargs())
    except Exception as exc:
        log.warning("open_question.classify unavailable: %s", exc)
        return text_awaits_user(text)

    outcome = await run_helper_call(
        lambda: query_fn(prompt=_build_prompt(text), options=options),
        subsystem="open_question.classify", account_key=config_dir or "")
    if outcome.skipped:
        return text_awaits_user(text)  # lane cooled-down — degrade, no call made
    if outcome.error is not None:
        log.warning("open_question.classify failed: %s", outcome.error)
        return text_awaits_user(text)
    parts: list[str] = []
    for msg in outcome.messages:
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            for block in content:
                t = getattr(block, "text", None)
                if isinstance(t, str):
                    parts.append(t)
    verdict = _extract_verdict("".join(parts).strip())
    if verdict is None:
        log.warning("open_question.classify unparseable output — using heuristic")
        return text_awaits_user(text)
    return verdict
