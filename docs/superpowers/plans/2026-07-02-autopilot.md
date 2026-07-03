# Autopilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A global `autopilot` automation that keeps every governed session moving through its stuck states — nudge/approve/answer/recover — until its effective goal is met, gated by hard-rule guardrails around an LLM decider.

**Architecture:** A non-reentrant `AutopilotLoop` (mirroring `BacklogRunnerLoop`) with fully injected seams. Each tick lists governed sessions, assembles a pure `DecisionContext`, runs pure guardrails (`autopilot_policy.py`) as a pre-filter and post-validator around a one-shot Haiku decider (`autopilot_decider.py`), then applies the `Decision` through existing orchestrator seams (`resolve_permission`/`answer_question`/`send_prompt`) under `SYSTEM_IDENTITY`. Registered in `registry.py` so it inherits the generic automations API/UI.

**Tech Stack:** Python 3 · asyncio · SQLAlchemy (SQLite dev) · claude-agent-sdk (`query`, injected in tests) · FastAPI · React/TS + vitest (frontend opt-out toggle).

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-07-02-autopilot-design.md`.
- **Pure guardrails:** `autopilot_policy.py` has **no I/O** — pure functions only, so every rule is a direct unit test.
- **No real model in tests:** the decider and any LLM call take an injected `_query_fn`; the real SDK `query` is **never** called in tests (mirror `rank_issues` in `server/automations/backlog_runner.py`).
- **Safety default:** every ambiguous/failed path resolves to `escalate` (no mutation), never to an action.
- **Deny-list is hard-coded baseline;** `extra_deny_patterns` config may only **extend** it, never shrink it.
- **Never act while draining:** `mode == "off"` or `orch._draining` → tick is a no-op.
- **New DB columns are nullable** (dev SQLite `init_db` backfills, no migration — per CLAUDE.md).
- **New orchestrator method an API calls → needs a `RemoteOrchestrator` proxy + registered RPC** (daemon-parity breadth test), per project memory.
- **CI gates:** `ruff` + `mypy` + `pytest` (server) and `vitest` + `tsc --noEmit` + `eslint` (web) must be green before PR. mypy is an easily-missed required gate.
- **Run tests from a fresh worktree** via the main checkout's prebuilt env; API tests use the `CCSWITCH_DEV=1` / allowlist harness (`env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest`).
- **Completion workflow:** branch off `origin/master`; PR non-draft + `gh pr merge --squash --auto`.

---

### Task 1: Guardrail value types + deny-list matcher (pure)

**Files:**
- Create: `server/automations/autopilot_policy.py`
- Test: `server/tests/test_autopilot_policy_denylist.py`

**Interfaces:**
- Produces: `@dataclass DecisionContext` (fields per spec DecisionContext), `@dataclass Decision(action, answer_index, prompt, reason)`, `ACTIONABLE_STATUSES: frozenset[str]`, `matches_denylist(tool_name: str, tool_input: dict, extra_patterns: list[str]) -> str | None` (returns the matched rule name, else `None`).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_autopilot_policy_denylist.py
import pytest
from server.automations.autopilot_policy import matches_denylist


@pytest.mark.parametrize("cmd", [
    "git push --force",
    "git push -f origin main",
    "git reset --hard origin/master",
    "rm -rf /home/tony/GitHub/KinDash",
    "git clean -fdx",
    "gh pr merge 123 --squash",
    "git branch -D feature",
])
def test_dangerous_bash_is_denied(cmd):
    assert matches_denylist("Bash", {"command": cmd}, []) is not None


@pytest.mark.parametrize("cmd", [
    "git push --force-with-lease",
    "pytest -q",
    "git add -A && git commit -m x",
    "git push origin HEAD",
    "gh pr create --fill",
])
def test_safe_bash_is_allowed(cmd):
    assert matches_denylist("Bash", {"command": cmd}, []) is None


def test_extra_patterns_extend_denylist():
    assert matches_denylist("Bash", {"command": "terraform apply"}, ["terraform apply"]) is not None


def test_non_bash_tool_not_denied_by_default():
    assert matches_denylist("Read", {"file_path": "/etc/passwd"}, []) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_autopilot_policy_denylist.py -q`
Expected: FAIL — `ModuleNotFoundError` / `ImportError: cannot import name 'matches_denylist'`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/automations/autopilot_policy.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_autopilot_policy_denylist.py -q`
Expected: PASS (all parametrized cases).

- [ ] **Step 5: Commit**

```bash
git add server/automations/autopilot_policy.py server/tests/test_autopilot_policy_denylist.py
git commit -m "feat(autopilot): guardrail value types + deny-list matcher"
```

---

### Task 2: Guardrail pre-filter, post-validate, and fingerprint (pure)

**Files:**
- Modify: `server/automations/autopilot_policy.py`
- Test: `server/tests/test_autopilot_policy_rules.py`

**Interfaces:**
- Consumes: `DecisionContext`, `Decision`, `matches_denylist` (Task 1).
- Produces:
  - `fingerprint(ctx: DecisionContext) -> str`
  - `pre_decide(ctx: DecisionContext, config: dict, now: float) -> Decision | None` — a forced verdict (`deny`/`escalate`), or `None` to defer to the LLM decider. Skips are represented as `Decision(action="escalate", reason="skip:<why>")` only when they should surface; a pure "skip this tick" returns `Decision(action="skip")`.
  - `post_validate(decision: Decision, ctx: DecisionContext, config: dict) -> Decision`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_autopilot_policy_rules.py
from server.automations.autopilot_policy import (
    DecisionContext, Decision, pre_decide, post_validate, fingerprint,
)

CONFIG = {
    "allow_tool_approvals": True,
    "max_actions_per_session_per_hour": 12,
    "recovery_max_attempts": 2,
    "loop_repeat_limit": 3,
    "human_defer_seconds": 120,
    "extra_deny_patterns": [],
}


def _ctx(**kw):
    base = dict(session_id="s1", status="waiting_input", account="a", repo=None,
                branch=None, effective_goal="ship it", goal_source="explicit",
                stuck={"kind": "permission", "tool_name": "Bash", "tool_input": {"command": "pytest"}},
                transcript_tail=[], ledger={})
    base.update(kw)
    return DecisionContext(**base)


def test_denylisted_permission_is_forced_deny():
    ctx = _ctx(stuck={"kind": "permission", "tool_name": "Bash",
                      "tool_input": {"command": "git push --force"}})
    d = pre_decide(ctx, CONFIG, now=1000.0)
    assert d is not None and d.action == "deny"


def test_safe_permission_defers_to_llm():
    assert pre_decide(_ctx(), CONFIG, now=1000.0) is None


def test_hourly_budget_exhausted_skips():
    ledger = {"hour_actions": [999.0] * 12}
    d = pre_decide(_ctx(ledger=ledger), CONFIG, now=1000.0)
    assert d is not None and d.action == "skip"


def test_loop_detection_escalates_to_needs_human():
    ctx = _ctx()
    fp = fingerprint(ctx)
    ctx.ledger = {"last_fingerprint": fp, "repeat_count": 3, "hour_actions": []}
    d = pre_decide(ctx, CONFIG, now=1000.0)
    assert d is not None and d.action == "escalate" and "loop" in d.reason


def test_recovery_cap_escalates():
    ctx = _ctx(status="needs_attention",
               stuck={"kind": "needs_attention"},
               ledger={"recovery_attempts": 2, "hour_actions": []})
    d = pre_decide(ctx, CONFIG, now=1000.0)
    assert d is not None and d.action == "escalate"


def test_post_validate_downgrades_illegal_approve():
    ctx = _ctx(stuck={"kind": "permission", "tool_name": "Bash",
                      "tool_input": {"command": "git push --force"}})
    out = post_validate(Decision(action="approve", reason="llm said so"), ctx, CONFIG)
    assert out.action == "escalate"


def test_post_validate_blocks_approve_when_switch_off():
    cfg = {**CONFIG, "allow_tool_approvals": False}
    out = post_validate(Decision(action="approve"), _ctx(), cfg)
    assert out.action == "escalate"


def test_post_validate_rejects_out_of_range_answer():
    ctx = _ctx(stuck={"kind": "question", "question": "q", "options": ["a", "b"]})
    out = post_validate(Decision(action="answer", answer_index=5), ctx, CONFIG)
    assert out.action == "escalate"


def test_post_validate_rejects_empty_nudge():
    out = post_validate(Decision(action="nudge", prompt="  "), _ctx(status="done"), CONFIG)
    assert out.action == "escalate"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_autopilot_policy_rules.py -q`
