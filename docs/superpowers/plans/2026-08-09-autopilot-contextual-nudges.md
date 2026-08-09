# Autopilot Contextual Nudges Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Autopilot never sends canned nudge text — every nudge is composed by a Sonnet-tier model from rich session context, wrong-state nudges are verified against GitHub first, and repetition is deterministically suppressed.

**Architecture:** Two-tier decider: the existing Haiku gate keeps choosing the action; a new `nudge_composer` (Sonnet) writes every nudge from a ~10× larger transcript window plus verified PR facts and prior-nudge history. `post_validate`'s completion-gate keeps its verdict authority but returns a composition-marked decision instead of a template. The loop gains two injected seams (`refresh_facts`, `compose`) so it stays pure/testable.

**Tech Stack:** Python 3 / asyncio, `claude_agent_sdk.query` one-shot pattern (injected `_query_fn` in tests), pytest.

**Spec:** `docs/superpowers/specs/2026-08-09-autopilot-contextual-nudges-design.md`

## Global Constraints

- Constitution: TDD (V), single source of truth / purge replaced code in the same PR (VI).
- No canned nudge text may reach a session on ANY path — failure modes land on `escalate`.
- The real model is NEVER called in tests: every model call goes through an injected `_query_fn` (house pattern from `autopilot_decider.py`).
- Dedupe threshold: token-set (Jaccard) ratio ≥ **0.85** = near-identical; window **6 h**; nudge history cap **5**.
- On-demand PR refresh: at most once per session per **600 s**; skipped while the poller is in failure cooldown.
- Composer model: `"sonnet"`; rich tail: `turns=20, max_entries=40, max_chars=1500, tool_chars=400`.
- Test invocation in this worktree (no venv): `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest …`
- Lint gates before finishing: `ruff check server/ src/` and `mypy server/ --ignore-missing-imports` (CI-exact).
- Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Work happens on branch `autopilot-contextual-nudges` (already exists, based on `origin/master`). Verify with `git branch --show-current` before every commit.

---

### Task 1: Completion-gate loses its voice (Decision metadata + post_validate)

**Files:**
- Modify: `server/automations/autopilot_policy.py` (Decision dataclass ~line 71; post_validate ~line 168)
- Test: `server/tests/test_autopilot_policy_rules.py`

**Interfaces:**
- Consumes: existing `Decision`, `post_validate`, `completion_bar.met_bar`.
- Produces (later tasks rely on these exact names):
  - `Decision` fields: `needs_composition: bool = False`, `downgraded_from: str | None = None`, `verdict_reason: str | None = None`, `verdict_remedy: str | None = None`.
  - `post_validate` completion-gate branch returns `Decision(action="nudge", prompt=None, needs_composition=True, downgraded_from="complete", verdict_reason=verdict.reason, verdict_remedy=verdict.remedy, reason=f"completion-gate: {verdict.reason}")`.
  - The `empty nudge → escalate` rule is REMOVED from `post_validate` (the loop escalates on composer failure instead — Task 6).

- [ ] **Step 1: Write the failing tests**

In `server/tests/test_autopilot_policy_rules.py`, update the completion-gate tests and add the new contract. Replace `test_post_validate_rejects_empty_nudge` (the rule moves to the loop) with:

```python
def test_post_validate_gate_nudge_carries_no_canned_text():
    # A gate downgrade must NEVER fabricate nudge text — it marks the decision
    # for composition and carries the bar verdict as structured context.
    ctx = _ctx_with_facts(open_unarmed_pr())   # reuse the file's existing helpers
    d = post_validate(Decision(action="complete", reason="agent said done"), ctx, CONFIG)
    assert d.action == "nudge"
    assert d.prompt is None
    assert d.needs_composition is True
    assert d.downgraded_from == "complete"
    assert d.verdict_reason and "not armed" in d.verdict_reason
    assert d.verdict_remedy and "gh pr ready" in d.verdict_remedy


def test_post_validate_passes_gate_nudge_through_unmarked():
    # A decider-authored nudge is not a downgrade: no composition marker forced here
    # (the loop composes all nudges anyway); its prompt survives as the intent hint.
    d = post_validate(Decision(action="nudge", prompt="fix CI"), _ctx(status="done"), CONFIG)
    assert d.action == "nudge" and d.prompt == "fix CI" and d.downgraded_from is None


def test_post_validate_empty_nudge_no_longer_escalates_here():
    # The empty-nudge rule moved to the loop (composer failure -> escalate there).
    d = post_validate(Decision(action="nudge", prompt="  "), _ctx(status="done"), CONFIG)
    assert d.action == "nudge"
```

