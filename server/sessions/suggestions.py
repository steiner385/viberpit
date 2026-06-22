from __future__ import annotations
import json
import logging

from .translator import Translator
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

    `sdk_factory(cwd, config_dir) -> SDKClientProtocol` builds a one-shot client bound to the
    session's own account config dir. Best-effort: returns [] on empty context or any failure.
    """

    def __init__(self, sdk_factory):
        self._factory = sdk_factory

    async def generate(self, cwd: str, config_dir: str, context: str) -> list[str]:
        if not context.strip():
            return []
        try:
            client = self._factory(cwd, config_dir)
            tr = Translator()
            parts: list[str] = []
            async with client:
                await client.query(build_prompt(context))
                async for msg in client.receive_response():
                    for e in tr.feed(msg):
                        if isinstance(e, ev.TextDelta):
                            parts.append(e.text)
            return parse_suggestions("".join(parts))
        except Exception:
            log.warning("suggestion generation failed cwd=%s", cwd, exc_info=True)
            return []