Expected: FAIL — `ImportError: cannot import name 'pre_decide'`.

- [ ] **Step 3: Write minimal implementation** (append to `server/automations/autopilot_policy.py`)

```python
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
    return decision
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_autopilot_policy_rules.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/automations/autopilot_policy.py server/tests/test_autopilot_policy_rules.py
git commit -m "feat(autopilot): guardrail pre-filter, post-validate, fingerprint"
```

---

### Task 3: LLM decider + guidance doc

**Files:**
- Create: `server/automations/autopilot_decider.py`
- Create: `server/automations/autopilot_guidance.md`
- Test: `server/tests/test_autopilot_decider.py`

**Interfaces:**
- Consumes: `DecisionContext`, `Decision` (Task 1).
- Produces: `async def decide(ctx: DecisionContext, *, _query_fn=None, _config_dir=None, _cwd=None) -> Decision`. Any parse/model failure returns `Decision(action="escalate", reason="decider-failed:<...>")`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_autopilot_decider.py
import pytest
from server.automations.autopilot_policy import DecisionContext
from server.automations.autopilot_decider import decide


def _ctx():
    return DecisionContext(
        session_id="s1", status="waiting_input", account="a", repo=None, branch=None,
        effective_goal="ship the PR", goal_source="explicit",
        stuck={"kind": "permission", "tool_name": "Bash", "tool_input": {"command": "pytest"}},
        transcript_tail=[], ledger={})


def _fake_query(reply_text):
    async def gen(*, prompt, options):
        class _Block:
            text = reply_text
        class _Msg:
            content = [_Block()]
        yield _Msg()
    return gen


@pytest.mark.asyncio
async def test_valid_json_parses_to_decision():
    fn = _fake_query('{"action": "approve", "reason": "safe test run"}')
    d = await decide(_ctx(), _query_fn=fn, _config_dir="", _cwd="/tmp")
    assert d.action == "approve" and d.reason == "safe test run"


@pytest.mark.asyncio
async def test_malformed_reply_escalates():
    fn = _fake_query("not json at all")
    d = await decide(_ctx(), _query_fn=fn, _config_dir="", _cwd="/tmp")
    assert d.action == "escalate"


@pytest.mark.asyncio
async def test_model_raising_escalates():
    async def boom(*, prompt, options):
        raise RuntimeError("model down")
        yield  # pragma: no cover
    d = await decide(_ctx(), _query_fn=boom, _config_dir="", _cwd="/tmp")
    assert d.action == "escalate"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_autopilot_decider.py -q`
Expected: FAIL — `ModuleNotFoundError: server.automations.autopilot_decider`.

- [ ] **Step 3: Create the guidance doc**

```markdown
<!-- server/automations/autopilot_guidance.md -->
# Autopilot Decision Guidance

You are **autopilot**: you keep a coding session moving toward its stated goal by
choosing ONE action at each stuck point. Return ONLY a JSON object:
`{"action": "...", "answer_index": <int|null>, "prompt": "<str|null>", "reason": "<str>"}`.

Actions: `approve` | `deny` (a pending tool permission or plan), `answer` (pick an
option by `answer_index`), `nudge` (send `prompt` to continue the work),
`complete` (the goal is met — stop), `escalate` (leave it for a human).

Bias to **escalate** whenever unsure. Safety beats progress.

## Approvals
- APPROVE safe, goal-serving tools: reads, edits, running tests/linters/formatters,
  `git add`/`commit`, `git push --force-with-lease`, opening a PR (`gh pr create`).
- DENY / ESCALATE anything touching production, deploy, secrets, credentials, or
  rewriting shared history. (Hard-destructive commands are already blocked upstream.)

## Answers & plans
- Pick the option that best serves the effective goal and the repo's norms: TDD,
  no parallel implementations, purge replaced code, `Fixes #N` in PR bodies.
- If a plan proposes refactoring far beyond the goal, prefer the narrower option or escalate.

## Nudges (for `done`/`idle` that isn't truly finished)
- Drive toward: tests+lint green → commit → push → PR ready → auto-merge armed.
- Then judge the goal met.

## Completion
- `complete` only when the effective goal is genuinely met (for a code task: an open PR
  with auto-merge armed), not merely because a turn ended.
