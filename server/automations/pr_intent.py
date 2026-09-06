"""Gated Haiku classifier: has a session decided NOT to open a PR? Mirrors
autopilot_decider — injected _query_fn so the real model is NEVER called in tests;
any failure/ambiguity degrades to an unresolved verdict (autopilot then falls back to
its normal no-PR nudge)."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from server.sessions.account_auth import helper_env
from server.sessions.claude_bin import cli_path_kwargs
from server.sessions.helper_call import run_helper_call

log = logging.getLogger(__name__)

_SYSTEM = (
    "You judge whether a coding session has deliberately decided NOT to open a pull "
    "request, judged against its GOAL — either the goal is already satisfied or genuinely "
    "needs no code change ('not-needed'), or the session/user cancelled or abandoned the "
    "PR work ('cancelled'). A goal that asks for a code change is NOT resolved by "
    "investigating it and stopping: recommending a next step, proposing a spec or plan, "
    "offering to continue, or waiting on the user is unfinished work, not a resolution — "
    "say resolved=false. You are NOT deciding whether a PR would be nice; only whether "
    "the session has already RESOLVED not to open one. When unsure, say resolved=false. "
    "Reply with ONLY a JSON object "
    '{"resolved": bool, "reason": "cancelled"|"not-needed"|"", "confidence": "high"|"low"} '
    "— no prose, no code fences."
)


@dataclass(frozen=True)
class PrIntentVerdict:
    resolved: bool
    reason: str
    confidence: str


_UNRESOLVED = PrIntentVerdict(False, "", "low")


def _default_sdk_query_fn():
    from claude_agent_sdk import query  # lazy — not available in tests
    return query


def _build_prompt(transcript_tail: list[dict], goal: str) -> str:
    payload = json.dumps({"goal": goal, "transcript_tail": transcript_tail[-40:]},
                         indent=2, default=str)
    return (payload + "\n\nHas this session resolved NOT to open a PR? "
            'Reply with ONLY the JSON object {"resolved","reason","confidence"}.')


def _extract_obj(raw: str) -> dict | None:
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
        if isinstance(obj, dict) and "resolved" in obj:
            return obj
        idx = start + 1


def _parse(raw: str) -> PrIntentVerdict:
    obj = _extract_obj(raw)
    if obj is None:
        return _UNRESOLVED
    if not bool(obj.get("resolved")):
        return _UNRESOLVED
    reason = "cancelled" if obj.get("reason") == "cancelled" else "not-needed"
    confidence = "high" if obj.get("confidence") == "high" else "low"
    return PrIntentVerdict(True, reason, confidence)


async def classify_pr_intent(transcript_tail: list[dict], goal: str, *,
                             _query_fn=None, _config_dir=None, _cwd=None) -> PrIntentVerdict:
    try:
        query_fn = _query_fn or _default_sdk_query_fn()
        config_dir = _config_dir if _config_dir is not None else os.environ.get("CLAUDE_CONFIG_DIR", "")
        cwd = _cwd or os.getcwd()
        options = None
        if _query_fn is None:
            from claude_agent_sdk import ClaudeAgentOptions
            options = ClaudeAgentOptions(
                model="haiku", system_prompt=_SYSTEM, cwd=cwd,
                env=helper_env(config_dir),
                allowed_tools=[], setting_sources=[], **cli_path_kwargs())
    except Exception as exc:
        log.warning("pr_intent.classify failed: %s", exc)
        return _UNRESOLVED

    outcome = await run_helper_call(
        lambda: query_fn(prompt=_build_prompt(transcript_tail, goal), options=options),
        subsystem="pr_intent.classify", account_key=config_dir)
    if outcome.skipped:
        return _UNRESOLVED  # account lane cooled-down — fall back to the normal nudge
    if outcome.error is not None:
        log.warning("pr_intent.classify failed: %s", outcome.error)
        return _UNRESOLVED
    parts: list[str] = []
    for msg in outcome.messages:
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            for block in content:
                t = getattr(block, "text", None)
                if isinstance(t, str):
                    parts.append(t)
    return _parse("".join(parts).strip())
