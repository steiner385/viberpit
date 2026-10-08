"""One-shot LLM decider for autopilot. Mirrors backlog_grouping.propose_groups: injected
_query_fn so the real model is NEVER called in tests; safe 'escalate' on any failure."""
from __future__ import annotations

import asyncio

import json
import logging
import os
from pathlib import Path

from server.sessions.account_auth import account_store_io, helper_env
from server.sessions.claude_bin import cli_path_kwargs
from server.sessions.helper_call import helper_cwd, run_helper_call, stateless_helper_options

from .autopilot_policy import Decision, DecisionContext, missing_claimed_prs

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


def _note_decide_failure(error: BaseException, kind: str | None = None,
                         detail: str | None = None) -> None:
    """Count one decider-call failure and log louder once it looks persistent.

    ``kind`` is the classified failure (``failure.classify``) and ``detail`` the
    CLI's own error text. Both are logged because the exception alone is often
    useless: for the whole of #1820 it read "Claude Code returned an error
    result: success" — the result *subtype* leaking into the message — and the
    real cause (one account's org had disabled Claude Code) was only findable by
    digging out a helper transcript. ``detail`` goes through ``%r`` so the error
    sink normalizes the quoted run to ``<str>`` and one condition stays one
    auto-error issue however the CLI words it.
    """
    global _consecutive_decide_failures
    _consecutive_decide_failures += 1
    if _consecutive_decide_failures >= _ESCALATE_FAILURE_THRESHOLD:
        log.error(
            "autopilot.decide PERSISTENT FAILURE: %d consecutive decider failures "
            "(threshold=%d) — likely an outage, not a blip: kind=%s %s detail=%r",
            _consecutive_decide_failures, _ESCALATE_FAILURE_THRESHOLD,
            kind or "unknown", error, detail or "")
    else:
        log.warning("autopilot.decide failed (%d consecutive): kind=%s %s detail=%r",
                    _consecutive_decide_failures, kind or "unknown", error, detail or "")


def _note_decide_success() -> None:
    """Reset the consecutive-failure counter — the decider is reachable again."""
    global _consecutive_decide_failures
    _consecutive_decide_failures = 0


def _default_sdk_query_fn():
    from claude_agent_sdk import query  # lazy: resolved off the package at call
    # time, which lets server/tests/conftest.py stub every helper door at once (#1383)
    return query


def _build_prompt(ctx: DecisionContext) -> str:
    payload = json.dumps({
        "status": ctx.status,
        "effective_goal": ctx.effective_goal,
        "goal_source": ctx.goal_source,
        "stuck": ctx.stuck,
        "pr_facts": ctx.pr_facts.to_prompt() if ctx.pr_facts is not None else None,
        "pr_facts_verified": ctx.facts_verified,
        "unlinked_pr_claims": sorted(missing_claimed_prs(ctx)),
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
    config_dir = _config_dir if _config_dir is not None else os.environ.get("CLAUDE_CONFIG_DIR", "")
    try:
        options = None
        if _query_fn is None:
            if not config_dir:
                # No helper account to run as (the orchestrator's engine-safe picker
                # found no live Anthropic account, and the daemon exports no
                # CLAUDE_CONFIG_DIR). A spawn would only answer "Not logged in" and
                # raise — 2026-09-09 that cost 4.5h of doomed calls and 270 feed
                # events for one Codex-account session. Known answer, no spawn.
                log.warning("autopilot.decide skipped: no helper account (sid=%s)",
                            ctx.session_id)
                return Decision(action="escalate", reason="decider-skipped:no-helper-account")
            from claude_agent_sdk import ClaudeAgentOptions
            options = ClaudeAgentOptions(
                model="haiku", system_prompt=_GUIDANCE, cwd=_cwd or helper_cwd(),
                env=await account_store_io(helper_env, config_dir),
                **stateless_helper_options(), **(await asyncio.to_thread(cli_path_kwargs)))
        query_fn = _query_fn or _default_sdk_query_fn()
    except Exception as exc:  # bad/unimportable options
        log.warning("autopilot.decide failed: %s", exc)
        return Decision(action="escalate", reason=f"decider-failed:{type(exc).__name__}")

    outcome = await run_helper_call(
        lambda: query_fn(prompt=_build_prompt(ctx), options=options),
        subsystem="autopilot.decide", account_key=config_dir)
    if outcome.skipped:
        # The account cannot answer right now — its decider lane is cooled down
        # (rate-limited/de-authed) or the account is org-denied outright. Escalate
        # to a human rather than fire a call certain to fail — the safe default.
        # The two read very differently to whoever picks the escalation up, so the
        # reason carries which one it was.
        return Decision(action="escalate",
                        reason=f"decider-skipped:{outcome.skip_reason or 'circuit-open'}")
    if outcome.error is not None:  # model down / parse error
        _note_decide_failure(outcome.error, outcome.failure_kind, outcome.error_text)
        # The classified kind, not the exception class: every SDK failure is a
        # RuntimeError, so "decider-failed:RuntimeError" said nothing about which.
        return Decision(
            action="escalate",
            reason=f"decider-failed:{outcome.failure_kind or type(outcome.error).__name__}")
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