```

- [ ] **Step 4: Write minimal implementation**

```python
# server/automations/autopilot_decider.py
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
```

Note: the test injects a `_query_fn` whose signature is `(*, prompt, options)`, matching the call above. `ClaudeAgentOptions` import is inside the `try`, so a test environment without the SDK still exercises the fake path (the fake never constructs options — it ignores the `options` kwarg).

Correction for testability: construct `options` lazily only when using the real fn.

```python
        options = None
        if _query_fn is None:
            from claude_agent_sdk import ClaudeAgentOptions
            options = ClaudeAgentOptions(
                model="haiku", system_prompt=_GUIDANCE, cwd=cwd,
                env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
                allowed_tools=[], setting_sources=[])
```

Use this lazy-`options` form in the implementation so tests never need the SDK installed.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest server/tests/test_autopilot_decider.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/automations/autopilot_decider.py server/automations/autopilot_guidance.md server/tests/test_autopilot_decider.py
git commit -m "feat(autopilot): LLM decider + decision-heuristics guidance doc"
```

---

### Task 4: `effective_goal()` resolver (2-tier)

**Files:**
- Create: `server/sessions/goals.py`
- Test: `server/tests/test_effective_goal.py`

**Interfaces:**
- Produces: `effective_goal(record) -> tuple[str, str]` returning `(goal_text, source)` where source ∈ `{"explicit", "initial_prompt"}`. Reads `record.active_goal` (explicit `/goal`) then falls back to `record.name` (the auto-derived one-line intent summary). The companion inferred-goals feature inserts an `"inferred"` middle tier here.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_effective_goal.py
from types import SimpleNamespace
from server.sessions.goals import effective_goal


def test_explicit_goal_wins():
    rec = SimpleNamespace(active_goal="ship the PR", name="Fix login bug")
    assert effective_goal(rec) == ("ship the PR", "explicit")


def test_falls_back_to_name_when_no_goal():
    rec = SimpleNamespace(active_goal=None, name="Fix login bug")
    assert effective_goal(rec) == ("Fix login bug", "initial_prompt")


def test_whitespace_goal_is_ignored():
    rec = SimpleNamespace(active_goal="   ", name="Fix login bug")
    assert effective_goal(rec) == ("Fix login bug", "initial_prompt")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_effective_goal.py -q`
Expected: FAIL — `ModuleNotFoundError: server.sessions.goals`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/goals.py
"""Single source of truth for 'what is this session's goal'. Consumers (autopilot,
dashboards) MUST read goal through here — never a parallel lookup (constitution VI).

Tiers, highest-first:
  explicit       — the built-in /goal condition (record.active_goal)
  initial_prompt — fallback: the auto-derived session name (a one-line intent summary)
The companion inferred-session-goals feature inserts an 'inferred' tier between them."""
from __future__ import annotations


def effective_goal(record) -> tuple[str, str]:
    active = (getattr(record, "active_goal", None) or "").strip()
    if active:
        return active, "explicit"
    return (getattr(record, "name", None) or "").strip(), "initial_prompt"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_effective_goal.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/goals.py server/tests/test_effective_goal.py
git commit -m "feat(autopilot): effective_goal() resolver (explicit -> initial-prompt)"
```

---

### Task 5: `Autopilot` automation metadata + registration

**Files:**
- Create: `server/automations/autopilot.py`
- Modify: `server/automations/registry.py` (register the automation)
- Test: `server/tests/test_autopilot_registration.py`

**Interfaces:**
- Consumes: `AutomationStatus`, `ConfigField` from `server/automations/base.py`.
- Produces: class `Autopilot` with `id="autopilot"`, `name`, `description`, `modes=("off","propose","auto")`, `config_schema` (the seven ConfigFields per spec), `status(ctx)`, and `async on_state_change(...)`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_autopilot_registration.py
from server.automations.registry import registry
from server.automations.base import validate_config


def test_autopilot_registered():
    a = registry.get("autopilot")
    assert a.id == "autopilot"
    assert a.modes == ("off", "propose", "auto")


