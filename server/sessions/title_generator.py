"""Generate a short session title with a one-shot claude-agent-sdk query.

A title query is itself a Claude process bound to an account's
``CLAUDE_CONFIG_DIR`` (per-process isolation, Principle II) — it runs under the
session's own account and spends a negligible amount of that account's quota.
The model is Haiku (cheapest/fastest); the query is stateless, tool-free, and
one turn. Any failure degrades to ``None`` — titling is best-effort and must
never break the session loop.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Generate a concise 3-6 word title summarizing this work session, based on "
    "the user's messages. Output only the title — no quotes, no trailing "
    "punctuation, no preamble or explanation."
)

# Keep the request small: cap how many prompts and how much text we send.
_MAX_PROMPTS = 30
_MAX_PROMPT_CHARS = 4000
_MAX_TITLE_CHARS = 60


def _default_query_fn():
    from claude_agent_sdk import query  # imported lazily so tests need no SDK
    return query


def _sanitize(text: str) -> str | None:
    """Strip wrapping quotes, collapse whitespace, clamp length. ``None`` if empty."""
    cleaned = re.sub(r"\s+", " ", text).strip().strip("\"'").strip()
    if not cleaned:
        return None
    return cleaned[:_MAX_TITLE_CHARS].strip()


def _extract_text(messages) -> str:
    """Join the text of all assistant text blocks across yielded messages.

    Duck-typed: any message exposing a ``content`` list whose blocks have a
    string ``text`` attribute contributes (real ``AssistantMessage``/``TextBlock``
    and test fakes alike). Tool-result blocks and non-content messages are
    ignored.
    """
    out: list[str] = []
    for msg in messages:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                out.append(text)
    return "".join(out)


async def generate_title(
    user_prompts: list[str],
    *,
    config_dir: str,
    cwd: str,
    model: str = "haiku",
    query_fn=None,
) -> str | None:
    """Summarize *user_prompts* into a short title, or ``None`` on empty/failure.

    Runs a stateless, tool-free Haiku query under *config_dir*'s account. Never
    raises.
    """
    if not user_prompts:
        return None

    body = "\n".join(p.strip() for p in user_prompts[:_MAX_PROMPTS] if p.strip())
    if not body:
        return None
    body = body[:_MAX_PROMPT_CHARS]

    query_fn = query_fn or _default_query_fn()

    try:
        from claude_agent_sdk import ClaudeAgentOptions

        options = ClaudeAgentOptions(
            model=model,
            system_prompt=_SYSTEM_PROMPT,
            cwd=cwd,
            env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
            allowed_tools=[],
            max_turns=1,
            setting_sources=[],
        )
        messages = []
        async for msg in query_fn(prompt=body, options=options):
            messages.append(msg)
    except Exception:
        log.warning("generate_title failed for config_dir=%s", config_dir, exc_info=True)
        return None

    return _sanitize(_extract_text(messages))
