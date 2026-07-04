# Autopilot Completion Bar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop autopilot archiving sessions from inferred completion by making "done" require a verified armed PR (or a genuine non-code goal), and retroactively audit + auto-unarchive premature archives.

**Architecture:** One pure `completion_bar.met_bar()` predicate is the single source of truth for "is the goal met?", consuming verified PR facts. The live autopilot path feeds it facts from the PR poller cache (plus a deterministic `post_validate` gate); a retro audit feeds it real `gh` query results and auto-unarchives the confidently-premature sessions.

**Tech Stack:** Python 3, FastAPI/SQLAlchemy backend, `claude_agent_sdk` (decider), `gh` CLI (retro). Tests: `pytest`, all I/O injected (no network/model).

## Global Constraints

- Python model columns added must be nullable (dev SQLite `create_all` never ALTERs). *This plan adds no columns.*
- CI gates that must be green: `ruff`, `mypy`, `pytest` (run in `server/`). `mypy` is a required, easily-missed gate.
- Run server tests with the prebuilt/main env; a fresh worktree has no built env. Command form: `python -m pytest server/tests/<file> -q` from repo root (add `PYTHONPATH=.:src` and `CCSWITCH_DEV=1` if importing app modules standalone).
- No parallel implementations: `met_bar()` is the ONLY definition of "done". Live + retro both call it.
- Every external dependency (model query, `gh`) is injected via a `_*_fn` seam so tests never touch the network — mirror the existing `autopilot_decider._query_fn` pattern.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## File Structure

- **Create** `server/automations/completion_bar.py` — pure: `PrFact`, `PrFacts`, `BarVerdict`, `met_bar()`, `_is_code_goal()`. No I/O.
- **Create** `server/tests/test_completion_bar.py` — the `met_bar` truth table.
- **Modify** `server/automations/autopilot_policy.py` — add `pr_facts` field to `DecisionContext`; add the un-armed-PR hard gate to `post_validate`.
- **Modify** `server/tests/test_autopilot_policy_rules.py` — gate tests.
- **Modify** `server/sessions/orchestrator.py` — `_autopilot_pr_facts()` helper; wire it into `_autopilot_get_context`.
- **Modify** `server/automations/autopilot_decider.py` — include `pr_facts` in the decider prompt payload.
- **Modify** `server/tests/test_autopilot_decider.py` — assert prompt carries pr_facts.
- **Modify** `server/automations/autopilot_guidance.md` — rewrite completion section to use `pr_facts`.
- **Create** `server/automations/archive_audit.py` — retro audit: `classify()` (pure), `build_pr_facts()` (injected `gh_fn`), `run_audit()`, `format_report()`, `__main__` entrypoint.
- **Create** `server/tests/test_archive_audit.py` — classifier + run_audit with fakes.

---

### Task 1: `completion_bar.py` — the pure "done" predicate

**Files:**
- Create: `server/automations/completion_bar.py`
- Test: `server/tests/test_completion_bar.py`

**Interfaces:**
- Consumes: nothing (pure, stdlib only).
- Produces:
  - `PrFact(number:int, state:str, is_draft:bool, auto_merge_enabled:bool, url:str)` (frozen dataclass)
  - `PrFacts(has_repo:bool, prs:tuple[PrFact,...])` with properties `armed_pr`, `merged_pr`, `open_unarmed_pr` → `PrFact | None`, and method `to_prompt() -> dict`
  - `BarVerdict(met:bool, reason:str, remedy:str|None, confidence:str)` (`confidence` ∈ `"high"|"low"`)
  - `met_bar(goal:str, goal_source:str, facts:PrFacts) -> BarVerdict`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_completion_bar.py
from server.automations.completion_bar import PrFact, PrFacts, met_bar


def _facts(prs=(), has_repo=True):
    return PrFacts(has_repo=has_repo, prs=tuple(prs))


def _pr(number=1, state="open", is_draft=False, auto_merge=False):
    return PrFact(number=number, state=state, is_draft=is_draft,
                  auto_merge_enabled=auto_merge, url=f"http://x/{number}")