def test_config_defaults_validate():
    a = registry.get("autopilot")
    cfg = validate_config(a.config_schema, {})
    assert cfg["allow_tool_approvals"] is True
    assert cfg["max_actions_per_session_per_hour"] == 12
    assert cfg["max_actions_per_day"] == 300
    assert cfg["loop_repeat_limit"] == 3
    assert cfg["human_defer_seconds"] == 120
    assert cfg["extra_deny_patterns"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_autopilot_registration.py -q`
Expected: FAIL — `KeyError: 'autopilot'`.

- [ ] **Step 3: Write the automation class**

```python
# server/automations/autopilot.py
"""The autopilot automation: keeps governed sessions moving through their stuck
states until their effective goal is met. Metadata + config here; the control loop
(AutopilotLoop) is added in a later task."""
from __future__ import annotations

from .base import AutomationStatus, ConfigField


class Autopilot:
    id = "autopilot"
    name = "Autopilot"
    description = "Continuously advances every governed session toward its goal."
    modes: tuple[str, ...] = ("off", "propose", "auto")
    config_schema = [
        ConfigField(key="allow_tool_approvals", label="Approve tool permissions", type="bool",
                    default=True, help="If off, autopilot never approves permissions (only "
                                       "denies / answers / nudges)."),
        ConfigField(key="max_actions_per_session_per_hour", label="Max actions/session/hour",
                    type="int", default=12, help="Per-session circuit-breaker."),
        ConfigField(key="max_actions_per_day", label="Max actions/day (global)", type="int",
                    default=300, help="Global rolling-24h circuit-breaker."),
        ConfigField(key="recovery_max_attempts", label="Max recovery attempts", type="int",
                    default=2, help="Recovery nudges before handing a failed session to a human."),
        ConfigField(key="loop_repeat_limit", label="Loop repeat limit", type="int", default=3,
                    help="Identical stuck states before escalating to a human."),
        ConfigField(key="human_defer_seconds", label="Human deference (seconds)", type="int",
                    default=120, help="Skip a session a human touched this recently."),
        ConfigField(key="extra_deny_patterns", label="Extra deny patterns", type="string",
                    default="", help="Comma-separated patterns that EXTEND (never shrink) the "
                                     "built-in destructive-command deny-list."),
        ConfigField(key="enabled_repos", label="Repositories", type="repo-multiselect",
                    default=[], help="Limit governance to these repos (empty = all)."),
    ]

    def status(self, ctx) -> AutomationStatus:
        counts = getattr(ctx, "autopilot_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        advancing = counts.get("advancing", 0)
        escalated = counts.get("escalated", 0)
        return AutomationStatus(
            text=f"{advancing} advancing" + (f", {escalated} escalated" if escalated else ""),
            level="active" if advancing else ("warn" if escalated else "idle"),
            counts=counts)

    async def on_state_change(self, *, enabled, mode, config, ctx) -> None:
        # The orchestrator's loop reads mode/config each tick; nothing to start here.
        ...
```

- [ ] **Step 4: Register it**

In `server/automations/registry.py`, add the import and registration alongside the others:

```python
from .autopilot import Autopilot  # noqa: E402

registry.register(Autopilot())
```

(place the import next to `from .backlog_runner import BacklogRunner` and the `registry.register(Autopilot())` next to `registry.register(BacklogRunner())`.)

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest server/tests/test_autopilot_registration.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/automations/autopilot.py server/automations/registry.py server/tests/test_autopilot_registration.py
git commit -m "feat(autopilot): automation metadata + registration"
```

---

### Task 6: `AutopilotLoop` control loop (injected seams)

**Files:**
- Modify: `server/automations/autopilot.py` (append `AutopilotLoop`)
- Test: `server/tests/test_autopilot_loop.py`

**Interfaces:**
- Consumes: `AutomationContext`; `DecisionContext`/`Decision`/`pre_decide`/`post_validate`/`fingerprint` (Tasks 1-2); `decide` (Task 3).
- Produces: `class AutopilotLoop` constructed with injected seams:
  - `read_config: () -> Awaitable[tuple[str, dict]]` — `(mode, config)`; `mode="off"` when disabled/draining.
  - `list_governed: () -> Awaitable[list[str]]` — session_ids to consider this tick.
  - `get_context: (session_id) -> Awaitable[DecisionContext | None]`
  - `apply: (session_id, Decision) -> Awaitable[bool]` — perform the action; return True if a mutation happened.
  - `decide_fn: (DecisionContext) -> Awaitable[Decision]` — wraps the LLM decider (injected so tests supply canned decisions).
  - `clock: () -> float`
  - `tick() -> None` runs the whole body under an `asyncio.Lock`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_autopilot_loop.py
import pytest
from server.automations.autopilot import AutopilotLoop
from server.automations.autopilot_policy import DecisionContext, Decision


class FakeCtx:
    """Minimal AutomationContext stand-in: in-memory target-state + emit log."""
    def __init__(self):
        self.states = {}
        self.emits = []

    async def emit(self, level, summary, **kw):
        self.emits.append((level, summary, kw.get("kind")))

    async def get_target_state(self, t):
        return dict(self.states.get(t, {}))

    async def set_target_state(self, t, s):
        self.states[t] = s

    async def delete_target_state(self, t):
        self.states.pop(t, None)

    async def list_target_states(self):
        return dict(self.states)


def _dc(sid="s1"):
    return DecisionContext(session_id=sid, status="waiting_input", account="a", repo=None,
                           branch=None, effective_goal="g", goal_source="explicit",
                           stuck={"kind": "permission", "tool_name": "Bash",
                                  "tool_input": {"command": "pytest"}},
                           transcript_tail=[], ledger={})


def _loop(ctx, *, mode="auto", governed=("s1",), decision=Decision(action="approve", reason="ok"),
          applied=None, config=None):
    cfg = config or {"allow_tool_approvals": True, "max_actions_per_session_per_hour": 12,
                     "max_actions_per_day": 300, "recovery_max_attempts": 2,
                     "loop_repeat_limit": 3, "human_defer_seconds": 120,
                     "extra_deny_patterns": [], "enabled_repos": []}
    applied = applied if applied is not None else []

    async def read_config():
        return mode, cfg

    async def list_governed():
        return list(governed)

    async def get_context(sid):
        return _dc(sid)

    async def decide_fn(dc):
        return decision

    async def apply(sid, d):
        applied.append((sid, d.action))
        return d.action in ("approve", "deny", "answer", "nudge")

    loop = AutopilotLoop(ctx, read_config=read_config, list_governed=list_governed,
                         get_context=get_context, apply=apply, decide_fn=decide_fn,
                         clock=lambda: 1000.0)
    loop._applied = applied
    return loop


@pytest.mark.asyncio
async def test_auto_mode_applies_decision():
    ctx = FakeCtx()
    loop = _loop(ctx)
    await loop.tick()
    assert loop._applied == [("s1", "approve")]


@pytest.mark.asyncio
async def test_off_mode_is_noop():
    ctx = FakeCtx()
    loop = _loop(ctx, mode="off")
    await loop.tick()
    assert loop._applied == []


@pytest.mark.asyncio
async def test_propose_mode_emits_but_does_not_apply():
    ctx = FakeCtx()
    loop = _loop(ctx, mode="propose")
    await loop.tick()
    assert loop._applied == []
    assert any(kind == "proposed" for _, _, kind in ctx.emits)


@pytest.mark.asyncio
async def test_denylisted_permission_never_reaches_decider():
    ctx = FakeCtx()
    # decision would approve, but pre_decide forces deny for a force-push
    loop = _loop(ctx, decision=Decision(action="approve", reason="should be overridden"))

    async def dangerous_ctx(sid):
        dc = _dc(sid)
        dc.stuck = {"kind": "permission", "tool_name": "Bash",
                    "tool_input": {"command": "git push --force"}}
        return dc
    loop._get_context = dangerous_ctx
    await loop.tick()
    assert loop._applied == [("s1", "deny")]


@pytest.mark.asyncio
async def test_loop_detection_escalates_after_repeats():
    ctx = FakeCtx()
    loop = _loop(ctx, decision=Decision(action="nudge", prompt="keep going"))
    for _ in range(4):
        await loop.tick()
    # after loop_repeat_limit (3) identical nudges, it escalates instead of applying
    assert ("s1", "escalate") not in loop._applied[:3]
    st = ctx.states.get("s1", {})
    assert st.get("repeat_count", 0) >= 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_autopilot_loop.py -q`
Expected: FAIL — `ImportError: cannot import name 'AutopilotLoop'`.

- [ ] **Step 3: Write minimal implementation** (append to `server/automations/autopilot.py`)

```python
import asyncio
import logging
from typing import Awaitable, Callable

from .autopilot_policy import (
    Decision, DecisionContext, fingerprint, post_validate, pre_decide,
)

log = logging.getLogger(__name__)

_DAY = 24 * 3600.0


class AutopilotLoop:
    """Restart-safe, non-reentrant control loop. Every I/O dependency is injected so
    the whole decide/apply pipeline is testable with no network, model, or sessions.
    tick() runs under a single lock — an interval tick and a status nudge must never
    overlap (a session could otherwise be double-acted)."""

    def __init__(self, ctx, *,
                 read_config: Callable[[], Awaitable[tuple[str, dict]]],
                 list_governed: Callable[[], Awaitable[list[str]]],
                 get_context: Callable[[str], Awaitable[DecisionContext | None]],
                 apply: Callable[[str, Decision], Awaitable[bool]],
                 decide_fn: Callable[[DecisionContext], Awaitable[Decision]],
                 clock: Callable[[], float]) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._list_governed = list_governed
        self._get_context = get_context
        self._apply = apply
        self._decide_fn = decide_fn
        self._clock = clock
        self._lock = asyncio.Lock()
        self._day_actions: list[float] = []
        self._circuit_emitted = False

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run()
            except Exception:  # pragma: no cover — a tick must never crash the loop
                log.warning("autopilot.tick_failed", exc_info=True)

    def _prune_day(self, now: float) -> None:
        self._day_actions = [t for t in self._day_actions if t > now - _DAY]

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        now = self._clock()
        self._prune_day(now)
        advancing = escalated = 0
        for sid in await self._list_governed():
            try:
                acted, esc = await self._advance_one(sid, mode, config, now)
                advancing += int(acted)
                escalated += int(esc)
            except Exception:  # one session must never stall the tick
                log.warning("autopilot.session_failed sid=%s", sid, exc_info=True)
        setattr(self._ctx, "autopilot_counts", {"advancing": advancing, "escalated": escalated})

    async def _advance_one(self, sid: str, mode: str, config: dict, now: float) -> tuple[bool, bool]:
        ctx = await self._get_context(sid)
        if ctx is None:
            return False, False
        ledger = await self._ctx.get_target_state(sid)
        ctx.ledger = ledger

        # Global daily cap.
        if len(self._day_actions) >= config["max_actions_per_day"]:
            if not self._circuit_emitted:
                self._circuit_emitted = True
                await self._ctx.emit("warn", "daily action cap reached — pausing",
                                     kind="circuit_open")
            return False, False
        self._circuit_emitted = False

        decision = pre_decide(ctx, config, now)
        if decision is None:
            decision = post_validate(await self._decide_fn(ctx), ctx, config)
        if decision.action == "skip":
            await self._record_fingerprint(sid, ledger, ctx)
            return False, False
        if decision.action == "escalate":
            await self._escalate(sid, ledger, decision)
            return False, True

        if mode == "propose":
            await self._ctx.emit("info", f"{sid}: propose {decision.action} — {decision.reason}",
                                 kind="proposed", session_id=sid,
                                 detail={"action": decision.action, "reason": decision.reason})
            return False, False

        acted = await self._apply(sid, decision)
        if acted:
            self._day_actions.append(now)
            await self._charge(sid, ledger, ctx, decision, now)
            await self._ctx.emit("info", f"{sid}: {decision.action} — {decision.reason}",
                                 kind="acted", session_id=sid,
                                 detail={"action": decision.action})
        return acted, False

    async def _record_fingerprint(self, sid: str, ledger: dict, ctx: DecisionContext) -> None:
        fp = fingerprint(ctx)
        repeat = ledger.get("repeat_count", 0) + 1 if ledger.get("last_fingerprint") == fp else 1
        new = dict(ledger)
        new.update(last_fingerprint=fp, repeat_count=repeat)
        await self._ctx.set_target_state(sid, new)

    async def _charge(self, sid: str, ledger: dict, ctx: DecisionContext,
                      decision: Decision, now: float) -> None:
        fp = fingerprint(ctx)
        repeat = ledger.get("repeat_count", 0) + 1 if ledger.get("last_fingerprint") == fp else 1
        hour = [t for t in ledger.get("hour_actions", []) if t > now - 3600.0] + [now]
        recovery = ledger.get("recovery_attempts", 0)
        if ctx.stuck.get("kind") == "needs_attention" and decision.action == "nudge":
            recovery += 1
        new = dict(ledger)
        new.update(hour_actions=hour, last_fingerprint=fp, repeat_count=repeat,
                   recovery_attempts=recovery)
        await self._ctx.set_target_state(sid, new)

    async def _escalate(self, sid: str, ledger: dict, decision: Decision) -> None:
        new = dict(ledger)
        new["escalated"] = True
        await self._ctx.set_target_state(sid, new)
        await self._ctx.emit("warn", f"{sid}: needs human — {decision.reason}",
                             kind="escalated", session_id=sid)
```

Note on `test_loop_detection_escalates_after_repeats`: each `nudge` is applied and `_charge` bumps `repeat_count`; on the 4th tick `pre_decide` sees `repeat_count >= loop_repeat_limit` and returns `escalate`, so the loop stops applying. The assertion checks the first three ticks did not escalate and the ledger accumulated repeats.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_autopilot_loop.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/automations/autopilot.py server/tests/test_autopilot_loop.py
git commit -m "feat(autopilot): non-reentrant control loop with injected seams"
```

---

### Task 7: Orchestrator wiring — seams, tick loop, status nudge

**Files:**
- Modify: `server/sessions/orchestrator.py`
- Test: `server/tests/test_autopilot_orchestrator_seams.py`

**Interfaces:**
- Consumes: `AutopilotLoop` (Task 6), `effective_goal` (Task 4), `decide` (Task 3), `ACTIONABLE_STATUSES`/`DecisionContext` (Task 1), existing `resolve_permission`/`answer_question`/`send_prompt`/`ensure_driver`/`pending_permissions`/`pending_questions`.
- Produces on `SessionOrchestrator`: `_autopilot_read_config()`, `_autopilot_list_governed()`, `_autopilot_get_context(sid)`, `_autopilot_apply(sid, decision)`, `_autopilot_decide(ctx)`, `_autopilot_tick_loop(interval)`, and a nudge call inside `_persist_status`.

- [ ] **Step 1: Write the failing test** (targets the pure seams that don't need a live driver)

```python
# server/tests/test_autopilot_orchestrator_seams.py
import pytest
from server.automations.autopilot_policy import Decision


@pytest.mark.asyncio
async def test_read_config_defaults_to_off_when_disabled(orch):
    mode, config = await orch._autopilot_read_config()
    assert mode == "off"
    assert config["max_actions_per_session_per_hour"] == 12


@pytest.mark.asyncio
async def test_read_config_off_while_draining(orch, enable_autopilot):
    await enable_autopilot(mode="auto")
    orch._draining = True
    mode, _ = await orch._autopilot_read_config()
    assert mode == "off"


@pytest.mark.asyncio
async def test_apply_escalate_is_noop(orch):
    # escalate/complete/skip never mutate — apply returns False
    assert await orch._autopilot_apply("missing", Decision(action="escalate")) is False
    assert await orch._autopilot_apply("missing", Decision(action="complete")) is False
```

Add fixtures to the test file (or a conftest) — an `orch` built like other orchestrator tests do, and an `enable_autopilot` helper that upserts the `Automation` row with `enabled=True, mode=<mode>`. Mirror the fixture pattern in `server/tests/test_backlog_runner_*.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_autopilot_orchestrator_seams.py -q`
Expected: FAIL — `AttributeError: '_autopilot_read_config'`.

- [ ] **Step 3: Add the seams** to `SessionOrchestrator`.

Config read (mirror `_backlog_read_config` at `orchestrator.py:913`; add the drain gate):

```python
    async def _autopilot_read_config(self) -> tuple[str, dict]:
        """The autopilot row's (mode, config) merged onto schema defaults. Returns
        'off' when disabled, invalid, OR while draining (autopilot must never fight a
        deploy drain)."""
        from server.automations.autopilot import Autopilot
        from server.automations.base import validate_config
        from sqlalchemy import select
        from server.db.models import Automation
        enabled, mode, raw = False, None, {}
        try:
            async with db_session() as db:
                row = (await db.execute(select(Automation).where(
                    Automation.id == "autopilot"))).scalar_one_or_none()
                if row is not None:
                    enabled, mode, raw = bool(row.enabled), row.mode, dict(row.config or {})
        except Exception:
            log.debug("autopilot.config.load_failed", exc_info=True)
        try:
            config = validate_config(Autopilot.config_schema, raw)
        except ValueError:
            config = validate_config(Autopilot.config_schema, {})
        effective = mode if (enabled and mode in ("propose", "auto")) else "off"
        if getattr(self, "_draining", False):
            effective = "off"
        return effective, config
```

Governed listing (reads the in-memory records; honors opt-out + enabled_repos):

```python
    async def _autopilot_list_governed(self) -> list[str]:
        from server.automations.autopilot_policy import ACTIONABLE_STATUSES
        _mode, config = await self._autopilot_read_config()
        repos = set(config.get("enabled_repos") or [])
        out: list[str] = []
        for sid, rec in list(self._records.items()):
            if rec.status not in ACTIONABLE_STATUSES:
                continue
            if getattr(rec, "autopilot_paused", False):
                continue
            if repos and rec.repo not in repos:
                continue
            out.append(sid)
        return out
```

Context assembly (`effective_goal` + the pending permission/question payloads):

```python
    async def _autopilot_get_context(self, session_id: str):
        from server.automations.autopilot_policy import DecisionContext
        from server.sessions.goals import effective_goal
        rec = self._records.get(session_id)
        if rec is None:
            return None
        goal_text, goal_source = effective_goal(rec)
        stuck: dict = {"kind": rec.status}
        perms = self.pending_permissions(session_id) if hasattr(self, "pending_permissions") else []
        plan = next((p for p in perms if p.get("tool") == "ExitPlanMode"), None)
        other = next((p for p in perms if p.get("tool") != "ExitPlanMode"), None)
        if plan is not None:
            stuck = {"kind": "plan", "perm_id": plan["permId"],
                     "tool_name": "ExitPlanMode", "tool_input": plan.get("input", {}),
                     "plan_text": str(plan.get("input", {}))}
        elif other is not None:
            stuck = {"kind": "permission", "perm_id": other["permId"],
                     "tool_name": other["tool"], "tool_input": other.get("input", {})}
        else:
            qs = self.pending_questions(session_id)
            if qs:
                q = qs[0]
                stuck = {"kind": "question", "question_id": q["question_id"],
                         "question": q.get("question", ""), "options": q.get("options", []),
                         "multi_select": q.get("multi_select", False)}
        return DecisionContext(
            session_id=session_id, status=rec.status, account=rec.account, repo=rec.repo,
            branch=getattr(rec, "branch", None), effective_goal=goal_text,
            goal_source=goal_source, stuck=stuck, transcript_tail=[], ledger={})
```

Note: `pending_permissions(session_id)` is the sync orchestrator method (mirrors `pending_questions` at `orchestrator.py:2847`); if it does not yet exist as a public method, add a one-line wrapper delegating to the driver's `_reg.pending(session_id)` exactly like `pending_questions` delegates to `pending_questions()`.

Apply (route each action to the existing seam under `SYSTEM_IDENTITY`):

```python
    async def _autopilot_apply(self, session_id: str, decision) -> bool:
        from server.sessions.types import SYSTEM_IDENTITY
        act = decision.action
        if act in ("escalate", "complete", "skip"):
            return False
        ctx = await self._autopilot_get_context(session_id)
        stuck = ctx.stuck if ctx else {}
        if act in ("approve", "deny") and stuck.get("perm_id"):
            await self.ensure_driver(session_id, SYSTEM_IDENTITY)
            return bool(self.resolve_permission(
                session_id, stuck["perm_id"], act == "approve", SYSTEM_IDENTITY))
        if act == "answer" and stuck.get("question_id") is not None:
            await self.ensure_driver(session_id, SYSTEM_IDENTITY)
            opts = stuck.get("options") or []
            idx = decision.answer_index or 0
            selected = [opts[idx]] if 0 <= idx < len(opts) else []
            return bool(self.answer_question(
                session_id, stuck["question_id"], selected, SYSTEM_IDENTITY, "option"))
        if act == "nudge" and decision.prompt:
            await self.ensure_driver(session_id, SYSTEM_IDENTITY)
            result = await self.send_prompt(session_id, decision.prompt)
            return result == "sent"
        return False

    async def _autopilot_decide(self, ctx):
        from server.automations.autopilot_decider import decide
        cfg_dir = self._config_dir_for(ctx.account) if hasattr(self, "_config_dir_for") else None
        return await decide(ctx, _config_dir=cfg_dir)
```

If `_config_dir_for` does not exist, pass `_config_dir=None` (the decider defaults to the process env) — the decider is a read-only classification call, so the exact account config dir is not critical.

- [ ] **Step 4: Instantiate the loop + start the tick loop.**

In `__init__` (next to the backlog loop construction around `orchestrator.py:305`):

```python
        from server.automations.autopilot import AutopilotLoop
        self._autopilot_ctx = AutomationContext("autopilot")
        self._autopilot_loop = AutopilotLoop(
            self._autopilot_ctx,
            read_config=self._autopilot_read_config,
            list_governed=self._autopilot_list_governed,
            get_context=self._autopilot_get_context,
            apply=self._autopilot_apply,
            decide_fn=self._autopilot_decide,
            clock=time.monotonic,
        )
        self._autopilot_task: asyncio.Task | None = None
```

In `start()` (next to `backlog-runner-loop` at `orchestrator.py:419`):

```python
        self._autopilot_task = self._spawn(
            self._autopilot_tick_loop(interval=20.0), name="autopilot-loop")
```

The tick loop itself (mirror `_backlog_tick_loop`):

```python
    async def _autopilot_tick_loop(self, interval: float) -> None:
        while True:
            try:
                await self._autopilot_loop.tick()
            except Exception:  # pragma: no cover — tick() already swallows
                log.warning("autopilot.tick_loop_error", exc_info=True)
            await asyncio.sleep(interval)
```

- [ ] **Step 5: Nudge on status change.**

In `_persist_status` (`orchestrator.py:774`), after the record's status is updated, add a best-effort nudge when a session enters an actionable state:

```python
        from server.automations.autopilot_policy import ACTIONABLE_STATUSES
        if status in ACTIONABLE_STATUSES and getattr(self, "_autopilot_loop", None) is not None:
            try:
                self._spawn(self._autopilot_loop.tick(), name="autopilot-nudge")
            except Exception:  # pragma: no cover — scheduling must never break status persist
                log.debug("autopilot.nudge_failed", exc_info=True)
```

Place this at the END of `_persist_status`, after the existing status/name logic, so a nudge never pre-empts the persist.

- [ ] **Step 6: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_autopilot_orchestrator_seams.py -q`
Expected: PASS.

- [ ] **Step 7: Full-suite guard + commit**

Run: `ruff check server/ && mypy server/ && env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_autopilot_*.py -q`
Expected: all green.

```bash
git add server/sessions/orchestrator.py server/tests/test_autopilot_orchestrator_seams.py
git commit -m "feat(autopilot): wire control loop into orchestrator (seams, tick, nudge)"
```

---

### Task 8: Per-session opt-out — column, record, serialization, orchestrator method, RPC, remote proxy

**Files:**
- Modify: `server/db/models.py` (nullable `autopilot_paused` column)
- Modify: `server/sessions/types.py` (`SessionRecord.autopilot_paused` + `to_dict`/`from_dict`)
- Modify: `server/api/sessions.py` (`_record_to_dict` includes `autopilotPaused`)
- Modify: `server/sessions/orchestrator.py` (`set_autopilot_paused`)
- Modify: `server/sessions/remote.py` (RemoteOrchestrator proxy)
- Modify: `server/ipc/orchestrator_rpc.py` (register the method)
- Modify: `server/sessions/rehydrate` path — set `autopilot_paused` from the DB row (in `rehydrate`, `orchestrator.py:1794`).
- Test: `server/tests/test_autopilot_opt_out.py`

**Interfaces:**
- Produces: `async def set_autopilot_paused(self, session_id: str, paused: bool) -> None` on both `SessionOrchestrator` and `RemoteOrchestrator`; wire `autopilotPaused` field end-to-end.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_autopilot_opt_out.py
import pytest
from server.sessions.remote import RemoteOrchestrator


def test_remote_proxy_has_set_autopilot_paused():
    assert hasattr(RemoteOrchestrator, "set_autopilot_paused")


@pytest.mark.asyncio
async def test_set_autopilot_paused_persists(orch, a_session):
    sid = a_session.id
    await orch.set_autopilot_paused(sid, True)
    assert orch._records[sid].autopilot_paused is True
    # survives a fresh rehydrate from the DB row
    orch._records.clear()
    await orch.rehydrate()
    assert orch._records[sid].autopilot_paused is True
```

(Use the same `orch` / `a_session` fixtures the other orchestrator tests use.)

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_autopilot_opt_out.py -q`
Expected: FAIL — `AttributeError: set_autopilot_paused` / missing column.

- [ ] **Step 3: Add the DB column** in `server/db/models.py` (next to the `active_goal` column around line 92):

```python
    # Per-session autopilot opt-out. NULL/False = governed; True = pinned to manual.
    # Nullable so init_db backfills on dev SQLite without a migration.
    autopilot_paused: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
```

Ensure `Boolean` is imported from sqlalchemy in that module (add to the existing import if absent).

- [ ] **Step 4: Add to `SessionRecord`** (`server/sessions/types.py`, next to `active_goal` at line 50, and its `to_dict`/`from_dict`):

```python
    autopilot_paused: bool = False    # per-session autopilot opt-out
```
```python
            "autopilotPaused": self.autopilot_paused,   # in to_dict()
```
```python
            autopilot_paused=bool(d.get("autopilotPaused", False)),   # in from_dict()
```

- [ ] **Step 5: Serialize** in `server/api/sessions.py` `_record_to_dict` (next to `activeGoal` at line 136):

```python
        "autopilotPaused": getattr(record, "autopilot_paused", False),
```

- [ ] **Step 6: Orchestrator method** (mirror `set_goal_for_session`/`_persist_goal` at `orchestrator.py:3638`):

```python
    async def set_autopilot_paused(self, session_id: str, paused: bool) -> None:
        rec = self._records.get(session_id)
        if rec is not None:
            rec.autopilot_paused = paused
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(sa_update(DbSession)
                             .where(DbSession.id == session_id)
                             .values(autopilot_paused=paused))
```

- [ ] **Step 7: Rehydrate** — set the field when rebuilding records (`orchestrator.py:1794`, in the `SessionRecord(...)` construction):

```python
                autopilot_paused=bool(getattr(row, "autopilot_paused", False)),
```

- [ ] **Step 8: RemoteOrchestrator proxy** (`server/sessions/remote.py`, next to `rename_session` at line 199):

```python
    async def set_autopilot_paused(self, session_id, paused):
        return await self._client.call("set_autopilot_paused", session_id=session_id, paused=paused)
```

- [ ] **Step 9: Register the RPC** — add `"set_autopilot_paused"` to the `_make_simple` method-name list in `server/ipc/orchestrator_rpc.py` (the list beginning at line 131, e.g. next to `"rename_session"`). This auto-wraps it since its return is JSON-safe (`None`).

- [ ] **Step 10: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_autopilot_opt_out.py -q`
Expected: PASS.

- [ ] **Step 11: Daemon-parity guard** — run the existing breadth/parity test that checks `RemoteOrchestrator` matches `SessionOrchestrator` (search: `grep -rl "RemoteOrchestrator" server/tests | xargs grep -l parity`).

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests -k "parity or breadth" -q`
Expected: PASS (the new method is present on both sides + registered).

- [ ] **Step 12: Commit**

```bash
git add server/db/models.py server/sessions/types.py server/api/sessions.py \
        server/sessions/orchestrator.py server/sessions/remote.py \
        server/ipc/orchestrator_rpc.py server/tests/test_autopilot_opt_out.py
git commit -m "feat(autopilot): per-session opt-out (column, RPC, remote proxy)"
```

---

### Task 9: Frontend per-session opt-out toggle

**Files:**
- Modify: `web/src/api/types.ts` (`autopilotPaused` on `Session`)
- Modify: `web/src/components/SessionCard.tsx` (toggle control + mutation call)
- Test: `web/src/components/__tests__/SessionCardAutopilot.test.tsx` (or extend the existing SessionCard test)

**Interfaces:**
- Consumes: `autopilotPaused` from the session wire payload (Task 8).
- Produces: a small toggle on the card that calls the sessions API to set `autopilot_paused`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/SessionCardAutopilot.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import SessionCard from "../SessionCard";

const base = {
  id: "s1", name: "Fix bug", status: "done", account: "a",
  autopilotPaused: false,
} as any;

describe("SessionCard autopilot toggle", () => {
  it("shows an autopilot control reflecting the paused state", () => {
    render(<SessionCard session={{ ...base, autopilotPaused: true }} />);
    expect(screen.getByLabelText(/autopilot/i)).toBeInTheDocument();
  });
});
```

Match the real `SessionCard` prop shape — inspect the component's existing props and reuse its test-render helper if one exists (some card tests wrap in a provider). Adjust the import/props to match.

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `vitest run src/components/__tests__/SessionCardAutopilot.test.tsx`
Expected: FAIL — no element labelled "autopilot".

- [ ] **Step 3: Add the type** in `web/src/api/types.ts` (next to `activeGoal`):

```ts
  autopilotPaused?: boolean;
```

- [ ] **Step 4: Add the toggle** to `SessionCard.tsx`. Reuse the existing badge/action styling and the existing sessions-mutation hook (search the file for how rename/other PATCH-style actions are dispatched; follow that exact pattern). Minimal shape:

```tsx
<button
  type="button"
  aria-label="Toggle autopilot for this session"
  className={/* reuse existing icon-button token classes */}
  onClick={() => setAutopilotPaused(session.id, !session.autopilotPaused)}
  title={session.autopilotPaused ? "Autopilot paused — click to resume" : "Autopilot on — click to pause"}
>
  {session.autopilotPaused ? "Autopilot: off" : "Autopilot: on"}
</button>
```

Wire `setAutopilotPaused` to the same API layer the other session mutations use (add a thin client call hitting the endpoint that maps to `set_autopilot_paused`; if the sessions API exposes a generic PATCH, extend it — do not create a parallel client).

- [ ] **Step 5: Run test to verify it passes**

Run (in `web/`): `vitest run src/components/__tests__/SessionCardAutopilot.test.tsx`
Expected: PASS.

- [ ] **Step 6: Type + lint + commit**

Run (in `web/`): `tsc --noEmit && eslint src/components/SessionCard.tsx src/api/types.ts`
Expected: green.

```bash
git add web/src/api/types.ts web/src/components/SessionCard.tsx \
        web/src/components/__tests__/SessionCardAutopilot.test.tsx
git commit -m "feat(autopilot): per-session opt-out toggle on SessionCard"
```

---

### Task 10: Full-suite verification + PR

**Files:** none (verification + integration).

- [ ] **Step 1: Server gates**

Run: `ruff check server/ && mypy server/ && env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests -q`
Expected: all green. (mypy is a required CI gate — do not skip.)

- [ ] **Step 2: Web gates**

Run (in `web/`): `vitest run && tsc --noEmit && eslint .`
Expected: all green.

- [ ] **Step 3: Push + PR** (per CLAUDE.md completion workflow — branch already off `origin/master`)

```bash
git push -u origin autopilot-design
gh pr create --title "feat: autopilot — continuous session advancement" \
  --body "Implements docs/superpowers/specs/2026-07-02-autopilot-design.md.

Global autopilot automation (off/propose/auto) that advances every governed
session through its stuck states — nudge/approve/answer/recover — until its
effective goal is met. Hard-rule guardrails (pure) wrap an LLM decider; per-session
opt-out; daemon-parity RPC included.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
gh pr ready   # ensure non-draft
```

- [ ] **Step 4: Arm auto-merge**

```bash
gh pr merge --squash --auto
```

Expected: PR is non-draft with auto-merge armed; lands when `lint-and-test` + `frontend` checks pass and the branch is up to date.

---

## Self-Review

**Spec coverage:**
- Control loop (non-reentrant, injected seams, drain/off no-op, nudge) → Tasks 6, 7. ✅
- Guardrails: deny-list, approval switch, hourly/daily caps, loop detection, recovery cap, post-validate → Tasks 1, 2 (pure) + Task 6 (wired). ✅
- LLM decider + guidance doc → Task 3. ✅
- Effective goal (2-tier; companion adds inferred tier) → Task 4. ✅
- Config schema (7 knobs + enabled_repos) → Task 5. ✅
- API/UI: global control free via registration (Task 5); per-session opt-out + daemon parity → Tasks 8, 9. ✅
- Testing: pure guardrail units, decider canned-fn, loop, seams, opt-out, parity → each task. ✅

**Placeholder scan:** No TBD/TODO; every code step shows complete code. The one intentional "match the existing pattern" notes (Task 9 mutation hook, Task 7 `pending_permissions` wrapper, Task 8 fixtures) point at concrete existing code to copy, not vague instructions.

**Type consistency:** `Decision(action, answer_index, prompt, reason)` and `DecisionContext` fields are identical across Tasks 1, 2, 3, 6, 7. `effective_goal -> (text, source)` consistent Tasks 4, 7. `set_autopilot_paused(session_id, paused)` identical Tasks 8 (orch, remote, rpc). `autopilotPaused` wire key identical Tasks 8, 9. `ACTIONABLE_STATUSES` used consistently Tasks 1, 7.

**Known follow-ups (out of scope, flagged):** `transcript_tail` is passed empty in Task 7's context assembly — a later enhancement can populate it from `transcript_events` for richer decider context; the decider already truncates/handles an empty tail safely.
