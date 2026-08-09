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
    # True when a human unarchived this session: the loop surfaces a met goal but NEVER
    # auto-archives it again (only archiving is suppressed — other help continues).
    no_auto_archive: bool = False
    # Compact caution string ("N recent auto-archives in this repo were manually reversed…")
    # fed to the decider so the auto-archive judgement learns from prior reversals. None = none.
    reversal_hint: str | None = None
    # Elapsed wall-seconds since the user last steered this session (prompt/answer/permission/typing).
    # Precomputed by _autopilot_get_context (autopilot's clock is monotonic, so a raw timestamp
    # cannot be compared against pre_decide's `now`). None = the human never touched it.
    seconds_since_human: float | None = None
    # Compact view of the REST of the fleet (same-repo siblings, other actives,
    # recently archived + outcomes) from fleet_context.build_fleet_view. Advisory
    # input to the LLM decider ONLY — no guardrail reads it. None = nothing to report.
    fleet_context: dict | None = None
    # Durable 'session resolved it will not open a PR' token (marker or high-confidence
    # classifier). When set and no real PR exists, met_bar treats the goal as met, so
    # post_validate stops downgrading `complete` to an open-a-PR nudge. None = unresolved.
    pr_resolution: str | None = None
    # The session's initial prompt (or equivalent task specification). Forwarded to
    # met_bar so an explicit `--squash --auto` arming step in the instructions raises the
    # completion bar to require an armed PR even when the goal text reads as non-code work.
    instructions: str | None = None
    # Set by the loop after an on-demand refresh: pr_facts is GitHub-fresh.
    facts_verified: bool = False


@dataclass
class Decision:
    action: str                 # approve|deny|answer|nudge|complete|escalate
    answer_index: int | None = None
    prompt: str | None = None
    reason: str = ""
    # Composition metadata (spec §1): a gate downgrade carries the bar verdict as
    # structured context; the nudge text is ALWAYS written by the composer.
    needs_composition: bool = False
    downgraded_from: str | None = None
    verdict_reason: str | None = None
    verdict_remedy: str | None = None


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
    """Stable identity of a stuck state, for loop detection.

    A concrete interaction (pending permission / plan / question) keys the identity on
    that payload — a genuinely new one is new information and resets the loop counter.

    With NO concrete interaction, autopilot's only lever is a nudge toward the goal, so
    the identity is the GOAL alone. The momentary ``status`` is deliberately EXCLUDED
    here: a rejected nudge (e.g. 'Prompt is too long' on a context-exhausted session)
    flaps the status between ticks (idle <-> needs_attention <-> working). Keying on it
    let every flap reset ``repeat_count``, so a stuck 'keep nudging the same goal' loop
    never tripped detection and autopilot nudged forever (prod: PR #11273, 2026-07-06).
    Collapsing to the goal keeps ONE identity across the flapping, so the counter climbs
    monotonically and escalation sticks."""
    s = ctx.stuck or {}
    concrete = s.get("tool_name") or s.get("question") or (s.get("plan_text") or "")[:60]
    if concrete:
        return f"{s.get('kind')}|{concrete}|{ctx.effective_goal}"
    return f"advance|{ctx.effective_goal}"


# --- Contextual-nudge policy constants (spec: contextual-nudges design) ---------
NUDGE_DEDUPE_WINDOW_S = 6 * 3600.0
NUDGE_DEDUPE_THRESHOLD = 0.85
RECENT_NUDGES_CAP = 5
FACTS_REFRESH_MIN_INTERVAL_S = 600.0

_PR_NUM_RE = re.compile(r"\bPR\s*#(\d+)", re.IGNORECASE)


def claimed_pr_numbers(transcript_tail: list[dict]) -> set[int]:
    """PR numbers the transcript tail claims exist (e.g. 'PR #123 is armed')."""
    nums: set[int] = set()
    for entry in transcript_tail or []:
        for m in _PR_NUM_RE.finditer(entry.get("text", "") or ""):
            nums.add(int(m.group(1)))
    return nums


def facts_contradicted(ctx: DecisionContext) -> bool:
    """True when the cached pr_facts look stale enough to verify before directing:
    the session claims a PR the cache doesn't know, or it self-declared complete,
    while the cache shows no armed/merged PR. No facts / no repo -> nothing to
    verify (never triggers a refresh)."""
    facts = ctx.pr_facts
    if facts is None or not facts.has_repo:
        return False
    if facts.merged_pr is not None or facts.armed_pr is not None:
        return False
    known = {p.number for p in facts.prs}
    if claimed_pr_numbers(ctx.transcript_tail) - known:
        return True
    return ctx.status == "complete"


def _token_set(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9#_./-]+", (text or "").lower()))


def near_duplicate(text: str, recent: list[dict], now: float, *,
                   window_s: float = NUDGE_DEDUPE_WINDOW_S,
                   threshold: float = NUDGE_DEDUPE_THRESHOLD) -> bool:
    """Deterministic anti-repetition: token-set (Jaccard) similarity of a composed
    nudge against nudges sent in the window. Keyed on what autopilot SAID — immune
    to the goal-text drift that resets `fingerprint` (spec §4)."""
    a = _token_set(text)
    if not a:
        return False
    for entry in recent or []:
        if now - float(entry.get("ts", 0.0)) > window_s:
            continue
        b = _token_set(entry.get("text", ""))
        if b and len(a & b) / len(a | b) >= threshold:
            return True
    return False


def _hourly_count(ledger: dict, now: float) -> int:
    return sum(1 for t in ledger.get("hour_actions", []) if t > now - _HOUR)


def pre_decide(ctx: DecisionContext, config: dict, now: float) -> Decision | None:
    """Hard-rule pre-filter. Returns a forced Decision (deny/escalate/skip) or None
    to defer the judgement to the LLM decider. Precedence per the spec."""
    ledger = ctx.ledger or {}
    stuck = ctx.stuck or {}

    # 1. Human deference — stand down while the human is actively steering this session.
    if ctx.seconds_since_human is not None and ctx.seconds_since_human < int(config.get("human_defer_seconds", 120)):
        return Decision(action="skip", reason="human steering — deferring")

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
    approve or index a non-existent option. Empty-nudge enforcement was removed:
    the loop escalates on composer failure instead (Task 6)."""
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
    if decision.action == "complete" and ctx.pr_facts is not None:
        # Deterministic backstop the untrusted decider cannot override: an open PR
        # that is not armed AND a code goal that opened zero PRs are both definitionally
        # NOT the finished state. Downgrade to a composition-marked nudge in both cases.
        # `not verdict.met` covers exactly three cases (per completion_bar.met_bar):
        #   - open-unarmed PR (confidence=high) → nudge toward arming it
        #   - armed PR with a red required check (confidence=high) → nudge toward fixing CI (#635)
        #   - code goal + repo + no PR at all (confidence=low) → nudge toward opening one
        # The non-code/research case has met=True, so research/Q&A sessions still complete.
        # prompt=None: the gate NEVER fabricates nudge text — it marks the decision for
        # composition (the composer writes the actual message from verdict_reason/remedy).
        from .completion_bar import met_bar
        verdict = met_bar(ctx.effective_goal, ctx.goal_source, ctx.pr_facts,
                          ctx.pr_resolution, instructions=ctx.instructions)
        if not verdict.met:
            return Decision(action="nudge", prompt=None, needs_composition=True,
                            downgraded_from="complete",
                            verdict_reason=verdict.reason, verdict_remedy=verdict.remedy,
                            reason=f"completion-gate: {verdict.reason}")
    return decision
