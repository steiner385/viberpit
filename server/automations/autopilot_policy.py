"""Pure, synchronous hard-rule guardrails for autopilot. NO I/O — every rule is a
direct unit test. A guardrail verdict always wins over the LLM decider."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .completion_bar import PrFacts

# Statuses autopilot may act on (everything else is left alone). `complete` is the
# agent's own completion marker — the strongest goal-met signal — so it MUST be
# actionable: the decider confirms the goal is met and archives the session (or nudges
# a premature marker toward the remaining work). Without it a well-behaved session that
# signals completion strands in `complete` forever, archived by no one.
ACTIONABLE_STATUSES = frozenset({
    "done", "idle", "waiting_input", "waiting_question", "ready_review", "needs_attention",
    "complete",
})

# Hard-coded destructive-command deny-list (regex, matched against a Bash command
# or any tool_input value). Baseline is non-negotiable; config only EXTENDS it.
_BASELINE_DENY: tuple[tuple[str, str], ...] = (
    ("force_push", r"\bgit\s+push\b.*(--force(?!-with-lease)|\s-f\b)"),
    ("history_rewrite", r"\bgit\s+(reset\s+--hard\s+origin/|rebase\b.*origin/|filter-branch\b|push\b.*--mirror)"),
    ("bulk_delete", r"\brm\s+-rf\b|\bgit\s+clean\s+-[a-z]*f[a-z]*d|\bgit\s+clean\s+-[a-z]*d[a-z]*f"),
    ("branch_delete", r"\bgit\s+branch\s+-D\b|\bgit\s+push\b.*--delete"),
    ("pr_merge_or_delete", r"\bgh\s+pr\s+(merge|close)\b|\bgh\s+repo\s+delete\b"),
    ("secret_exfil", r"(~/\.ssh|\.env)\b.*(curl|nc|wget)|curl\b.*\|\s*sh\b"),
)


@dataclass
class DecisionContext:
    session_id: str
    status: str
    account: str
    repo: str | None
    branch: str | None
    effective_goal: str
    goal_source: str
    stuck: dict           # {kind, tool_name?, tool_input?, question?, options?, plan_text?}
    transcript_tail: list[dict] = field(default_factory=list)
    ledger: dict = field(default_factory=dict)
    pr_facts: "PrFacts | None" = None   # verified PR ground-truth (None = unknown)


@dataclass
class Decision:
    action: str                 # approve|deny|answer|nudge|complete|escalate
    answer_index: int | None = None
    prompt: str | None = None
    reason: str = ""


def _flatten_input(tool_input: dict) -> str:
    """Join every string value in a tool input into one searchable blob."""
    parts: list[str] = []
    for v in (tool_input or {}).values():
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, (list, tuple)):
            parts.extend(str(x) for x in v)
    return "\n".join(parts)


def matches_denylist(tool_name: str, tool_input: dict, extra_patterns: list[str]) -> str | None:
    """Return the name of the first matched deny rule, else None. Patterns match the
    tool name + all string inputs, case-insensitively."""
    blob = f"{tool_name}\n{_flatten_input(tool_input)}"
    for name, pat in _BASELINE_DENY:
        if re.search(pat, blob, re.IGNORECASE):
            return name
    for i, pat in enumerate(extra_patterns or []):
        try:
            if pat and re.search(re.escape(pat), blob, re.IGNORECASE):
                return f"extra:{i}"
        except re.error:
            continue
    return None


_HOUR = 3600.0


def fingerprint(ctx: DecisionContext) -> str:
    """Stable identity of a stuck state, for loop detection."""
    s = ctx.stuck or {}
    head = (s.get("tool_name") or s.get("question")
            or (s.get("plan_text") or "")[:60] or s.get("kind") or "")
    return f"{s.get('kind')}|{head}|{ctx.effective_goal}"


def _hourly_count(ledger: dict, now: float) -> int:
    return sum(1 for t in ledger.get("hour_actions", []) if t > now - _HOUR)


def pre_decide(ctx: DecisionContext, config: dict, now: float) -> Decision | None:
    """Hard-rule pre-filter. Returns a forced Decision (deny/escalate/skip) or None
    to defer the judgement to the LLM decider. Precedence per the spec."""
    ledger = ctx.ledger or {}
    stuck = ctx.stuck or {}

    # 2. Destructive deny-list (permission/plan only).
    if stuck.get("kind") in ("permission", "plan"):
        rule = matches_denylist(stuck.get("tool_name", ""), stuck.get("tool_input", {}),
                                config.get("extra_deny_patterns", []))
        if rule is not None:
            return Decision(action="deny", reason=f"deny-list:{rule}")

    # 5. Per-session hourly budget.
    if _hourly_count(ledger, now) >= config["max_actions_per_session_per_hour"]:
        return Decision(action="skip", reason="hourly budget exhausted")

    # 7. Loop detection.
    if (ledger.get("last_fingerprint") == fingerprint(ctx)
            and ledger.get("repeat_count", 0) >= config["loop_repeat_limit"]):
        return Decision(action="escalate", reason="loop: identical stuck state repeated")

    # 8. Recovery attempt cap.
    if (stuck.get("kind") == "needs_attention"
            and ledger.get("recovery_attempts", 0) >= config["recovery_max_attempts"]):
        return Decision(action="escalate", reason="recovery attempts exhausted")

    return None  # defer to the LLM decider


def post_validate(decision: Decision, ctx: DecisionContext, config: dict) -> Decision:
    """Second net: the untrusted decider may never upgrade a rule-denied tool to
    approve, index a non-existent option, or emit an empty nudge."""
    stuck = ctx.stuck or {}
    if decision.action == "approve":
        if not config.get("allow_tool_approvals", True):
            return Decision(action="escalate", reason="approvals disabled")
        if stuck.get("kind") in ("permission", "plan") and matches_denylist(
                stuck.get("tool_name", ""), stuck.get("tool_input", {}),
                config.get("extra_deny_patterns", [])):
            return Decision(action="escalate", reason="approve blocked by deny-list")
    if decision.action == "answer":
        opts = stuck.get("options") or []
        if decision.answer_index is None or not (0 <= decision.answer_index < len(opts)):
            return Decision(action="escalate", reason="answer_index out of range")
    if decision.action == "nudge" and not (decision.prompt or "").strip():
        return Decision(action="escalate", reason="empty nudge prompt")
    if decision.action == "complete" and ctx.pr_facts is not None:
        # Deterministic backstop the untrusted decider cannot override: an open PR
        # that is not armed is definitionally NOT the finished state (CLAUDE.md
        # completion workflow). Downgrade to a nudge toward arming it. The "no PR at
        # all" case is intentionally left to the fact-fed decider (goal judgment).
        from .completion_bar import met_bar
        verdict = met_bar(ctx.effective_goal, ctx.goal_source, ctx.pr_facts)
        if not verdict.met and verdict.confidence == "high":
            return Decision(action="nudge", prompt=verdict.remedy or
                            "Open a PR and arm auto-merge before finishing.",
                            reason=f"completion-gate: {verdict.reason}")
    return decision