def test_merged_pr_is_met_high():
    v = met_bar("fix ci", "explicit", _facts([_pr(state="merged")]))
    assert v.met and v.confidence == "high"


def test_armed_pr_is_met_high():
    v = met_bar("fix ci", "explicit", _facts([_pr(state="open", auto_merge=True)]))
    assert v.met and v.confidence == "high"


def test_open_unarmed_pr_is_not_met_high_with_remedy():
    v = met_bar("fix ci", "explicit", _facts([_pr(state="open", auto_merge=False)]))
    assert not v.met and v.confidence == "high" and v.remedy


def test_draft_pr_is_not_met_high():
    v = met_bar("fix ci", "explicit", _facts([_pr(state="draft", is_draft=True, auto_merge=True)]))
    assert not v.met and v.confidence == "high"


def test_code_goal_no_pr_is_not_met_low():
    v = met_bar("fix the cicd doctor", "explicit", _facts([], has_repo=True))
    assert not v.met and v.confidence == "low"


def test_research_goal_no_pr_is_met_low():
    v = met_bar("investigate why deploys are slow", "explicit", _facts([], has_repo=True))
    assert v.met and v.confidence == "low"


def test_no_repo_no_pr_is_met_low():
    v = met_bar("anything", "initial_prompt", _facts([], has_repo=False))
    assert v.met and v.confidence == "low"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_completion_bar.py -q`
Expected: FAIL — `ModuleNotFoundError: server.automations.completion_bar`

- [ ] **Step 3: Write minimal implementation**

```python
# server/automations/completion_bar.py
"""The single source of truth for autopilot's "is this session's goal met?"
decision. Pure — no I/O. Consumed by BOTH the live autopilot gate/decider and the
retro archive audit, so "done" is defined in exactly one place (constitution VI)."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Research-escape signal: a goal that reads as investigation/Q&A rather than a code
# change is allowed to complete with no PR. Kept deliberately narrow — when unsure we
# treat a repo-attached session as code work (the safe direction: it only ever routes
# a no-PR session to low-confidence "report only", never to auto-unarchive).
_RESEARCH_RE = re.compile(
    r"\b(investigate|research|analy[sz]e|summari[sz]e|explain|understand|explore|"
    r"question|answer|diagnose|assess|review|audit|compare|document)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PrFact:
    number: int
    state: str            # 'open' | 'draft' | 'merged' | 'closed'
    is_draft: bool
    auto_merge_enabled: bool
    url: str


@dataclass(frozen=True)
class PrFacts:
    has_repo: bool
    prs: tuple[PrFact, ...] = ()

    @property
    def merged_pr(self) -> "PrFact | None":
        return next((p for p in self.prs if p.state == "merged"), None)

    @property
    def armed_pr(self) -> "PrFact | None":
        return next((p for p in self.prs
                     if p.state == "open" and not p.is_draft and p.auto_merge_enabled), None)

    @property
    def open_unarmed_pr(self) -> "PrFact | None":
        return next((p for p in self.prs
                     if p.state in ("open", "draft")
                     and not (p.state == "open" and not p.is_draft and p.auto_merge_enabled)),
                    None)

    def to_prompt(self) -> dict:
        return {
            "has_repo": self.has_repo,
            "prs": [{"number": p.number, "state": p.state, "is_draft": p.is_draft,
                     "auto_merge_enabled": p.auto_merge_enabled} for p in self.prs],
        }


@dataclass(frozen=True)
class BarVerdict:
    met: bool
    reason: str
    remedy: str | None
    confidence: str        # 'high' | 'low'


def _is_code_goal(goal: str) -> bool:
    """A repo-attached goal is code work unless it clearly reads as research/Q&A."""
    return not _RESEARCH_RE.search(goal or "")


def met_bar(goal: str, goal_source: str, facts: PrFacts) -> BarVerdict:
    """Is the session's goal genuinely met? Precedence: merged > armed > open-unarmed
    > no-PR. Only high-confidence verdicts are safe to auto-act on."""
    merged = facts.merged_pr
    if merged is not None:
        return BarVerdict(True, f"code landed (PR #{merged.number} merged)", None, "high")
    armed = facts.armed_pr
    if armed is not None:
        return BarVerdict(True, f"PR #{armed.number} open with auto-merge armed", None, "high")
    unarmed = facts.open_unarmed_pr
    if unarmed is not None:
        return BarVerdict(
            False, f"PR #{unarmed.number} is open but not armed",
            f"ready the PR and arm auto-merge: `gh pr ready {unarmed.number}` then "
            f"`gh pr merge {unarmed.number} --squash --auto`", "high")
    if facts.has_repo and _is_code_goal(goal):
        return BarVerdict(False, "code goal but no PR opened",
                          "open a PR and arm auto-merge before finishing", "low")
    return BarVerdict(True, "non-code goal — no PR expected", None, "low")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_completion_bar.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add server/automations/completion_bar.py server/tests/test_completion_bar.py
git commit -m "feat(autopilot): completion_bar — pure met_bar() done predicate

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `DecisionContext.pr_facts` + `post_validate` hard gate

**Files:**
- Modify: `server/automations/autopilot_policy.py` (`DecisionContext` dataclass; `post_validate`)
- Test: `server/tests/test_autopilot_policy_rules.py`

**Interfaces:**
- Consumes: `completion_bar.PrFacts` (Task 1).
- Produces: `DecisionContext.pr_facts: PrFacts | None = None`; `post_validate` downgrades `complete`→`nudge` when `pr_facts.open_unarmed_pr` is present.

- [ ] **Step 1: Write the failing test** (append to `test_autopilot_policy_rules.py`)

```python
from server.automations.completion_bar import PrFact, PrFacts


def _armed_facts():
    return PrFacts(has_repo=True, prs=(PrFact(1, "open", False, True, "u"),))


def _unarmed_facts():
    return PrFacts(has_repo=True, prs=(PrFact(1, "open", False, False, "u"),))


def test_complete_with_unarmed_pr_is_downgraded_to_nudge():
    ctx = _ctx(status="complete", stuck={"kind": "complete"}, pr_facts=_unarmed_facts())
    d = post_validate(Decision(action="complete", reason="agent said done"), ctx, CONFIG)
    assert d.action == "nudge" and d.prompt


def test_complete_with_armed_pr_survives():
    ctx = _ctx(status="complete", stuck={"kind": "complete"}, pr_facts=_armed_facts())
    d = post_validate(Decision(action="complete", reason="armed"), ctx, CONFIG)
    assert d.action == "complete"


def test_complete_with_no_pr_facts_is_left_to_decider():
    ctx = _ctx(status="complete", stuck={"kind": "complete"})  # pr_facts defaults None
    d = post_validate(Decision(action="complete", reason="x"), ctx, CONFIG)
    assert d.action == "complete"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_autopilot_policy_rules.py -q -k "unarmed or armed_pr_survives or left_to_decider"`
Expected: FAIL — `DecisionContext` has no `pr_facts` (TypeError on unexpected kwarg).

- [ ] **Step 3: Write minimal implementation**

In `autopilot_policy.py`, add the import and field. At the top imports add:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .completion_bar import PrFacts
```

In `DecisionContext` add (after `ledger`):

```python
    pr_facts: "PrFacts | None" = None   # verified PR ground-truth (None = unknown)
```

In `post_validate`, before `return decision`, add the completion gate:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_autopilot_policy_rules.py -q`
Expected: PASS (all, including the pre-existing rules)

- [ ] **Step 5: Commit**

```bash
git add server/automations/autopilot_policy.py server/tests/test_autopilot_policy_rules.py
git commit -m "feat(autopilot): hard gate — un-armed PR blocks complete, nudges to arm

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Assemble `pr_facts` in `_autopilot_get_context` + feed the decider

**Files:**
- Modify: `server/sessions/orchestrator.py` (`_autopilot_get_context`; add `_autopilot_pr_facts`)
- Modify: `server/automations/autopilot_decider.py` (`_build_prompt`)
- Modify: `server/automations/autopilot_guidance.md`
- Test: `server/tests/test_autopilot_decider.py`

**Interfaces:**
- Consumes: `completion_bar.PrFact`, `PrFacts` (Task 1); `orchestrator.pr_for(repo, branch) -> PullRequestInfo | None`; `PullRequestInfo` fields `number, state, is_draft, auto_merge_enabled, url`.
- Produces: `DecisionContext.pr_facts` populated on the live path; decider prompt payload key `"pr_facts"`.

- [ ] **Step 1: Write the failing test** (append to `test_autopilot_decider.py`)

```python
from server.automations.autopilot_decider import _build_prompt
from server.automations.autopilot_policy import DecisionContext
from server.automations.completion_bar import PrFact, PrFacts


def test_build_prompt_includes_pr_facts():
    ctx = DecisionContext(
        session_id="s1", status="complete", account="a", repo="o/r", branch="feat/x",
        effective_goal="fix ci", goal_source="explicit", stuck={"kind": "complete"},
        transcript_tail=[], ledger={},
        pr_facts=PrFacts(has_repo=True, prs=(PrFact(7, "open", False, False, "u"),)))
    prompt = _build_prompt(ctx)
    assert "pr_facts" in prompt and "\"number\": 7" in prompt and "has_repo" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_autopilot_decider.py::test_build_prompt_includes_pr_facts -q`
Expected: FAIL — `"pr_facts"` not in prompt.

- [ ] **Step 3a: Add `pr_facts` to the decider prompt**

In `autopilot_decider.py` `_build_prompt`, add to the payload dict (before `transcript_tail`):

```python
        "pr_facts": ctx.pr_facts.to_prompt() if ctx.pr_facts is not None else None,
```

- [ ] **Step 3b: Assemble pr_facts in the orchestrator**

In `orchestrator.py`, add the helper near `_autopilot_get_context`:

```python
    def _autopilot_pr_facts(self, rec) -> "object":
        """Verified PR ground-truth for a governed session, from the poller cache
        only (no new I/O). Mirrors request_pr_fix: check every authored branch plus
        the scratch branch, dedupe by PR number."""
        from server.automations.completion_bar import PrFact, PrFacts
        branches = list(getattr(rec, "authored_branches", None) or [])
        if getattr(rec, "branch", None):
            branches.append(rec.branch)
        seen: dict[int, PrFact] = {}
        for br in branches:
            info = self.pr_for(rec.repo, br) if rec.repo else None
            if info is not None and info.number not in seen:
                seen[info.number] = PrFact(
                    number=info.number, state=info.state, is_draft=info.is_draft,
                    auto_merge_enabled=info.auto_merge_enabled, url=info.url)
        return PrFacts(has_repo=bool(rec.repo), prs=tuple(seen.values()))
```

Then in `_autopilot_get_context`, in the `return DecisionContext(...)` call, add the argument:

```python
            transcript_tail=await self._autopilot_transcript_tail(session_id), ledger={},
            pr_facts=self._autopilot_pr_facts(rec))
```

- [ ] **Step 3c: Update the guidance completion section**

In `autopilot_guidance.md`, replace the `## Completion` and `## \`complete\` status` sections with:

```markdown
## Completion (uses `pr_facts` — verified PR state, do NOT infer from prose)
`pr_facts` gives you the session's real PRs: `{has_repo, prs:[{number, state, is_draft, auto_merge_enabled}]}`.
- An **armed** PR = `state:"open"`, `is_draft:false`, `auto_merge_enabled:true`. A **merged** PR means code landed.
- `complete` ONLY when `pr_facts` shows an armed or merged PR, OR the goal is clearly
  non-code (research / diagnosis / Q&A) and `has_repo` work isn't expected.
- Code-oriented session (`has_repo:true`) with no armed PR → `nudge` toward tests+lint
  green → commit → push → `gh pr ready` → `gh pr merge --squash --auto`. NEVER `complete`.
- An open-but-un-armed PR is already force-nudged upstream; if you still see one, `nudge` to arm it.

## `complete` status (the session emitted its own completion marker)
A STRONG signal the agent *believes* it is done — but confirm against `pr_facts`, not the
marker alone. Armed/merged PR → `complete`. Premature marker (code goal, no armed PR) →
`nudge` toward the remaining work. Only `escalate` when you genuinely can't tell.
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest server/tests/test_autopilot_decider.py -q`
Expected: PASS (new + existing)

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/automations/autopilot_decider.py server/automations/autopilot_guidance.md server/tests/test_autopilot_decider.py
git commit -m "feat(autopilot): feed verified pr_facts to the decider + guidance

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Retro audit — classifier + `run_audit` (injected `gh`)

**Files:**
- Create: `server/automations/archive_audit.py`
- Test: `server/tests/test_archive_audit.py`

**Interfaces:**
- Consumes: `completion_bar.met_bar`, `PrFact`, `PrFacts` (Task 1); `orchestrator.unarchive(session_id)`.
- Produces:
  - `classify(goal, goal_source, facts) -> tuple[str, BarVerdict]` where bucket ∈ `"legit"|"premature"|"ambiguous"`
  - `build_pr_facts(repo, branches, gh_fn) -> PrFacts` (`gh_fn(repo, branch) -> list[dict]`; a raising `gh_fn` → `has_repo=True, prs=()` and the caller buckets ambiguous)
  - `async run_audit(rows, gh_fn, unarchive_fn, dry_run=False) -> list[dict]` where each `row` is `{"id","repo","branch","authored_branches","goal","goal_source"}`
  - `format_report(results) -> str`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_archive_audit.py
import asyncio
from server.automations import archive_audit as aa


def _row(id, goal, repo="o/r", branch="feat/x", authored=("feat/x",)):
    return {"id": id, "repo": repo, "branch": branch,
            "authored_branches": list(authored), "goal": goal, "goal_source": "explicit"}


def _gh_returning(pr):
    def gh_fn(repo, branch):
        return [pr] if pr else []
    return gh_fn


def _pr(number=1, state="OPEN", is_draft=False, auto_merge=False):
    return {"number": number, "state": state, "isDraft": is_draft,
            "autoMergeRequest": {"enabledAt": "t"} if auto_merge else None,
            "url": f"http://x/{number}"}


def test_merged_pr_is_legit_not_unarchived():
    unarchived = []
    gh = _gh_returning(_pr(state="MERGED"))
    out = asyncio.run(aa.run_audit([_row("s1", "fix ci")], gh, unarchived.append))
    assert out[0]["bucket"] == "legit" and unarchived == []


def test_unarmed_open_pr_is_premature_and_unarchived():
    unarchived = []
    gh = _gh_returning(_pr(state="OPEN", auto_merge=False))
    out = asyncio.run(aa.run_audit([_row("s1", "fix ci")], gh, unarchived.append))
    assert out[0]["bucket"] == "premature" and unarchived == ["s1"]


def test_code_goal_no_pr_is_ambiguous_report_only():
    unarchived = []
    out = asyncio.run(aa.run_audit([_row("s1", "fix the cicd doctor")],
                                   _gh_returning(None), unarchived.append))
    assert out[0]["bucket"] == "ambiguous" and unarchived == []


def test_gh_error_is_ambiguous():
    def gh(repo, branch):
        raise RuntimeError("gh down")
    out = asyncio.run(aa.run_audit([_row("s1", "fix ci")], gh, lambda s: None))
    assert out[0]["bucket"] == "ambiguous"


def test_dry_run_does_not_unarchive():
    unarchived = []
    gh = _gh_returning(_pr(state="OPEN", auto_merge=False))
    out = asyncio.run(aa.run_audit([_row("s1", "fix ci")], gh, unarchived.append, dry_run=True))
    assert out[0]["bucket"] == "premature" and unarchived == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_archive_audit.py -q`
Expected: FAIL — `ModuleNotFoundError: server.automations.archive_audit`

- [ ] **Step 3: Write minimal implementation**

```python
# server/automations/archive_audit.py
"""Retro audit: re-judge already-archived sessions against the current completion
bar (completion_bar.met_bar — the SAME predicate the live autopilot uses). Reports
every session and auto-unarchives the confidently-premature ones. Every I/O
dependency (gh, unarchive) is injected, so the pipeline is unit-tested with no
network."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from .completion_bar import BarVerdict, PrFact, PrFacts, met_bar

