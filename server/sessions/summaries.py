from __future__ import annotations
import logging

from .translator import Translator
from . import events as ev

log = logging.getLogger(__name__)

# Terminal states whose transcript won't meaningfully change again — never spend a
# summary call on them. `limited` is included because its account is quota-exhausted,
# so a Haiku call is guaranteed-wasted. (Dormant active sessions are already filtered
# by the mtime-signature check, so this set is intentionally tiny.)
_SUMMARY_TERMINAL_STATUSES = {"ended", "archived", "limited"}
# Minimum seconds between regenerations for one session — the "once a minute" cap.
_SUMMARY_MIN_INTERVAL_S = 60.0

SUMMARY_SYSTEM_PROMPT = (
    "You summarize what a live coding session is currently doing, for a dashboard. "
    "Reply with ONE short present-tense phrase under 100 characters — no prose, no "
    "quotes, no code fences. Example: Refactoring the auth middleware and running tests."
)


def build_summary_prompt(context: str) -> str:
    return (
        "Here is the end of a transcript between a user and an AI coding agent:\n\n"
        f"{context}\n\n"
        "In one short present-tense phrase, summarize what the agent is currently "
        "working on. Reply with the phrase only."
    )


def clamp_summary(raw: str, limit: int = 100) -> str:
    """Normalize the model's reply to a single tidy line for the card."""
    text = " ".join(raw.split())
    text = text.strip("`").strip().strip('"').strip("'").strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def should_regenerate(
    viewing: bool,
    cached: tuple[float, float, str] | None,
    signature: float,
    now: float,
    status: str,
) -> bool:
    """Decide whether to (re)generate a session's summary.

    cached is the stored ``(transcript_signature, generated_at, text)`` or None.
    Regenerate only when a viewer is present, the session isn't terminal, the
    transcript changed since the cache, and at least _SUMMARY_MIN_INTERVAL_S has
    elapsed since the last generation. When no viewer is present, never regenerate
    (so an unwatched dashboard spends zero quota).
    """
    if not viewing:
        return False
    if status in _SUMMARY_TERMINAL_STATUSES:
        return False
    if cached is None:
        return True
    cached_sig, cached_at, _text = cached
    if cached_sig == signature:
        return False
    return (now - cached_at) >= _SUMMARY_MIN_INTERVAL_S


class SummaryService:
    """Generates a one-line status summary via a throwaway, resume-less SDK query.

    ``sdk_factory(cwd, config_dir, account) -> SDKClientProtocol`` builds a one-shot client
    bound to the session's own account config dir; ``account`` is used to also inject a
    setup-token env (see ``account_auth.oauth_env_for``) for accounts whose browser-OAuth
    refresh is dead. Best-effort: returns "" on empty context or any failure.
    """

    def __init__(self, sdk_factory):
        self._factory = sdk_factory

    async def generate(self, cwd: str, config_dir: str, context: str, account: str | None = None) -> str:
        if not context.strip():
            return ""
        try:
            client = self._factory(cwd, config_dir, account)
            tr = Translator()
            parts: list[str] = []
            async with client:
                await client.query(build_summary_prompt(context))
                async for msg in client.receive_response():
                    for e in tr.feed(msg):
                        if isinstance(e, ev.TextDelta):
                            parts.append(e.text)
            return clamp_summary("".join(parts))
        except Exception:
            log.warning("summary generation failed cwd=%s", cwd, exc_info=True)
            return ""