Adapt the existing downgrade assertions (`test_complete_with_unarmed_pr_is_downgraded_to_nudge`, `test_complete_code_goal_no_pr_is_downgraded_to_nudge`, `test_complete_unarmed_pr_regression_guard`, `test_complete_downgraded_without_resolution`): wherever they assert on `d.prompt` text, assert instead `d.prompt is None and d.needs_composition and d.verdict_remedy`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_autopilot_policy_rules.py -q`
Expected: FAIL — `Decision` has no `needs_composition` field / old canned-prompt asserts.

- [ ] **Step 3: Implement**

In `autopilot_policy.py`:

```python
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
```

In `post_validate`: delete the `if decision.action == "nudge" and not (decision.prompt or "").strip():` block, and change the completion-gate `if not verdict.met:` branch to:

```python
        if not verdict.met:
            return Decision(action="nudge", prompt=None, needs_composition=True,
                            downgraded_from="complete",
                            verdict_reason=verdict.reason, verdict_remedy=verdict.remedy,
                            reason=f"completion-gate: {verdict.reason}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_autopilot_policy_rules.py server/tests/test_autopilot_loop.py -q`
Expected: PASS (loop tests still pass — nothing consumes the new fields yet).

- [ ] **Step 5: Commit**

```bash
git add server/automations/autopilot_policy.py server/tests/test_autopilot_policy_rules.py
git commit -m "feat(autopilot): completion-gate marks nudges for composition instead of canned text

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Pure helpers — contradiction detection + near-duplicate dedupe

**Files:**
- Modify: `server/automations/autopilot_policy.py` (append after `fingerprint`)
- Test: `server/tests/test_autopilot_policy_rules.py`

**Interfaces:**
- Produces (exact signatures, used by Task 6):
  - `NUDGE_DEDUPE_WINDOW_S = 6 * 3600.0`, `NUDGE_DEDUPE_THRESHOLD = 0.85`, `RECENT_NUDGES_CAP = 5`, `FACTS_REFRESH_MIN_INTERVAL_S = 600.0` (module constants)
  - `claimed_pr_numbers(transcript_tail: list[dict]) -> set[int]`
  - `facts_contradicted(ctx: DecisionContext) -> bool`
  - `near_duplicate(text: str, recent: list[dict], now: float, *, window_s: float = NUDGE_DEDUPE_WINDOW_S, threshold: float = NUDGE_DEDUPE_THRESHOLD) -> bool` where `recent` entries are `{"ts": float, "text": str}` (ts on the loop's clock).

- [ ] **Step 1: Write the failing tests**

```python
# --- contradiction + dedupe helpers (spec §2, §4) ---
from server.automations.autopilot_policy import (
    claimed_pr_numbers, facts_contradicted, near_duplicate,
)


def test_claimed_pr_numbers_scans_tail_text():
    tail = [{"role": "assistant", "text": "PR #13393 is armed, see also PR#12"},
            {"role": "tool", "text": "gh pr view 99"}]
    assert claimed_pr_numbers(tail) == {13393, 12}


def test_facts_contradicted_when_tail_claims_unknown_pr():
    ctx = _ctx_with_facts(no_prs())          # has_repo=True, prs=()
    ctx.transcript_tail = [{"role": "assistant", "text": "PR #42 is armed"}]
    assert facts_contradicted(ctx) is True


def test_facts_contradicted_on_complete_status_without_armed_pr():
    ctx = _ctx_with_facts(no_prs())
    ctx.status = "complete"
    assert facts_contradicted(ctx) is True


def test_facts_not_contradicted_when_armed_pr_present():
    ctx = _ctx_with_facts(armed_pr())
    ctx.status = "complete"
    assert facts_contradicted(ctx) is False


def test_facts_not_contradicted_without_facts_or_repo():
    ctx = _ctx(status="complete")            # pr_facts is None
    assert facts_contradicted(ctx) is False


def test_near_duplicate_boundary():
    recent = [{"ts": 1000.0, "text": "fix the failing test in test_ws.py then arm the PR"}]
    same = "fix the failing test in test_ws.py then arm the PR"
    assert near_duplicate(same, recent, now=2000.0) is True
    fresh = "PR #99 conflicts with master; rebase onto origin/master and resolve server/app.py"
    assert near_duplicate(fresh, recent, now=2000.0) is False


def test_near_duplicate_outside_window_is_fresh():
    recent = [{"ts": 0.0, "text": "open a PR and arm auto-merge"}]
    assert near_duplicate("open a PR and arm auto-merge", recent,
                          now=7 * 3600.0) is False
```

Helper factories `no_prs()` / `armed_pr()` / `_ctx_with_facts(...)`: reuse or extend the file's existing PrFacts helpers (they exist for the completion-gate tests around line 92; follow their construction style).

- [ ] **Step 2: Run to verify failure**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_autopilot_policy_rules.py -q`
Expected: FAIL with ImportError (`claimed_pr_numbers` not defined).

- [ ] **Step 3: Implement in `autopilot_policy.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_autopilot_policy_rules.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/automations/autopilot_policy.py server/tests/test_autopilot_policy_rules.py
git commit -m "feat(autopilot): contradiction detection + near-duplicate nudge dedupe helpers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: The Sonnet nudge composer

**Files:**
- Create: `server/automations/nudge_composer.py`
- Create: `server/automations/nudge_composer_guidance.md`
- Test: `server/tests/test_nudge_composer.py`

**Interfaces:**
- Consumes: `DecisionContext` (fields: `effective_goal`, `goal_source`, `status`, `stuck`, `pr_facts`, `reversal_hint`, `fleet_context`), `PrFacts.to_prompt()`.
- Produces (exact signature, used by Task 5's orchestrator wrapper):

```python
async def compose_nudge(
    ctx,                       # DecisionContext
    *,
    intent_hint: str | None,       # the gate's one-line prompt field
    verdict_reason: str | None,    # from Decision.verdict_reason
    verdict_remedy: str | None,    # from Decision.verdict_remedy
    recent_nudges: list[str],      # texts only, newest last
    rich_tail: list[dict],         # large transcript window
    facts_verified: bool,          # True when pr_facts was just refreshed from GitHub
    _query_fn=None, _config_dir=None, _cwd=None,
) -> str | None
```

Returns the nudge text, or `None` when the model declined (`{"escalate": true}`), replied unparseably, or errored. **There is no template fallback** — `None` means the caller escalates.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_nudge_composer.py
"""Composer contract: injected query_fn only (the real model is never called),
None on decline/garbage/exception — never a canned string."""
import pytest
from server.automations.autopilot_policy import DecisionContext
from server.automations.nudge_composer import compose_nudge


def _ctx():
    return DecisionContext(
        session_id="s1", status="done", account="a", repo="o/r", branch="b",
        effective_goal="ship the parser fix", goal_source="inferred",
        stuck={"kind": "done"}, transcript_tail=[])


def _fake_query(reply: str):
    async def query_fn(*, prompt, options=None):
        class Block:
            text = reply
        class Msg:
            content = [Block()]
        yield Msg()
    return query_fn


@pytest.mark.asyncio
async def test_composes_nudge_from_json_reply():
    out = await compose_nudge(
        _ctx(), intent_hint="fix CI", verdict_reason=None, verdict_remedy=None,
        recent_nudges=[], rich_tail=[], facts_verified=False,
        _query_fn=_fake_query('{"nudge": "Fix the 2 failing tests in test_ws.py, then rerun pytest."}'))
    assert out == "Fix the 2 failing tests in test_ws.py, then rerun pytest."


@pytest.mark.asyncio
async def test_escalate_reply_returns_none():
    out = await compose_nudge(
        _ctx(), intent_hint=None, verdict_reason=None, verdict_remedy=None,
        recent_nudges=[], rich_tail=[], facts_verified=False,
        _query_fn=_fake_query('{"escalate": true, "reason": "nothing concrete to say"}'))
    assert out is None


@pytest.mark.asyncio
async def test_prose_reply_with_embedded_json_still_parses():
    out = await compose_nudge(
        _ctx(), intent_hint=None, verdict_reason=None, verdict_remedy=None,
        recent_nudges=[], rich_tail=[], facts_verified=False,
        _query_fn=_fake_query('Sure! {"nudge": "Rebase onto origin/master."}'))
    assert out == "Rebase onto origin/master."


@pytest.mark.asyncio
async def test_garbage_reply_returns_none_not_a_template():
    out = await compose_nudge(
        _ctx(), intent_hint=None, verdict_reason=None, verdict_remedy=None,
        recent_nudges=[], rich_tail=[], facts_verified=False,
        _query_fn=_fake_query("I think you should keep going."))
    assert out is None


@pytest.mark.asyncio
async def test_query_exception_returns_none():
    async def boom(*, prompt, options=None):
        raise RuntimeError("model down")
        yield  # pragma: no cover
    out = await compose_nudge(
        _ctx(), intent_hint=None, verdict_reason=None, verdict_remedy=None,
        recent_nudges=[], rich_tail=[], facts_verified=False, _query_fn=boom)
    assert out is None


@pytest.mark.asyncio
async def test_prompt_carries_context_and_history():
    seen = {}
    async def capture(*, prompt, options=None):
        seen["prompt"] = prompt
        class Block:
            text = '{"nudge": "ok"}'
        class Msg:
            content = [Block()]
        yield Msg()
    await compose_nudge(
        _ctx(), intent_hint="arm the PR", verdict_reason="PR #7 is open but not armed",
        verdict_remedy="gh pr merge 7 --squash --auto",
        recent_nudges=["previous nudge text"],
        rich_tail=[{"role": "user", "text": "please fix"}], facts_verified=True,
        _query_fn=capture)
    p = seen["prompt"]
    assert "arm the PR" in p and "PR #7 is open but not armed" in p
    assert "previous nudge text" in p and "please fix" in p
    assert '"facts_verified": true' in p
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_nudge_composer.py -q`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Write `nudge_composer_guidance.md`**

```markdown
<!-- server/automations/nudge_composer_guidance.md -->
# Nudge Composer Guidance

You write the SINGLE next direction for a stuck coding session, as the message the
session will receive. Reply with ONLY a JSON object — either
`{"nudge": "<the message>"}` or `{"escalate": true, "reason": "<why>"}`.
No prose, no preamble, no code fences.

## Contract
- Be CONCRETE: name the files, failing tests, commands, branch, and PR numbers you
  see in the transcript. Direction that names nothing from this session is a failure.
- State the next step AND what "done" looks like for it (e.g. "rerun
  `pytest server/tests/test_ws.py` — green means proceed to commit").
- NEVER restate the generic pipeline ("run tests, commit, push, open a PR, arm
  auto-merge") as the message. If a pipeline stage is genuinely next, name its
  session-specific target instead.
- `prior_nudges` lists what autopilot already told this session. Do NOT rephrase
  the last one. If the session did not follow it, either change strategy (different
  concrete step) or escalate.
- When `facts_verified` is true, `pr_facts` is fresh from GitHub — trust it over
  transcript claims, and say so when directing the session to reconcile
  (e.g. "verified on GitHub just now: no open PR on branch X").
- When `facts_verified` is false, the PR cache may lag: prefer directing the
  session to verify for itself (`gh pr view <n> --json state,autoMergeRequest`)
  over asserting the cache is right.
- `verdict_reason` / `verdict_remedy` explain why completion was rejected — use
  them as input, not as the message.
- If you have nothing concrete to add — the transcript gives no artifact to name
  and no new strategy — escalate. A human beats filler.
```

- [ ] **Step 4: Implement `nudge_composer.py`**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_nudge_composer.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/automations/nudge_composer.py server/automations/nudge_composer_guidance.md server/tests/test_nudge_composer.py
git commit -m "feat(autopilot): Sonnet nudge composer with concrete-direction contract

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Gate guidance rewrite — nudge means intent, not text

**Files:**
- Modify: `server/automations/autopilot_guidance.md` (the `## Nudges` section, lines 25–27)

**Interfaces:** none (doc only; the gate's `prompt` field becomes a one-line intent hint consumed by Task 5's wrapper as `intent_hint`).

- [ ] **Step 1: Replace the `## Nudges` section**

Replace:

```markdown
## Nudges (for `done`/`idle` that isn't truly finished)
- Drive toward: tests+lint green → commit → push → PR ready → auto-merge armed.
- Then judge the goal met.
```

with:

```markdown
## Nudges (for `done`/`idle` that isn't truly finished)
- Choose `nudge` when the session needs direction to keep advancing. Put a ONE-LINE
  intent in `prompt` (e.g. "fix the failing CI check", "rebase onto master") — a
  separate composer with richer context writes the actual message; do NOT write the
  full instruction text yourself.
- Judge the goal met only per the Completion rules below.
```

Also update line 36 (`Code-oriented session … → nudge toward tests+lint green → commit …`) to: `Code-oriented session (has_repo:true) with no armed PR → nudge with intent "finish and ship" (the composer details the step). NEVER complete.`

- [ ] **Step 2: Verify the decider still passes its tests**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_autopilot_decider.py -q`
Expected: PASS (guidance is data to the decider module; tests use fake query_fn).

- [ ] **Step 3: Commit**

```bash
git add server/automations/autopilot_guidance.md
git commit -m "docs(autopilot): gate guidance — nudge signals intent, composer writes the text

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Orchestrator seams — on-demand PR refresh, rich tail, composer wrapper

**Files:**
- Modify: `server/github/pr_poller.py` (add public `in_cooldown` property near `cached`, ~line 156)
- Modify: `server/sessions/orchestrator.py`:
  - `_autopilot_transcript_tail` (~line 2384): add `tool_chars: int = 160` param, use it for both `arg_head` slices.
  - New `_autopilot_refresh_pr_facts(session_id)` and `_autopilot_compose(ctx, decision)` next to the other `_autopilot_*` seams (~line 2483).
- Test: `server/tests/test_autopilot_orchestrator_seams.py` (follow that file's existing construction pattern for orchestrator fixtures)

**Interfaces:**
- Consumes: `PrPoller.refresh_once(pairs)`, `PrPoller._cooldown_until`/`_clock`, `refresh_authored_branches()`, `_session_pr_branches`, `_autopilot_pr_facts`, `_pr_population_pr`, `compose_nudge` (Task 3), `Decision` metadata (Task 1).
- Produces (exact signatures, wired by Task 7 into the loop):
  - `PrPoller.in_cooldown -> bool` (property)
  - `async _autopilot_refresh_pr_facts(self, session_id: str) -> PrFacts | None` — None on no-repo/cooldown/failure; otherwise fresh per-branch facts merged with population-cache hits.
  - `async _autopilot_compose(self, ctx, decision) -> str | None` — fetches the rich tail and calls `compose_nudge`; None propagates (loop escalates).

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_autopilot_orchestrator_seams.py` (adapt fixture names to the file's existing style — it already builds a minimal orchestrator for the other `_autopilot_*` seams; reuse that):

```python
@pytest.mark.asyncio
async def test_refresh_pr_facts_returns_none_without_repo(orch):
    orch._records["s1"].repo = None
    assert await orch._autopilot_refresh_pr_facts("s1") is None


@pytest.mark.asyncio
async def test_refresh_pr_facts_skips_in_cooldown(orch):
    orch._pr_poller._cooldown_until = orch._pr_poller._clock() + 999
    assert await orch._autopilot_refresh_pr_facts("s1") is None


@pytest.mark.asyncio
async def test_refresh_pr_facts_merges_population_cache(orch):
    # per-branch cache empty; population cache knows an open PR on the branch
    # -> the linkage-gap PR still lands in facts (spec §2)
    ...  # build via the file's fake poller/population helpers; assert the PR
         # number from the population cache appears in the returned facts.prs


@pytest.mark.asyncio
async def test_compose_wrapper_passes_rich_tail_and_history(orch, monkeypatch):
    captured = {}
    async def fake_compose(ctx, **kw):
        captured.update(kw)
        return "do the thing"
    monkeypatch.setattr("server.automations.nudge_composer.compose_nudge", fake_compose)
    ctx = make_decision_context(ledger={"recent_nudges": [{"ts": 1.0, "text": "old"}]})
    d = Decision(action="nudge", prompt="hint", downgraded_from="complete",
                 verdict_reason="r", verdict_remedy="m", needs_composition=True)
    out = await orch._autopilot_compose(ctx, d)
    assert out == "do the thing"
    assert captured["intent_hint"] == "hint"
    assert captured["recent_nudges"] == ["old"]
    assert captured["verdict_reason"] == "r"
```

Fill the `...` with the file's established fake-poller pattern (the seams tests already fake `_pr_poller`; mirror how `test_autopilot_orchestrator_seams` builds `PullRequestInfo`).

- [ ] **Step 2: Run to verify failure**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_autopilot_orchestrator_seams.py -q`
Expected: FAIL — `_autopilot_refresh_pr_facts` not defined.

- [ ] **Step 3: Implement**

`pr_poller.py`:

```python
    @property
    def in_cooldown(self) -> bool:
        """True while refresh failures have this poller backing off — on-demand
        callers (autopilot verify-before-directing) must not pile on."""
        return self._clock() < self._cooldown_until
```

`orchestrator.py` — parameterize the tail (`tool_chars: int = 160`, replace both `[:160]` slices with `[:tool_chars]`), then:

```python
    async def _autopilot_refresh_pr_facts(self, session_id: str):
        """On-demand PR ground-truth refresh for verify-before-directing (spec §2).
        Refreshes branch linkage + this session's poller pairs, then rebuilds facts,
        merging population-cache PRs the per-branch cache missed. Returns None on
        no-repo, poller cooldown, or failure — the caller composes against stale
        facts and says so. Rate limiting lives in the loop (ledger clock)."""
        from server.automations.completion_bar import PrFact, PrFacts
        rec = self._records.get(session_id)
        if rec is None or not rec.repo:
            return None
        poller = getattr(self, "_pr_poller", None)
        if poller is None or poller.in_cooldown:
            return None
        try:
            await self.refresh_authored_branches()
            pairs = [(rec.repo, br) for br in self._session_pr_branches(rec)]
            await poller.refresh_once(pairs)
        except Exception:
            log.warning("autopilot.refresh_pr_facts failed sid=%s", session_id, exc_info=True)
            return None
        facts = self._autopilot_pr_facts(rec)
        known = {p.number for p in facts.prs}
        extra = []
        for br in self._session_pr_branches(rec):
            info = self._pr_population_pr(rec.repo, br)
            if info is not None and info.number not in known:
                known.add(info.number)
                extra.append(PrFact(
                    number=info.number, state=info.state, is_draft=info.is_draft,
                    auto_merge_enabled=info.auto_merge_enabled,
                    in_merge_queue=info.in_merge_queue, url=info.url,
                    checks=info.checks))
        if extra:
            facts = PrFacts(has_repo=facts.has_repo, prs=facts.prs + tuple(extra))
        return facts

    async def _autopilot_compose(self, ctx, decision):
        """Compose the actual nudge text (spec §3): rich transcript window + the
        gate's intent hint + bar verdict + prior-nudge history. None -> escalate."""
        from server.automations import nudge_composer
        rich_tail = await self._autopilot_transcript_tail(
            ctx.session_id, turns=20, max_entries=40, max_chars=1500, tool_chars=400)
        try:
            cfg_dir = str(compose(ctx.account))
        except Exception:  # pragma: no cover — fall back to process env
            cfg_dir = None
        recent = [str(e.get("text", "")) for e in (ctx.ledger or {}).get("recent_nudges", [])]
        return await nudge_composer.compose_nudge(
            ctx, intent_hint=decision.prompt,
            verdict_reason=decision.verdict_reason,
            verdict_remedy=decision.verdict_remedy,
            recent_nudges=recent, rich_tail=rich_tail,
            facts_verified=getattr(ctx, "facts_verified", False),
            _config_dir=cfg_dir)
```

NOTE: `monkeypatch.setattr("server.automations.nudge_composer.compose_nudge", ...)` in the test requires the wrapper to resolve `compose_nudge` through the module at call time — hence `from server.automations import nudge_composer` + `nudge_composer.compose_nudge(...)`, NOT `from ... import compose_nudge`.

Also add the ctx field in `autopilot_policy.py` `DecisionContext` (one line, with the other advisory fields):

```python
    # Set by the loop after an on-demand refresh: pr_facts is GitHub-fresh.
    facts_verified: bool = False
```

- [ ] **Step 4: Run tests**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_autopilot_orchestrator_seams.py server/tests/test_autopilot_policy_rules.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/github/pr_poller.py server/sessions/orchestrator.py server/automations/autopilot_policy.py server/tests/test_autopilot_orchestrator_seams.py
git commit -m "feat(autopilot): on-demand PR-facts refresh seam + rich-tail composer wrapper

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Loop integration — verify, compose, dedupe, remember

**Files:**
- Modify: `server/automations/autopilot.py` (`AutopilotLoop.__init__`, `_advance_one`, `_charge`; new `_verify_nudge_facts`, `_compose_nudge_decision`, `_record_suppressed`)
- Test: `server/tests/test_autopilot_loop.py`

**Interfaces:**
- Consumes: Task 2 helpers/constants, Task 1 Decision fields, seam signatures from Task 5.
- Produces:
  - `AutopilotLoop.__init__(..., compose=None, refresh_facts=None)` — `compose: Callable[[DecisionContext, Decision], Awaitable[str | None]] | None`, `refresh_facts: Callable[[str], Awaitable[PrFacts | None]] | None` (keyword-only, after `archive`).
  - Ledger keys: `recent_nudges: list[{"ts": float, "text": str}]` (cap `RECENT_NUDGES_CAP`, loop clock), `last_facts_refresh: float`.
  - Internal decision action `"suppressed"` (never leaves the loop).

- [ ] **Step 1: Write the failing tests**

Extend `_loop()` in `server/tests/test_autopilot_loop.py` with pass-through kwargs `compose=None, refresh_facts=None, get_context=None` (default `get_context` stays `_dc`-based; when the new args are given, forward them to `AutopilotLoop`). Add:

```python
from server.automations.completion_bar import PrFact, PrFacts


def _armed_facts():
    return PrFacts(has_repo=True, prs=(PrFact(
        number=7, state="open", is_draft=False, auto_merge_enabled=True,
        url="u", checks="passing"),))


def _repo_ctx(sid="s1", status="complete", facts=None, tail=None, ledger=None):
    dc = _dc(sid)
    dc.status = status
    dc.repo = "o/r"
    dc.stuck = {"kind": status}
    dc.pr_facts = facts if facts is not None else PrFacts(has_repo=True, prs=())
    dc.transcript_tail = tail or []
    if ledger is not None:
        dc.ledger = ledger
    return dc


@pytest.mark.asyncio
async def test_stale_complete_verifies_and_archives():
    """Spec test 1: stale-facts complete + fresh-facts armed PR -> archive, no nudge."""
    ctx = FakeCtx()
    archived = []

    async def refresh_facts(sid):
        return _armed_facts()

    async def archive(sid):
        archived.append(sid)
        return True

    async def get_context(sid):
        return _repo_ctx(sid, tail=[{"role": "assistant", "text": "PR #7 is armed"}])

    loop = _loop(ctx, decision=Decision(action="complete", reason="marker"),
                 refresh_facts=refresh_facts)
    loop._get_context = get_context
    loop._archive = archive
    await loop.tick()
    assert archived == ["s1"]
    assert loop._applied == []          # no nudge was ever sent


@pytest.mark.asyncio
async def test_composed_nudge_is_sent_and_remembered():
    ctx = FakeCtx()

    async def compose(dc, decision):
        return "Fix the 2 failing tests in test_ws.py."

    loop = _loop(ctx, decision=Decision(action="nudge", prompt="fix tests"),
                 compose=compose)
    await loop.tick()
    assert loop._applied == [("s1", "nudge")]
    nudges = ctx.states["s1"]["recent_nudges"]
    assert nudges[-1]["text"] == "Fix the 2 failing tests in test_ws.py."


@pytest.mark.asyncio
async def test_near_duplicate_composition_is_suppressed_and_counts():
    """Spec test 2: near-identical composed nudge -> suppressed, repeat climbs."""
    ctx = FakeCtx()
    text = "Fix the 2 failing tests in test_ws.py."

    async def compose(dc, decision):
        return text

    async def get_context(sid):
        return _repo_ctx(sid, status="done",
                         ledger={"recent_nudges": [{"ts": 999.0, "text": text}]})

    loop = _loop(ctx, decision=Decision(action="nudge", prompt="fix tests"),
                 compose=compose)
    loop._get_context = get_context
    await loop.tick()
    assert loop._applied == []                            # send suppressed
    assert ctx.states["s1"]["repeat_count"] >= 1          # counted toward escalation


@pytest.mark.asyncio
async def test_composer_failure_escalates_never_sends_template():
    """Spec test 3: composer failure -> escalation, not a canned send."""
    ctx = FakeCtx()

    async def compose(dc, decision):
        return None

    loop = _loop(ctx, decision=Decision(action="nudge", prompt="hint"), compose=compose)
    await loop.tick()
    assert loop._applied == []
    assert any(kind == "escalated" for _, _, kind in ctx.emits)


@pytest.mark.asyncio
async def test_refresh_is_rate_limited_per_session():
    ctx = FakeCtx()
    calls = []

    async def refresh_facts(sid):
        calls.append(sid)
        return None

    async def get_context(sid):
        st = await ctx.get_target_state(sid)
        return _repo_ctx(sid, ledger=st)

    async def compose(dc, decision):
        return f"unique direction {len(calls)} referencing file_{len(calls)}.py"

    loop = _loop(ctx, decision=Decision(action="nudge", prompt="hint"),
                 compose=compose, refresh_facts=refresh_facts)
    loop._get_context = get_context
    await loop.tick()
    await loop.tick()                                     # same clock -> within 600s
    assert calls == ["s1"]                                # second tick skipped refresh


@pytest.mark.asyncio
async def test_no_composer_and_no_prompt_escalates():
    ctx = FakeCtx()
    d = Decision(action="nudge", prompt=None, needs_composition=True,
                 downgraded_from="complete")
    loop = _loop(ctx, decision=d)                         # compose=None (legacy)
    await loop.tick()
    assert loop._applied == []
    assert any(kind == "escalated" for _, _, kind in ctx.emits)
```

Note on `test_stale_complete_verifies_and_archives`: the gate returns `complete`; `post_validate` downgrades it (empty facts) to a composition-marked nudge; the verify step refreshes, re-runs `met_bar`, sees the armed PR, and restores `complete` → archive. The test exercises the full downgrade→verify→complete path.

Also update `test_loop_detection_escalates_after_repeats`: with a composer absent and a non-empty gate prompt the legacy path still applies the nudge, so it keeps passing unchanged — verify, and if the new `_repo_ctx` default ledger broke `_dc`-based tests, fix the helper, not the tests.

- [ ] **Step 2: Run to verify failure**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_autopilot_loop.py -q`
Expected: FAIL — `AutopilotLoop.__init__` has no `compose`/`refresh_facts`.

- [ ] **Step 3: Implement in `autopilot.py`**

`__init__` additions (after `archive`):

```python
                 archive: Callable[[str], Awaitable[bool]] | None = None,
                 compose: "Callable[[DecisionContext, Decision], Awaitable[str | None]] | None" = None,
                 refresh_facts: "Callable[[str], Awaitable[object | None]] | None" = None) -> None:
        ...
        self._compose = compose          # Sonnet nudge composer (None = legacy pass-through)
        self._refresh_facts = refresh_facts  # on-demand PR ground-truth refresh
```

In `_advance_one`, insert after `decision = post_validate(...)` (i.e. right after the decide step, before the `skip` handling):

```python
        if decision.action == "nudge":
            decision = await self._verify_nudge_facts(sid, ctx, decision, now)
```

and immediately before `acted = await self._apply(...)` (after the propose-mode block, so propose never burns a composition):

```python
        if decision.action == "nudge":
            decision = await self._compose_nudge_decision(sid, ctx, decision, now)
            if decision.action == "suppressed":
                return False, False
            if decision.action == "escalate":
                await self._escalate(sid, ledger, ctx, decision)
                return False, True
```

New methods:

```python
    async def _verify_nudge_facts(self, sid: str, ctx: DecisionContext,
                                  decision: Decision, now: float) -> Decision:
        """Verify-before-directing (spec §2): when the cached PR facts contradict
        the session's claims (or a self-declared complete), refresh ground truth
        once per FACTS_REFRESH_MIN_INTERVAL_S and re-judge. A goal verified met
        restores the original `complete` — the nudge never happens."""
        from .autopilot_policy import FACTS_REFRESH_MIN_INTERVAL_S, facts_contradicted
        if self._refresh_facts is None or not facts_contradicted(ctx):
            return decision
        ledger = ctx.ledger or {}
        if now - float(ledger.get("last_facts_refresh", 0.0)) < FACTS_REFRESH_MIN_INTERVAL_S:
            return decision
        new = dict(ledger)
        new["last_facts_refresh"] = now
        await self._ctx.set_target_state(sid, new)
        ctx.ledger = new
        fresh = await self._refresh_facts(sid)
        if fresh is None:
            return decision          # cooldown/failure: compose against stale facts
        ctx.pr_facts = fresh
        ctx.facts_verified = True
        if decision.downgraded_from == "complete":
            from .completion_bar import met_bar
            verdict = met_bar(ctx.effective_goal, ctx.goal_source, fresh,
                              ctx.pr_resolution, instructions=ctx.instructions)
            if verdict.met:
                return Decision(action="complete", reason=f"verified: {verdict.reason}")
        return decision

    async def _compose_nudge_decision(self, sid: str, ctx: DecisionContext,
                                      decision: Decision, now: float) -> Decision:
        """Compose the final nudge text (spec §3) and dedupe it (spec §4). No
        composer: a gate-authored prompt passes through (legacy); a composition-
        marked decision without text escalates — never a template."""
        from .autopilot_policy import near_duplicate
        if self._compose is None:
            if (decision.prompt or "").strip():
                return decision
            return Decision(action="escalate", reason="nudge needs composition — no composer")
        text = await self._compose(ctx, decision)
        if not (text or "").strip():
            return Decision(action="escalate",
                            reason=f"composer declined/failed ({decision.reason})")
        if near_duplicate(text, (ctx.ledger or {}).get("recent_nudges", []), now):
            await self._record_suppressed(sid, ctx.ledger or {}, ctx)
            return Decision(action="suppressed", reason="near-duplicate nudge suppressed")
        return Decision(action="nudge", prompt=text, reason=decision.reason,
                        downgraded_from=decision.downgraded_from)

    async def _record_suppressed(self, sid: str, ledger: dict, ctx: DecisionContext) -> None:
        """A suppressed near-duplicate counts toward loop escalation UNCONDITIONALLY —
        repetition here is keyed on what autopilot said, immune to the goal-text
        drift that resets `fingerprint` (spec §4)."""
        new = dict(ledger)
        new.update(last_fingerprint=fingerprint(ctx),
                   repeat_count=ledger.get("repeat_count", 0) + 1)
        await self._ctx.set_target_state(sid, new)
```

In `_charge`, remember sent nudges (add before `new = dict(ledger)`; note `_charge` reads `ledger` — the decide-time snapshot updated by the verify step via `ctx.ledger`, so pass `ctx.ledger or ledger` consistently — simplest is to change `_advance_one`'s `_charge`/`_record_fingerprint` calls to pass `ctx.ledger`):

```python
        recent = list(ledger.get("recent_nudges", []))
        if decision.action == "nudge" and (decision.prompt or "").strip():
            from .autopilot_policy import RECENT_NUDGES_CAP
            recent = (recent + [{"ts": now, "text": decision.prompt}])[-RECENT_NUDGES_CAP:]
```

and add `recent_nudges=recent` to the `new.update(...)` call.

In `_advance_one`, replace the two `ledger` argument usages that now need decide-time freshness: `await self._charge(sid, ctx.ledger or ledger, ctx, decision, now)` and keep `_record_fingerprint` as-is (it re-reads nothing stale that matters).

- [ ] **Step 4: Run the loop + policy suites**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_autopilot_loop.py server/tests/test_autopilot_policy_rules.py server/tests/test_autopilot_turn_restart_defer.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/automations/autopilot.py server/tests/test_autopilot_loop.py
git commit -m "feat(autopilot): loop verify->compose->dedupe pipeline for contextual nudges

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Wire the seams + full verification

**Files:**
- Modify: `server/sessions/orchestrator.py` (~line 411, `AutopilotLoop(...)` construction)
- Test: full server suite + lint gates

**Interfaces:**
- Consumes: everything above.
- Produces: the live daemon path uses the new pipeline.

- [ ] **Step 1: Wire the constructor**

In the `AutopilotLoop(...)` call add:

```python
            archive=self._autopilot_archive,
            compose=self._autopilot_compose,
            refresh_facts=self._autopilot_refresh_pr_facts,
```

- [ ] **Step 2: Grep for dead code / parallel implementations**

Run: `grep -rn "arm auto-merge before finishing" server/ src/ web/src/ | grep -v tests`
Expected: only `completion_bar.py`'s remedy strings (now hints, still the single source of truth) — no other template senders. If a test asserts the old canned prompt reaches a session, fix the test to the new contract.

- [ ] **Step 3: Run the full server suite**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/ -q --ignore=server/tests/daemon_properties -p no:cacheprovider --deselect server/tests/test_eta_parity_live.py 2>&1 | tail -15`
Expected: all pass except known-local failures per memory (`copilot_proxy_resolve` may fail if the binary is installed locally — pre-existing, not this change).

- [ ] **Step 4: Lint + types (CI-exact)**

Run: `ruff check server/ src/ && mypy server/ --ignore-missing-imports`
Expected: clean. Fix anything introduced by this work.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py
git commit -m "feat(autopilot): wire composer + on-demand PR refresh into the daemon loop

Fixes the canned-nudge pipeline end to end: the 'open a PR and arm
auto-merge before finishing' template (sent 6,936 times in prod) can no
longer reach a session on any path.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Ship — push, PR, auto-merge

- [ ] **Step 1: Push**

```bash
git push -u origin autopilot-contextual-nudges
```

- [ ] **Step 2: Open the PR (non-draft) with the Claude Code trailer**

```bash
gh pr create --title "feat(autopilot): contextual nudges — two-tier decider, verified facts, anti-repetition" --body "$(cat <<'EOF'
## Summary
- Autopilot's completion-gate keeps its verdict authority but no longer sends canned text: the `met_bar` remedy template ("open a PR and arm auto-merge before finishing") was sent **6,936 times** in prod and can no longer reach a session on any path.
- Two-tier decider: the Haiku gate picks the action; a new Sonnet `nudge_composer` writes every nudge from a ~10× larger transcript window, verified PR facts, fleet context, and prior-nudge history — composer failure escalates, never templates.
- Verify-before-directing: when cached `pr_facts` contradict the session (claimed PR missing, or self-declared complete), autopilot refreshes ground truth from GitHub (rate-limited, cooldown-aware, population-cache fallback) and re-judges — a verified-met goal archives instead of nudging.
- Deterministic anti-repetition: last 5 sent nudges live in the session ledger; a composed nudge ≥0.85 token-set-similar to one sent in 6h is suppressed and counts toward loop escalation (closes the fingerprint-reset hole).

Spec: `docs/superpowers/specs/2026-08-09-autopilot-contextual-nudges-design.md`

## Test plan
- [ ] `pytest server/tests/` green (policy rules, composer contract, loop pipeline, orchestrator seams)
- [ ] `ruff check server/ src/` + `mypy server/ --ignore-missing-imports` clean

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Arm auto-merge and verify**

```bash
gh pr merge --squash --auto
gh pr view --json state,isDraft,autoMergeRequest --jq '{state, isDraft, armed: (.autoMergeRequest != null)}'
```

Expected: `state: OPEN, isDraft: false, armed: true`.

---

## Self-Review (completed at plan-writing time)

- **Spec coverage:** §1 completion-gate → Task 1; §2 verify-before-directing (rate limit, cooldown, population fallback, re-met_bar) → Tasks 2, 5, 6; §3 two-tier composer (rich tail, contract, escalate-on-failure) → Tasks 3, 5, 6; §4 anti-repetition (ledger memory, 0.85/6h dedupe, unconditional repeat) → Tasks 2, 6; §5 guidance rewrite → Tasks 3, 4; spec's three loop-level tests → Task 6 steps. Error-handling table → Tasks 3 (None on failure), 6 (stale-facts fallback, escalate).
- **Placeholders:** one intentional `...` in Task 5's population-cache test with explicit instructions to follow the file's existing fake-poller pattern (the pattern is file-specific; copying a guess would be worse).
- **Type consistency:** `compose(ctx, decision) -> str | None` (Tasks 5/6 match); `refresh_facts(sid) -> PrFacts | None` (Tasks 5/6 match); Decision fields consistent across Tasks 1/5/6; ledger keys `recent_nudges`/`last_facts_refresh` consistent across Tasks 2/5/6.
