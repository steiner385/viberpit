"""Synthesize a Codex session title from its own conversation — no LLM call.

Claude's fallback titler (``server/sessions/title_generator.py``) spends a Haiku
query under the session's own Claude account (``compose(account)`` builds that
account's ``CLAUDE_CONFIG_DIR``). A Codex account has no Claude credential slot
to compose — accounts are engine-scoped identities with at most one credential
authority (Principle II, ``.specify/memory/constitution.md``) — so routing a
Codex session's title through the same helper would mean spending Claude
credentials under a Codex account. Rather than do that, this picks a title
straight out of the session's own normalized prompts (see
``CodexThreadReplay.user_prompts`` — sourced from the supported ``thread/read``
RPC, never Codex's private rollout files).
"""
from __future__ import annotations

from server.sessions.title_generator import sanitize_title

# Mirrors generate_title()'s own selection window: Claude's fallback title
# summarizes the earliest prompts (the session's stated topic, not its most
# recent activity), so this walks prompts in the same order.
_MAX_PROMPTS = 30

# A prompt this short (or one of the exact acknowledgements below) reads as a
# bare "go ahead" rather than a description of the session's topic — skip it in
# favor of the next, more substantive prompt.
_MIN_SOURCE_CHARS = 12

_TRIVIAL_PROMPTS = frozenset({
    "ok", "okay", "yes", "no", "yep", "yup", "continue", "go", "go ahead",
    "please continue", "sure", "thanks", "thank you", "sounds good", "do it",
    "proceed", "looks good", "lgtm",
})


def _is_substantive(prompt: str) -> bool:
    stripped = prompt.strip()
    if len(stripped) < _MIN_SOURCE_CHARS:
        return False
    return stripped.strip(".!").lower() not in _TRIVIAL_PROMPTS


def codex_fallback_title(prompts: list[str]) -> str | None:
    """Return a title synthesized from *prompts*, or ``None`` if there are none.

    Never raises: a candidate that fails to sanitize (all-whitespace) is
    skipped in favor of the next one. An all-acknowledgement conversation still
    gets titled from its very first prompt rather than staying nameless.
    """
    if not prompts:
        return None
    for prompt in prompts[:_MAX_PROMPTS]:
        if not _is_substantive(prompt):
            continue
        title = sanitize_title(prompt)
        if title:
            return title
    return sanitize_title(prompts[0])
