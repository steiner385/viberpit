"""One-shot Sonnet composer for autopilot nudge text. Mirrors autopilot_decider:
injected _query_fn so the real model is NEVER called in tests; returns None on any
failure or an explicit escalate — there is NO template fallback (spec §3)."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_GUIDANCE = (Path(__file__).parent / "nudge_composer_guidance.md").read_text(encoding="utf-8")
_MAX_NUDGE_CHARS = 2000


def _default_sdk_query_fn():
    from claude_agent_sdk import query  # lazy — not available in tests
    return query


def _build_prompt(ctx, *, intent_hint, verdict_reason, verdict_remedy,
                  recent_nudges, rich_tail, facts_verified) -> str:
    payload = json.dumps({
        "status": ctx.status,
        "effective_goal": ctx.effective_goal,
        "goal_source": ctx.goal_source,
        "stuck": ctx.stuck,
        "pr_facts": ctx.pr_facts.to_prompt() if ctx.pr_facts is not None else None,
        "facts_verified": facts_verified,
        "reversal_hint": ctx.reversal_hint,
        "fleet": ctx.fleet_context,
        "intent_hint": intent_hint,
        "verdict_reason": verdict_reason,
        "verdict_remedy": verdict_remedy,
        "prior_nudges": recent_nudges,
        "transcript": rich_tail,
    }, indent=2, default=str)
    # Format demand in the USER prompt too — the proven one-shot pattern
    # (system-prompt-only instructions drift; see autopilot_decider._build_prompt).
    return (payload + "\n\nWrite the single next direction for this session. Reply "
            'with ONLY {"nudge": "<message>"} or {"escalate": true, "reason": "<why>"} '
            "— no prose, no code fences.")


def _extract_obj(raw: str) -> dict | None:
    """First JSON object carrying 'nudge' or 'escalate' in a possibly-prose reply
    (same raw_decode scan as autopilot_decider._extract_decision_obj)."""
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
        if isinstance(obj, dict) and ("nudge" in obj or "escalate" in obj):
            return obj
        idx = start + 1


async def compose_nudge(ctx, *, intent_hint, verdict_reason, verdict_remedy,
                        recent_nudges, rich_tail, facts_verified,
                        _query_fn=None, _config_dir=None, _cwd=None) -> str | None:
    query_fn = _query_fn or _default_sdk_query_fn()
    config_dir = _config_dir if _config_dir is not None else os.environ.get("CLAUDE_CONFIG_DIR", "")
    cwd = _cwd or os.getcwd()
    try:
        options = None
        if _query_fn is None:
            from claude_agent_sdk import ClaudeAgentOptions
            options = ClaudeAgentOptions(
                model="sonnet", system_prompt=_GUIDANCE, cwd=cwd,
                env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
                allowed_tools=[], setting_sources=[])
        parts: list[str] = []
        prompt = _build_prompt(ctx, intent_hint=intent_hint, verdict_reason=verdict_reason,
                               verdict_remedy=verdict_remedy, recent_nudges=recent_nudges,
                               rich_tail=rich_tail, facts_verified=facts_verified)
        async for msg in query_fn(prompt=prompt, options=options):
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for block in content:
                    t = getattr(block, "text", None)
                    if isinstance(t, str):
                        parts.append(t)
        obj = _extract_obj("".join(parts).strip())
        if obj is None:
            log.warning("nudge_composer unparseable reply (head): %r", "".join(parts)[:400])
            return None
        if obj.get("escalate"):
            log.info("nudge_composer declined: %s", str(obj.get("reason", ""))[:200])
            return None
        text = str(obj.get("nudge", "")).strip()
        return text[:_MAX_NUDGE_CHARS] or None
    except Exception as exc:  # model down / bad options — caller escalates
        log.warning("nudge_composer failed: %s", exc)
        return None
