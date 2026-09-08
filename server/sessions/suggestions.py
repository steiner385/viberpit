from __future__ import annotations
import json
import logging

from .translator import Translator
from .sdk_contract import one_turn
from . import events as ev

log = logging.getLogger(__name__)

SUGGEST_SYSTEM_PROMPT = (
    "You suggest what the user might want to say NEXT in a live coding session with an "
    "AI agent. Reply with ONLY a JSON array of 2-4 short, imperative prompts (each under "
    '60 characters), no prose, no code fences. Example: ["Run the tests", "Commit it"]'
)


def build_prompt(context: str) -> str:
    return (
        "Here is the end of a transcript between a user and an AI coding agent:\n\n"
        f"{context}\n\n"
        "Suggest 2-4 short next prompts the user might send. Reply with a JSON array of "
        "strings only."
    )


def build_codex_suggest_prompt(context: str) -> str:
    """Compose the one-shot Codex turn text for suggestion generation (#1281).

    Codex threads have no separate system-prompt slot a one-shot caller can set
    (see ``prompt_refiner.build_codex_refine_prompt`` for the same constraint on
    the refine helper), so ``SUGGEST_SYSTEM_PROMPT`` and the transcript context
    are folded into a single turn instead of split across a system/user pair.
    """
    return f"{SUGGEST_SYSTEM_PROMPT}\n\n{build_prompt(context)}"


def parse_suggestions(raw: str, limit: int = 4) -> list[str]:
    raw = raw.strip()
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out = [s.strip() for s in data if isinstance(s, str) and s.strip()]
    return out[:limit]


def last_turn_context(events: list) -> tuple[str, str]:
    """Extract (user_text, assistant_text) for the latest turn from replayed events.

    user_text is the most-recent UserMessage's text; assistant_text concatenates the
    TextDelta texts that follow it. Returns ("", "") when there's no user message.
    Used to seed suggestion generation for a session resumed into an idle state,
    whose turn context is not in the driver's memory.
    """
    last_user = -1
    for i, e in enumerate(events):
        if isinstance(e, ev.UserMessage):
            last_user = i
    if last_user == -1:
        return "", ""
    user_text = events[last_user].text or ""
    parts = [e.text for e in events[last_user + 1:] if isinstance(e, ev.TextDelta)]
    return user_text, "".join(parts)


def kick_idle_suggestions(driver, events: list) -> None:
    """Trigger one idle-session suggestion generation from replayed transcript events.

    Glue for the WS attach path: extract the latest-turn context and hand it to the
    driver, which decides (once-only, gated) whether to generate.
    """
    user_text, assistant_text = last_turn_context(events)
    driver.seed_idle_suggestions(user_text, assistant_text)


class SuggestionService:
    """Generates next-prompt suggestions via a throwaway, resume-less SDK query.

    `sdk_factory(cwd, config_dir, account) -> SDKClientProtocol` builds a one-shot client bound
    to the session's own account config dir; `account` is used to also inject a setup-token env
    (see `account_auth.oauth_env_for`) for accounts whose browser-OAuth refresh is dead. Best-
    effort: returns [] on empty context or any failure.
    """

    def __init__(self, sdk_factory):
        self._factory = sdk_factory

    async def generate(self, cwd: str, config_dir: str, context: str, account: str | None = None) -> list[str]:
        if not context.strip():
            return []
        try:
            client = self._factory(cwd, config_dir, account)
            tr = Translator()
            parts: list[str] = []
            async with client:
                await client.query(build_prompt(context))
                async for msg in one_turn(client):
                    for e in tr.feed(msg):
                        if isinstance(e, ev.TextDelta):
                            parts.append(e.text)
            return parse_suggestions("".join(parts))
        except Exception:
            log.warning("suggestion generation failed cwd=%s", cwd, exc_info=True)
            return []