log = logging.getLogger(__name__)

GhFn = Callable[[str, str], list[dict]]              # (repo, branch) -> raw PR dicts
UnarchiveFn = Callable[[str], Awaitable[None] | None]


def _pr_fact_from_gh(d: dict) -> PrFact:
    """Map one `gh pr list --json ...` row to a PrFact. gh states are UPPERCASE
    (OPEN/MERGED/CLOSED); autoMergeRequest is non-null when auto-merge is armed."""
    state = str(d.get("state", "")).lower()
    is_draft = bool(d.get("isDraft", False))
    if state == "open" and is_draft:
        state = "draft"
    return PrFact(number=int(d.get("number", 0)), state=state, is_draft=is_draft,
                  auto_merge_enabled=d.get("autoMergeRequest") is not None,
                  url=str(d.get("url", "")))


def build_pr_facts(repo: str | None, branches: list[str], gh_fn: GhFn) -> PrFacts:
    """PrFacts for an archived session by querying real PRs per branch. A raising
    gh_fn propagates (the caller buckets that session ambiguous)."""
    seen: dict[int, PrFact] = {}
    for br in branches:
        if not repo or not br:
            continue
        for raw in gh_fn(repo, br) or []:
            fact = _pr_fact_from_gh(raw)
            seen.setdefault(fact.number, fact)
    return PrFacts(has_repo=bool(repo), prs=tuple(seen.values()))


