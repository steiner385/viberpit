"""One-shot LLM decider for autopilot. Mirrors backlog_grouping.propose_groups: injected
_query_fn so the real model is NEVER called in tests; safe 'escalate' on any failure."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from server.sessions.account_auth import helper_env
from server.sessions.helper_call import run_helper_call

from .autopilot_policy import Decision, DecisionContext

log = logging.getLogger(__name__)

_GUIDANCE = (Path(__file__).parent / "autopilot_guidance.md").read_text(encoding="utf-8")
_VALID_ACTIONS = {"approve", "deny", "answer", "nudge", "complete", "escalate"}

# Consecutive decider-call failures (model unreachable / erroring — the
# `outcome.error is not None` branch below) before a transient blip is treated
# as a persistent outage. 5 is ~45 minutes at the observed ~9-minute autopilot
# cadence: on 2026-08-22/23 autopilot failed every call for over a day and the
# only trace anywhere was a per-call log.warning nobody was reading. Module-level
# because the outage this tracks is infra-wide (the decider's model connection),
# not per-session — reset to 0 on the next call that gets a real reply.
_ESCALATE_FAILURE_THRESHOLD = 5
_consecutive_decide_failures = 0


def _note_decide_failure(error: BaseException) -> None:
    """Count one decider-call failure and log louder once it looks persistent."""
    global _consecutive_decide_failures
    _consecutive_decide_failures += 1
    if _consecutive_decide_failures >= _ESCALATE_FAILURE_THRESHOLD:
        log.error(
            "autopilot.decide PERSISTENT FAILURE: %d consecutive decider failures "
            "(threshold=%d) — likely an outage, not a blip: %s",
            _consecutive_decide_failures, _ESCALATE_FAILURE_THRESHOLD, error)
    else:
        log.warning("autopilot.decide failed (%d consecutive): %s",
                    _consecutive_decide_failures, error)


def _note_decide_success() -> None:
    """Reset the consecutive-failure counter — the decider is reachable again."""
    global _consecutive_decide_failures
    _consecutive_decide_failures = 0


def _default_sdk_query_fn():
    from claude_agent_sdk import query  # lazy — not available in tests
    return query


def _build_prompt(ctx: DecisionContext) -> str:
    payload = json.dumps({
        "status": ctx.status,
        "effective_goal": ctx.effective_goal,
        "goal_source": ctx.goal_source,
        "stuck": ctx.stuck,
        "pr_facts": ctx.pr_facts.to_prompt() if ctx.pr_facts is not None else None,
        "reversal_hint": ctx.reversal_hint,
        "fleet": ctx.fleet_context,
        "transcript_tail": ctx.transcript_tail[-12:],
    }, indent=2, default=str)
    # The format demand must live in the USER prompt too (the proven one-shot
    # pattern): a system-prompt-only instruction drifts under weak context and the
    # model answers in prose instead of the decision object (prod, 2026-07-03).
    return (payload + "\n\nDecide the single next action for this session. "
            "Reply with ONLY the JSON decision object "
            '{"action", "answer_index", "prompt", "reason"} — '
            "no prose, no preamble, no code fences.")


def _extract_decision_obj(raw: str) -> dict | None:
    """Find the first action-bearing JSON object in a possibly-prose reply.

    Scans every '{' with raw_decode so stray braces in surrounding prose and
    multiple objects can't break parsing (the greedy find/rfind slice raised
    "Extra data" on real replies). Fenced blocks need no special-casing — the
    scan lands on the object inside the fence."""
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
        if isinstance(obj, dict) and "action" in obj:
            return obj
        idx = start + 1


def _parse(raw: str) -> Decision:
    obj = _extract_decision_obj(raw)
    if obj is None:
        # Log the reply head: without it a production parse failure is undiagnosable
        # (every escalation reads "no-json" with no evidence of what the model said).
        log.warning("autopilot.decider unparseable reply (head): %r", raw[:400])
        return Decision(action="escalate", reason="decider-failed:no-json")
    action = obj.get("action")
    if action not in _VALID_ACTIONS:
        log.warning("autopilot.decider bad action %r (head): %r", action, raw[:400])
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
                env=helper_env(config_dir),
                allowed_tools=[], setting_sources=[])
    except Exception as exc:  # bad/unimportable options
        log.warning("autopilot.decide failed: %s", exc)
        return Decision(action="escalate", reason=f"decider-failed:{type(exc).__name__}")

    outcome = await run_helper_call(
        lambda: query_fn(prompt=_build_prompt(ctx), options=options),
        subsystem="autopilot.decide", account_key=config_dir)
    if outcome.skipped:
        # Account's decider lane is cooled-down (rate-limited/de-authed). Escalate
        # to a human rather than fire a call certain to fail — the safe default.
        return Decision(action="escalate", reason="decider-skipped:circuit-open")
    if outcome.error is not None:  # model down / parse error
        _note_decide_failure(outcome.error)
        return Decision(action="escalate",
                        reason=f"decider-failed:{type(outcome.error).__name__}")
    _note_decide_success()
    parts: list[str] = []
    for msg in outcome.messages:
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            for block in content:
                t = getattr(block, "text", None)
                if isinstance(t, str):
                    parts.append(t)
    return _parse("".join(parts).strip())
