"""Pure, synchronous hard-rule guardrails for autopilot. NO I/O — every rule is a
direct unit test. A guardrail verdict always wins over the LLM decider."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Statuses autopilot may act on (everything else is left alone).
ACTIONABLE_STATUSES = frozenset({
    "done", "idle", "waiting_input", "waiting_question", "ready_review", "needs_attention",
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