def classify(goal: str, goal_source: str, facts: PrFacts) -> tuple[str, BarVerdict]:
    """Bucket via met_bar: met→legit; not-met+high→premature; not-met+low→ambiguous."""
    v = met_bar(goal, goal_source, facts)
    if v.met:
        return "legit", v
    return ("premature" if v.confidence == "high" else "ambiguous"), v


async def _maybe_await(x):
    if asyncio.iscoroutine(x):
        return await x
    return x


async def run_audit(rows: list[dict], gh_fn: GhFn, unarchive_fn: UnarchiveFn,
                    *, dry_run: bool = False) -> list[dict]:
    """Audit each archived-session row; auto-unarchive the premature ones (unless
    dry_run). Best-effort: one failing session never aborts the run."""
    results: list[dict] = []
    for row in rows:
        sid = row["id"]
        branches = list(row.get("authored_branches") or [])
        if row.get("branch"):
            branches.append(row["branch"])
        try:
            facts = build_pr_facts(row.get("repo"), branches, gh_fn)
        except Exception as exc:
            log.warning("archive_audit gh failed sid=%s: %s", sid, exc)
            results.append({"id": sid, "bucket": "ambiguous", "reason": f"gh error: {exc}",
                            "goal": row.get("goal", ""), "action": "none"})
            continue
        bucket, verdict = classify(row.get("goal", ""), row.get("goal_source", ""), facts)
        action = "none"
        if bucket == "premature" and not dry_run:
            try:
                await _maybe_await(unarchive_fn(sid))
                action = "unarchived"
            except Exception as exc:  # unarchive failure never aborts the audit
                log.warning("archive_audit unarchive failed sid=%s: %s", sid, exc)
                action = f"unarchive-failed: {exc}"
        elif bucket == "premature":
            action = "would-unarchive"
        results.append({"id": sid, "bucket": bucket, "reason": verdict.reason,
                        "goal": row.get("goal", ""), "action": action})
    return results


