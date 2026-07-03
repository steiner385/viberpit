"""One-shot LLM decider for autopilot. Mirrors backlog_runner.rank_issues: injected
_query_fn so the real model is NEVER called in tests; safe 'escalate' on any failure."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .autopilot_policy import Decision, DecisionContext

log = logging.getLogger(__name__)

_GUIDANCE = (Path(__file__).parent / "autopilot_guidance.md").read_text(encoding="utf-8")
_VALID_ACTIONS = {"approve", "deny", "answer", "nudge", "complete", "escalate"}


def _default_sdk_query_fn():
    from claude_agent_sdk import query  # lazy — not available in tests
    return query


def _build_prompt(ctx: DecisionContext) -> str:
    return json.dumps({
        "status": ctx.status,
        "effective_goal": ctx.effective_goal,
        "goal_source": ctx.goal_source,
        "stuck": ctx.stuck,
        "transcript_tail": ctx.transcript_tail[-12:],
    }, indent=2, default=str)


def _parse(raw: str) -> Decision:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return Decision(action="escalate", reason="decider-failed:no-json")
    obj = json.loads(raw[start:end + 1])
    action = obj.get("action")
    if action not in _VALID_ACTIONS:
        return Decision(action="escalate", reason=f"decider-failed:bad-action:{action}")
    ai = obj.get("answer_index")
    return Decision(action=action,
                    answer_index=int(ai) if isinstance(ai, (int, float)) else None,
                    prompt=obj.get("prompt"),
                    reason=str(obj.get("reason", ""))[:500])


async def decide(ctx: DecisionContext, *, _query_fn=None, _config_dir=None, _cwd=None) -> Decision:
    query_fn = _query_fn or _default_sdk_query_fn()
    config_dir = _config_dir if _config_dir is not None else os.environ.get("CLAUDE_CONFIG_DIR", "")
    cwd = _cwd or os.getcwd()
    try:
        options = None
        if _query_fn is None:
            from claude_agent_sdk import ClaudeAgentOptions
            options = ClaudeAgentOptions(
                model="haiku", system_prompt=_GUIDANCE, cwd=cwd,
                env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
                allowed_tools=[], setting_sources=[])
        parts: list[str] = []
        async for msg in query_fn(prompt=_build_prompt(ctx), options=options):
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for block in content:
                    t = getattr(block, "text", None)
                    if isinstance(t, str):
                        parts.append(t)
        return _parse("".join(parts).strip())
    except Exception as exc:  # model down / parse error / bad options
        log.warning("autopilot.decide failed: %s", exc)
        return Decision(action="escalate", reason=f"decider-failed:{type(exc).__name__}")
