"""Prompt refinement helper: one-shot Haiku call that returns an improved
version of the user's draft dispatch prompt.

Fail-open: returns None on any error (timeout, model failure, etc.).
The caller (POST /api/dispatch/assess) shows the refinement only when non-None.
"""
from __future__ import annotations

import logging
import os

from server.sessions.helper_call import pick_helper_config_dir, run_helper_call

log = logging.getLogger("ccswitch.dispatch")

REFINE_TIMEOUT_S: float = float(os.environ.get("CCSWITCH_REFINE_PROMPT_TIMEOUT_S", "20"))

_REFINE_SYSTEM = (
    "You are a prompt engineer for a coding-agent fleet. "
    "A user is about to start a Claude coding session. "
    "They have typed a draft prompt. Rewrite it into a single, tight, "
    "unambiguous task description that will get the best result from a "
    "Claude coding agent:\n"
    "- Preserve the user's intent exactly — do not add features or change scope.\n"
    "- Make success criteria explicit (e.g. 'tests pass', 'no TypeScript errors').\n"
    "- Keep it concise — one short paragraph at most.\n"
    "- Reply with ONLY the improved prompt text, nothing else — no commentary, "
    "no preamble, no trailing explanation."
)


def _default_sdk_query_fn():
    from claude_agent_sdk import query  # lazy import — not available in tests
    return query


async def refine_prompt(
    text: str,
    *,
    _query_fn=None,
    _config_dir: str | None = None,
    _cwd: str | None = None,
    _timeout: float | None = None,
) -> str | None:
    """One-shot Haiku query that returns a refined version of *text*, or None.

    Mirrors ``assess_model``: tool-less one-shot query with an injected
    ``_query_fn`` seam so tests never call the real model. Returns the refined
    prompt string, or ``None`` on ANY failure (exception, timeout, empty reply)
    so the caller degrades gracefully.
    """
    if not text or not text.strip():
        return None

    timeout = _timeout if _timeout is not None else REFINE_TIMEOUT_S
    config_dir = _config_dir or pick_helper_config_dir() or os.environ.get("CLAUDE_CONFIG_DIR", "")
    cwd = _cwd or os.getcwd()

    try:
        # SDK resolution stays INSIDE the try: an unimportable SDK must degrade
        # to None, never raise to the caller.
        query_fn = _query_fn or _default_sdk_query_fn()
        from claude_agent_sdk import ClaudeAgentOptions

        options = ClaudeAgentOptions(
            model="haiku",
            system_prompt=_REFINE_SYSTEM,
            cwd=cwd,
            env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
            allowed_tools=[],
            setting_sources=[],
        )
    except Exception as exc:
        log.warning("refine_prompt: setup failed: %s", exc)
        return None

    body = text.strip()

    outcome = await run_helper_call(
        lambda: query_fn(prompt=body, options=options),
        subsystem="refine_prompt",
        account_key=config_dir,
        per_attempt_timeout=timeout,
        retries=0,
    )
    if outcome.skipped:
        return None
    if outcome.error is not None:
        if outcome.failure_kind == "timeout":
            log.warning("refine_prompt: exceeded %.0fs — no refinement", timeout)
        else:
            log.warning("refine_prompt: model call failed: %s", outcome.error)
        return None

    text_parts: list[str] = []
    for msg in outcome.messages:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            t = getattr(block, "text", None)
            if isinstance(t, str):
                text_parts.append(t)
    refined = "".join(text_parts).strip()
    return refined if refined else None