def format_report(results: list[dict]) -> str:
    """Human-readable table + counts."""
    from collections import Counter
    counts = Counter(r["bucket"] for r in results)
    lines = [f"Archive audit: {len(results)} sessions — "
             + ", ".join(f"{k}={counts[k]}" for k in ("legit", "premature", "ambiguous"))]
    for r in results:
        lines.append(f"  [{r['bucket']:<9}] {r['id'][:8]}  {r['action']:<16} "
                     f"{r['goal'][:48]!r}  — {r['reason']}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_archive_audit.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add server/automations/archive_audit.py server/tests/test_archive_audit.py
git commit -m "feat(autopilot): retro archive audit — classify + auto-unarchive

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Retro audit entrypoint (`python -m server.automations.archive_audit`)

**Files:**
- Modify: `server/automations/archive_audit.py` (add real-`gh` fn, DB row loader, `main()`, `__main__`)
- Test: `server/tests/test_archive_audit.py` (add real-gh JSON-parse + row-loader tests)

**Interfaces:**
- Consumes: `run_audit`, `build_pr_facts`, `format_report` (Task 4); `orchestrator.unarchive`; `DbSession`, `db_session`; `server.sessions.goals.effective_goal`.
- Produces: `_real_gh(repo, branch) -> list[dict]`; `async load_archived_rows(db_session_factory) -> list[dict]`; `async main(argv) -> int`.

- [ ] **Step 1: Write the failing test** (append to `test_archive_audit.py`)

```python
def test_real_gh_parses_json(monkeypatch):
    import subprocess
    sample = '[{"number": 3, "state": "OPEN", "isDraft": false, "autoMergeRequest": null, "url": "u"}]'

    def fake_run(cmd, capture_output, text, timeout, check):
        class R:
            stdout = sample
            returncode = 0
        assert "--head" in cmd and "--state" in cmd
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = aa._real_gh("o/r", "feat/x")
    assert out[0]["number"] == 3


def test_real_gh_returns_empty_on_error(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(subprocess, "run", boom)
    assert aa._real_gh("o/r", "feat/x") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_archive_audit.py -q -k real_gh`
Expected: FAIL — `module 'archive_audit' has no attribute '_real_gh'`

- [ ] **Step 3: Add the entrypoint pieces**

Append to `archive_audit.py`:

```python
import subprocess


def _real_gh(repo: str, branch: str) -> list[dict]:
    """`gh pr list` for one head branch, all states. Returns [] on any gh failure —
    the caller then buckets the session ambiguous (never a false 'premature')."""
    try:
        r = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "all",
             "--json", "number,state,isDraft,autoMergeRequest,url"],
            capture_output=True, text=True, timeout=30, check=True)
    except Exception as exc:  # gh missing / auth / network / non-zero
        log.warning("archive_audit gh pr list failed repo=%s head=%s: %s", repo, branch, exc)
        return []
    try:
        return json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return []


async def load_archived_rows(db_session_factory) -> list[dict]:
    """Load archived DbSession rows with the fields the audit needs (list_archived
    omits repo/branch/goal, so query the model directly)."""
    from sqlalchemy import select as sa_select
    from server.db.models import DbSession
    from server.sessions.goals import effective_goal
    async with db_session_factory() as db:
        rows = (await db.execute(
            sa_select(DbSession).where(DbSession.status == "archived"))).scalars().all()
    out: list[dict] = []
    for row in rows:
        goal, source = effective_goal(row)
        out.append({"id": row.id, "repo": row.repo, "branch": row.branch,
                    "authored_branches": list(row.authored_branches or []),
                    "goal": goal, "goal_source": source})
    return out


async def main(argv: list[str] | None = None) -> int:
    import argparse
    from server.db.session import db_session
    from server.sessions.orchestrator import get_orchestrator

    parser = argparse.ArgumentParser(description="Audit archived sessions vs the completion bar.")
    parser.add_argument("--dry-run", action="store_true",
                        help="report only; do not unarchive premature sessions")
    args = parser.parse_args(argv)

    rows = await load_archived_rows(db_session)
    orch = get_orchestrator()
    results = await run_audit(rows, _real_gh, orch.unarchive, dry_run=args.dry_run)
    print(format_report(results))
    return 0


if __name__ == "__main__":  # pragma: no cover — thin CLI shell
    import sys
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
```

> Before writing Step 3, VERIFY the two real imports resolve in this codebase and fix
> the paths if they differ: (a) the async DB session factory — grep
> `def db_session` / `async def db_session` under `server/db/`; (b) the orchestrator
> accessor — grep `def get_orchestrator` under `server/sessions/`. If a singleton
> accessor doesn't exist, construct/obtain the orchestrator the same way the daemon
> entrypoint does (grep the daemon/`__main__` for how it builds the orchestrator) and
> use that here. Keep `main()` thin so `run_audit` (already tested) holds the logic.

- [ ] **Step 4: Run tests**

Run: `python -m pytest server/tests/test_archive_audit.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add server/automations/archive_audit.py server/tests/test_archive_audit.py
git commit -m "feat(autopilot): archive_audit CLI entrypoint (real gh + unarchive)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Full-suite green + lint/type gates

**Files:** none (verification + any fixes surfaced)

- [ ] **Step 1: Run the touched-area tests together**

Run: `python -m pytest server/tests/test_completion_bar.py server/tests/test_archive_audit.py server/tests/test_autopilot_policy_rules.py server/tests/test_autopilot_decider.py -q`
Expected: PASS

- [ ] **Step 2: Run the broader autopilot + orchestrator suites** (catch DecisionContext-construction fallout)

Run: `python -m pytest server/tests/ -q -k "autopilot or orchestrator or archive"`
Expected: PASS. If a test constructs `DecisionContext` positionally and breaks on the new field, fix the call (the field has a default, so only positional-after-ledger callers are affected).

- [ ] **Step 3: Lint + type gates**

Run: `ruff check server/automations/completion_bar.py server/automations/archive_audit.py server/automations/autopilot_policy.py server/automations/autopilot_decider.py server/sessions/orchestrator.py`
Run: `mypy server/automations/completion_bar.py server/automations/archive_audit.py server/automations/autopilot_policy.py`
Expected: clean (fix any `mypy` issues — required gate).

- [ ] **Step 4: Commit any fixes**

```bash
git add -A && git commit -m "test(autopilot): green suite + lint/type fixes for completion bar

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Unit 1 `completion_bar.met_bar` → Task 1. ✓
- Unit 2 inject pr_facts / guidance / hard gate → Task 3 (inject + guidance) + Task 2 (gate). ✓
- Unit 3 retro audit classify+unarchive+report → Task 4; entrypoint + real gh → Task 5. ✓
- Testing section (met_bar table, gate, get_context facts, decider prompt, audit with injected gh, dry-run) → Tasks 1–5 tests. ✓
- Error handling: stale cache never forces archive (gate only fires on positive un-armed evidence — Task 2); gh error → ambiguous (Task 4 `test_gh_error_is_ambiguous`); unarchive failure continues (Task 4 `run_audit` try/except). ✓
- CI gates → Task 6. ✓

**Placeholder scan:** No TBD/TODO. The one investigate-before-write note (Task 5 real imports) is a verification instruction with a concrete grep + fallback, not a code placeholder. ✓

**Type consistency:** `PrFacts(has_repo, prs)`, `PrFact(number, state, is_draft, auto_merge_enabled, url)`, `BarVerdict(met, reason, remedy, confidence)`, `met_bar(goal, goal_source, facts)`, `classify(...) -> (bucket, verdict)`, `run_audit(rows, gh_fn, unarchive_fn, *, dry_run)` — used identically across Tasks 1–5. `DecisionContext.pr_facts` default `None` consistent in Tasks 2 & 3. gh JSON keys (`state`,`isDraft`,`autoMergeRequest`,`url`,`number`) consistent between Task 4 fakes and Task 5 `_real_gh`/`_pr_fact_from_gh`. ✓
