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
per-account breaker).

The shared prose heuristic ``background_tasks.text_awaits_user`` is the RECALL
FLOOR, not a fallback: it runs first and, when it sees no ask at all, the answer
is False and no model is called. The classifier only ever NARROWS what the
heuristic already flagged (a polite closing, a rhetorical question), so it can
never invent a hold. Prod 2026-09-07: two completed sessions were flipped to
waiting_question ("Answer needed") on final messages that asked nothing — one
closed "No further action needed from me" — and the user opened each card to
find no question anywhere; the pm-classify session's entire run was discarded
because its ingest chain waits for a settled status. Both tails were "no ask"
to the heuristic. Ordering it first also drops a Haiku call from every clean
completion. The trade is explicit: recall is now bounded by
``_ASK_PHRASES`` + a trailing "?" over that function's tail scan.

Above the floor, the heuristic is still the degrade path whenever the helper
lane is unavailable (breaker open, no usable account, SDK/model failure,
unparseable output) — and there it is already known True, so a helper outage
holds the session rather than archiving a question away.
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
    the user. The shared prose heuristic gates the call (see module docstring);
    above that floor it is the LLM verdict, or the heuristic's own True when the
    helper lane is unusable. Never raises."""
    from .background_tasks import text_awaits_user
    if not (text or "").strip():
        return False
    # Recall floor. Below it there is nothing for the classifier to narrow, so a
    # "yes" from it could only ever be a false positive on a finished session.
    if not text_awaits_user(text):
        return False
    # From here on the heuristic has already answered True, so every degrade path
    # below returns True rather than re-scanning the same prose for the same answer.
    try:
        # Lazy options: construct SDK options ONLY on the real-model path, so an
        # injected query_fn (tests) never touches the SDK (pr_intent pattern).
        options = None
        if query_fn is None:
            cfg = config_dir if config_dir is not None else (
                os.environ.get("CLAUDE_CONFIG_DIR") or pick_helper_config_dir())
            if not cfg:
                return True  # no usable account — degrade to the heuristic's verdict
            config_dir = cfg
            from claude_agent_sdk import ClaudeAgentOptions, query
            query_fn = query
            options = ClaudeAgentOptions(
                model="haiku", system_prompt=_SYSTEM, cwd=cwd or os.getcwd(),
                env=helper_env(config_dir),
                allowed_tools=[], setting_sources=[], **cli_path_kwargs())
    except Exception as exc:
        log.warning("open_question.classify unavailable: %s", exc)
        return True

    outcome = await run_helper_call(
        lambda: query_fn(prompt=_build_prompt(text), options=options),
        subsystem="open_question.classify", account_key=config_dir or "")
    if outcome.skipped:
        return True  # lane cooled-down — degrade, no call made
    if outcome.error is not None:
        log.warning("open_question.classify failed: %s", outcome.error)
        return True
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
        return True
    return verdict
