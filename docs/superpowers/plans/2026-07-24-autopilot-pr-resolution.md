# Autopilot PR-Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the autopilot durable knowledge that a session finished-without-a-PR or cancelled its PR work, so it stops nudging "open a PR" and archives such sessions at the right time.

**Architecture:** A durable `pr_resolution` session field is set two ways — an explicit `<!-- ccswitch:no-pr -->` marker (deterministic, detected in the translator and threaded through `TurnEnd` → driver → `_persist_status`) and a gated Haiku classifier (probabilistic, run only on the "no-PR" decision branch during async context assembly). The pure `met_bar()` verdict gains a `pr_resolution` parameter whose "resolved" case is overridden by any real PR fact. Auto-archive is broadened from backlog-runner-only to any non-paused autopilot-driven session and gains a resolution trigger.

**Tech Stack:** Python 3, SQLAlchemy (async, SQLite dev / Postgres prod), dataclasses, pytest. LLM via `claude_agent_sdk.query` with an injected `_query_fn` seam (never called in tests).

## Global Constraints

- New DB columns MUST be nullable (dev SQLite `init_db` backfills nullable columns only; a NOT NULL column needs a real migration). — verbatim from CLAUDE.md dev notes.
- `completion_bar.py` stays **pure / no I/O** — it is the single source of truth consumed by both the live autopilot and the retro archive audit (constitution VI).
- Never write session state on the WS-attach coroutine; persist only via the `_persist_status` driver callback (detached-attach invariant).
- LLM plumbing MUST use an injected `_query_fn` so the real model is never called in tests (mirror `autopilot_decider.decide`).
- Run server tests with the repo's documented local invocation; CI runs ruff + mypy + pytest — all three must be green.
- Marker granularity: ONE marker `<!-- ccswitch:no-pr -->` + free-text reason (not two markers).
- Stale-resolution override: real PR facts always win over `pr_resolution`.

---

### Task 1: Durable `pr_resolution` fields on the model + record

**Files:**
- Modify: `server/db/models.py` (near line 113-119, alongside `autopilot_paused` / `auto_archive_opt_out_reason`)
- Modify: `server/sessions/types.py` (`SessionRecord` near line 53-56; `to_dict` near 124; `from_dict` near 158)
- Test: `server/tests/test_session_record_roundtrip.py` (create; if an equivalent record round-trip test module already exists, add cases there instead)

**Interfaces:**
- Produces: `DbSession.pr_resolution: str | None`, `DbSession.pr_resolution_source: str | None`; `SessionRecord.pr_resolution: str | None = None`, `SessionRecord.pr_resolution_source: str | None = None`; serialized as `"prResolution"` / `"prResolutionSource"` in `to_dict`/`from_dict`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_session_record_roundtrip.py
from server.sessions.types import SessionRecord


def test_pr_resolution_roundtrips_through_dict():
    rec = SessionRecord(id="s1", account="a", name="n")
    rec.pr_resolution = "cancelled: dropped the change"
    rec.pr_resolution_source = "marker"
    d = rec.to_dict()
    assert d["prResolution"] == "cancelled: dropped the change"
    assert d["prResolutionSource"] == "marker"
    back = SessionRecord.from_dict(d)
    assert back.pr_resolution == "cancelled: dropped the change"
    assert back.pr_resolution_source == "marker"


def test_pr_resolution_defaults_none():
    rec = SessionRecord(id="s1", account="a", name="n")
    assert rec.pr_resolution is None
    assert rec.pr_resolution_source is None
    assert SessionRecord.from_dict(rec.to_dict()).pr_resolution is None
```

Adjust the `SessionRecord(...)` constructor call to match the real required fields (read the dataclass head at `types.py:24`).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_session_record_roundtrip.py -v`
Expected: FAIL — `AttributeError: 'SessionRecord' object has no attribute 'pr_resolution'`

- [ ] **Step 3: Add the DB columns** (`server/db/models.py`, immediately after `auto_archive_opt_out_reason`'s column definition)

```python
    # Durable record that a session resolved it will NOT open a PR — either it
    # finished without one ("not-needed") or cancelled the PR work ("cancelled").
    # None = unresolved (default). Reason token may carry ": detail" for audit.
    # Source distinguishes the deterministic marker from the probabilistic classifier.
    pr_resolution: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    pr_resolution_source: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
```

Confirm `String` is already imported in `models.py` (it is used by `auto_archive_opt_out_reason`).

- [ ] **Step 4: Add the record fields + serialization** (`server/sessions/types.py`)

In the `SessionRecord` dataclass, after `auto_archive_opt_out_reason` (line ~56):

```python
    pr_resolution: str | None = None
    pr_resolution_source: str | None = None
```

In `to_dict` (after the `autoArchiveOptOut` entry, line ~125):

```python
            "prResolution": self.pr_resolution,
            "prResolutionSource": self.pr_resolution_source,
```

In `from_dict` (after the `auto_archive_opt_out_reason` entry, line ~159):

```python
            pr_resolution=d.get("prResolution"),
            pr_resolution_source=d.get("prResolutionSource"),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest server/tests/test_session_record_roundtrip.py -v`
Expected: PASS (both tests)

- [ ] **Step 6: Commit**

```bash
git add server/db/models.py server/sessions/types.py server/tests/test_session_record_roundtrip.py
git commit -m "feat(autopilot): durable pr_resolution fields on session model + record"
```

---

### Task 2: `NO_PR_MARKER` marker + instruction (single source of truth)

**Files:**
- Modify: `server/sessions/completion.py`
- Test: `server/tests/test_completion_marker.py` (create)

**Interfaces:**
- Produces: `completion.NO_PR_MARKER = "<!-- ccswitch:no-pr -->"`; `completion.NO_PR_INSTRUCTION` (str); `completion.resolution_from_text(text: str) -> str | None` returning `"cancelled"`, `"not-needed"`, or `None`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_completion_marker.py
from server.sessions.completion import NO_PR_MARKER, resolution_from_text


def test_marker_absent_returns_none():
    assert resolution_from_text("all done, PR opened") is None


def test_marker_defaults_to_not_needed():
    assert resolution_from_text(f"Nothing to ship.\n{NO_PR_MARKER}") == "not-needed"


def test_marker_with_cancel_phrasing_returns_cancelled():
    txt = f"User asked to cancel this PR work, dropping it.\n{NO_PR_MARKER}"
    assert resolution_from_text(txt) == "cancelled"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_completion_marker.py -v`
Expected: FAIL — `ImportError: cannot import name 'NO_PR_MARKER'`

- [ ] **Step 3: Implement in `completion.py`** (append after `COMPLETION_INSTRUCTION`)

```python
import re

NO_PR_MARKER = "<!-- ccswitch:no-pr -->"

NO_PR_INSTRUCTION = (
    "If the work is genuinely finished but NO pull request is appropriate — either "
    "the task needed no code change, or you have deliberately decided to cancel / not "
    f"open a PR — end your final message with `{NO_PR_MARKER}` on its own line "
    "(optionally preceded by a short reason). Use this INSTEAD of "
    f"`{COMPLETION_MARKER}` when finishing without a PR; never emit it while a PR is "
    "still expected or you are still working."
)

_CANCEL_RE = re.compile(
    r"\b(cancel(?:led|ling)?|abandon(?:ed|ing)?|drop(?:ped|ping)?|"
    r"revert(?:ed|ing)?|won'?t\s+open|not\s+opening|no\s+longer)\b",
    re.IGNORECASE,
)


def resolution_from_text(text: str) -> str | None:
    """Classify a final-turn text carrying NO_PR_MARKER into a resolution token.

    Returns None when the marker is absent. When present, 'cancelled' if the text
    reads as an explicit cancellation/abandonment, else 'not-needed'."""
    if text is None or NO_PR_MARKER not in text:
        return None
    return "cancelled" if _CANCEL_RE.search(text) else "not-needed"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_completion_marker.py -v`
Expected: PASS (all three)

- [ ] **Step 5: Wire the instruction into the session system prompt**

Find where `COMPLETION_INSTRUCTION` is appended to the system prompt:
Run: `grep -rn "COMPLETION_INSTRUCTION" server/ src/`
Add `NO_PR_INSTRUCTION` at the same site (append after `COMPLETION_INSTRUCTION`, joined by a blank line). If that site has its own test asserting prompt content, extend it to assert `NO_PR_MARKER` appears.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/completion.py server/tests/test_completion_marker.py <system-prompt-file>
git commit -m "feat(autopilot): NO_PR marker + reason classifier + system-prompt instruction"
```

---

### Task 3: Translator detects the marker → `TurnEnd.pr_resolution`

**Files:**
- Modify: `server/sessions/events.py` (`TurnEnd`, line 48-53)
- Modify: `server/sessions/translator.py` (ResultMessage branch, lines 146-168)
- Test: `server/tests/test_translator.py` (add cases; module exists)

**Interfaces:**
- Consumes: `completion.resolution_from_text` (Task 2).
- Produces: `TurnEnd.pr_resolution: str | None = None` (new dataclass field + `to_wire` key `"prResolution"`). Translator sets it on the terminal `TurnEnd` when the marker is present.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_translator.py  (add)
from server.sessions.completion import NO_PR_MARKER
# ... reuse the module's existing Translator construction helper ...

def test_no_pr_marker_sets_turn_end_pr_resolution(make_translator):
    tr = make_translator()
    tr._turn_text = f"Decided not to open a PR; cancelling.\n{NO_PR_MARKER}"
    events = tr._on_result(_success_result())   # use the module's existing result helper
    end = [e for e in events if e.__class__.__name__ == "TurnEnd"][0]
    assert end.outcome == "complete"
    assert end.pr_resolution == "cancelled"


def test_no_marker_leaves_pr_resolution_none(make_translator):
    tr = make_translator()
    tr._turn_text = "opened PR #12"
    end = [e for e in tr._on_result(_success_result()) if e.__class__.__name__ == "TurnEnd"][0]
    assert end.pr_resolution is None
```

Adapt to the module's actual Translator test scaffolding (construction + a helper that builds a successful `ResultMessage`). Read the top of `test_translator.py` for the existing pattern before writing.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_translator.py -k pr_resolution -v`
Expected: FAIL — `TypeError`/`AttributeError` (no `pr_resolution` on `TurnEnd`)

- [ ] **Step 3: Add the `TurnEnd` field** (`server/sessions/events.py`, line 48-53)

```python
@dataclass
class TurnEnd:
    turn_id: str; usage: dict | None = None; cost_usd: float | None = None
    src_uuid: str | None = None; parent_tool_use_id: str | None = None; outcome: str = "success"
    pr_resolution: str | None = None
    def to_wire(self): return {"type": "turnEnd", "turnId": self.turn_id, "usage": self.usage,
                               "costUsd": self.cost_usd, "srcUuid": self.src_uuid,
                               "parentToolUseId": self.parent_tool_use_id, "outcome": self.outcome,
                               "prResolution": self.pr_resolution}
```

- [ ] **Step 4: Set it in the translator** (`server/sessions/translator.py`, ResultMessage branch)

At line 151, the completion detection already exists. Compute the resolution and treat the marker as completion:

```python
            from server.sessions.completion import COMPLETION_MARKER, resolution_from_text
            pr_resolution = resolution_from_text(self._turn_text)
            if msg.subtype != "success" or bool(getattr(msg, "is_error", False)):
                outcome = "failed"
            elif COMPLETION_MARKER in self._turn_text or pr_resolution is not None:
                outcome = "complete"
            else:
                outcome = "success"
```

Capture `pr_resolution` BEFORE `self._reset()` clears `self._turn_text` (the reset at line 155). Then pass it into the `TurnEnd` construction (line 166-168):

```python
            out.append(ev.TurnEnd(tid, _usage_dict(msg.usage),
                                  getattr(msg, "total_cost_usd", None), getattr(msg, "uuid", None),
                                  outcome=outcome, pr_resolution=pr_resolution))
```

(If `COMPLETION_MARKER` is already imported at module top, drop it from the local import.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest server/tests/test_translator.py -k pr_resolution -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add server/sessions/events.py server/sessions/translator.py server/tests/test_translator.py
git commit -m "feat(autopilot): translator emits TurnEnd.pr_resolution on no-pr marker"
```

---

### Task 4: Driver captures resolution; `_persist_status` writes it

**Files:**
- Modify: `server/sessions/driver.py` (capture at line 681; init at 171; expose via a property)
- Modify: `server/sessions/orchestrator.py` (`_persist_status`, lines 1213-1275 — the `values` dict + record write)
- Test: `server/tests/test_ws.py` or the orchestrator persist test module (find with `grep -rln "_persist_status" server/tests`); plus a driver unit test in `server/tests/test_driver*.py`

**Interfaces:**
- Consumes: `TurnEnd.pr_resolution` (Task 3); `SessionRecord.pr_resolution*` (Task 1).
- Produces: `SessionDriver.last_pr_resolution: str | None` (property). After a `complete` turn carrying a resolution, `_persist_status` sets `rec.pr_resolution` / `rec.pr_resolution_source = "marker"` and writes both DB columns.

- [ ] **Step 1: Write the failing driver test**

```python
# server/tests/test_driver_pr_resolution.py  (create; adapt to the module's driver fixture)
def test_driver_captures_last_pr_resolution(make_driver):
    drv = make_driver()
    # feed a TurnEnd carrying a resolution through the driver's event consumer
    drv._consume_events([TurnEnd("t1", outcome="complete", pr_resolution="not-needed")])
    assert drv.last_pr_resolution == "not-needed"
```

Read an existing `test_driver*.py` for the real fixture + event-feeding helper before writing; match it.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_driver_pr_resolution.py -v`
Expected: FAIL — no `last_pr_resolution`

- [ ] **Step 3: Capture + expose in the driver** (`server/sessions/driver.py`)

Init alongside `self._last_turn_outcome` (line 171):

```python
        self._last_pr_resolution: str | None = None
```

At the TurnEnd handler (line 680-682):

```python
                    elif isinstance(e, ev.TurnEnd):
                        self._last_turn_outcome = e.outcome
                        self._last_turn_id = e.turn_id
                        if e.pr_resolution is not None:
                            self._last_pr_resolution = e.pr_resolution
```

Add a property near the other driver accessors:

```python
    @property
    def last_pr_resolution(self) -> str | None:
        return self._last_pr_resolution
```

- [ ] **Step 4: Persist in `_persist_status`** (`server/sessions/orchestrator.py`)

Inside `_persist_status`, after the `rec` in-memory block (~line 1258) and before/within the `values` build (~line 1263), add:

```python
        driver = self._drivers.get(session_id)
        resolution = driver.last_pr_resolution if driver is not None else None
        if status == "complete" and resolution and rec is not None and rec.pr_resolution is None:
            rec.pr_resolution = resolution
            rec.pr_resolution_source = "marker"
```

Then in the `values` dict (~line 1263, where `values: dict = {"status": status}` is built):

```python
            if status == "complete" and resolution and rec is not None and rec.pr_resolution == resolution:
                values["pr_resolution"] = resolution
                values["pr_resolution_source"] = "marker"
```

(The `rec.pr_resolution == resolution` guard makes the DB write idempotent with the in-memory set and avoids clobbering a pre-existing classifier value.)

- [ ] **Step 5: Write + run the orchestrator persist test**

Add a test that drives a session driver to a `complete` turn carrying `pr_resolution` and asserts the DB row + record both carry it (model it on the existing `_persist_status` tests found via grep). Run:

Run: `python -m pytest server/tests/test_driver_pr_resolution.py server/tests/<persist-test> -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add server/sessions/driver.py server/sessions/orchestrator.py server/tests/test_driver_pr_resolution.py
git commit -m "feat(autopilot): persist marker pr_resolution via _persist_status driver callback"
```

---

### Task 5: `met_bar()` gains `pr_resolution` precedence

**Files:**
- Modify: `server/automations/completion_bar.py` (`met_bar`, lines 80-98)
- Modify: `server/automations/autopilot_policy.py` (call site line 186)
- Modify: `server/automations/archive_audit.py` (call site line 209)
- Test: `server/tests/test_completion_bar.py` (extend)

**Interfaces:**
- Produces: `met_bar(goal, goal_source, facts, pr_resolution: str | None = None) -> BarVerdict`. When `pr_resolution` is truthy AND no PR exists (no merged/armed/open-unarmed), returns `BarVerdict(True, "session resolved no PR needed (<token>)", None, "high")`. Any real PR fact still takes precedence (resolution ignored).

- [ ] **Step 1: Write the failing tests** (append to `server/tests/test_completion_bar.py`)

```python
def test_pr_resolution_short_circuits_no_pr_branch():
    v = met_bar("fix ci", "explicit", _facts([], has_repo=True), pr_resolution="not-needed")
    assert v.met is True and v.confidence == "high"


def test_pr_resolution_ignored_when_open_unarmed_pr_exists():
    facts = _facts([_pr(state="open", auto_merge=False)])
    v = met_bar("fix ci", "explicit", facts, pr_resolution="cancelled")
    assert v.met is False  # real PR facts win — still nudge to arm it


def test_pr_resolution_ignored_when_merged():
    facts = _facts([_pr(state="merged")])
    v = met_bar("fix ci", "explicit", facts, pr_resolution="cancelled")
    assert v.met is True and "merged" in v.reason


def test_no_resolution_unchanged():
    v = met_bar("fix ci", "explicit", _facts([], has_repo=True))
    assert v.met is False and v.reason == "code goal but no PR opened"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest server/tests/test_completion_bar.py -k pr_resolution -v`
Expected: FAIL — `met_bar() got an unexpected keyword argument 'pr_resolution'`

- [ ] **Step 3: Implement** (`server/automations/completion_bar.py`)

Change the signature and insert the resolution branch AFTER the open-unarmed check, BEFORE the no-PR nudge (so real PRs win):

```python
def met_bar(goal: str, goal_source: str, facts: PrFacts,
            pr_resolution: str | None = None) -> BarVerdict:
    """Is the session's goal genuinely met? Precedence: merged > armed >
    open-unarmed > pr_resolution(no-PR) > no-PR-nudge. Only high-confidence
    verdicts are safe to auto-act on. A real PR fact always overrides a
    pr_resolution (a stale 'no PR' claim never masks an actual PR)."""
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
    if pr_resolution:
        return BarVerdict(True, f"session resolved no PR needed ({pr_resolution})",
                          None, "high")
    if facts.has_repo and _is_code_goal(goal):
        return BarVerdict(False, "code goal but no PR opened",
                          "open a PR and arm auto-merge before finishing", "low")
    return BarVerdict(True, "non-code goal — no PR expected", None, "low")
```

- [ ] **Step 4: Update both callers**

`autopilot_policy.py:186` — pass the resolution from the context (field added in Task 7; until then use `getattr` so this task's tests pass standalone):

```python
        verdict = met_bar(ctx.effective_goal, ctx.goal_source, ctx.pr_facts,
                          getattr(ctx, "pr_resolution", None))
```

`archive_audit.py:209` — pass the archived session's resolution. Read the surrounding function to get the session/context variable; add `pr_resolution=<row>.pr_resolution` (the audit reads archived `DbSession` rows). If the row object isn't in scope there, thread it from the caller. Add an audit test asserting a resolved-but-no-PR archived row buckets as `legit` (not `premature`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest server/tests/test_completion_bar.py server/tests/test_autopilot_policy.py server/tests/test_archive_audit.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add server/automations/completion_bar.py server/automations/autopilot_policy.py server/automations/archive_audit.py server/tests/test_completion_bar.py
git commit -m "feat(autopilot): met_bar pr_resolution precedence (real PR facts override)"
```

---

### Task 6: `pr_intent.classify_pr_intent` classifier module

**Files:**
- Create: `server/automations/pr_intent.py`
- Test: `server/tests/test_pr_intent.py` (create)

**Interfaces:**
- Produces: `PrIntentVerdict(resolved: bool, reason: str, confidence: str)` (frozen dataclass); `async classify_pr_intent(transcript_tail: list[dict], goal: str, *, _query_fn=None, _config_dir=None, _cwd=None) -> PrIntentVerdict`. Any error or ambiguous reply → `PrIntentVerdict(False, "", "low")`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_pr_intent.py
import pytest
from server.automations.pr_intent import PrIntentVerdict, classify_pr_intent


def _fake_query(reply: str):
    async def q(*, prompt, options=None):
        class _Blk:
            text = reply
        class _Msg:
            content = [_Blk()]
        yield _Msg()
    return q


@pytest.mark.asyncio
async def test_high_confidence_cancel_resolves():
    q = _fake_query('{"resolved": true, "reason": "cancelled", "confidence": "high"}')
    v = await classify_pr_intent([{"role": "assistant", "text": "we are dropping this"}],
                                 "fix ci", _query_fn=q)
    assert v == PrIntentVerdict(True, "cancelled", "high")


@pytest.mark.asyncio
async def test_ambiguous_is_unresolved():
    q = _fake_query('{"resolved": false, "reason": "", "confidence": "low"}')
    v = await classify_pr_intent([], "fix ci", _query_fn=q)
    assert v.resolved is False


@pytest.mark.asyncio
async def test_llm_error_returns_unresolved():
    async def boom(*, prompt, options=None):
        raise RuntimeError("model down")
        yield  # pragma: no cover
    v = await classify_pr_intent([], "fix ci", _query_fn=boom)
    assert v == PrIntentVerdict(False, "", "low")


@pytest.mark.asyncio
async def test_unparseable_reply_returns_unresolved():
    q = _fake_query("I think maybe not sure")
    v = await classify_pr_intent([], "fix ci", _query_fn=q)
    assert v.resolved is False
```

Confirm the async test marker style used elsewhere (`grep -rn "pytest.mark.asyncio\|asyncio_mode" server/tests server/pytest.ini pyproject.toml`); match it (the repo may use auto mode, in which case drop the decorators).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest server/tests/test_pr_intent.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement `server/automations/pr_intent.py`** (mirror `autopilot_decider`)

```python
"""Gated Haiku classifier: has a session decided NOT to open a PR? Mirrors
autopilot_decider — injected _query_fn so the real model is NEVER called in tests;
any failure/ambiguity degrades to an unresolved verdict (autopilot then falls back to
its normal no-PR nudge)."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

_SYSTEM = (
    "You judge whether a coding session has deliberately decided NOT to open a pull "
    "request — either the work needed no code change ('not-needed') or the session/user "
    "cancelled or abandoned the PR work ('cancelled'). You are NOT deciding whether a PR "
    "would be nice; only whether the session has already RESOLVED not to open one. When "
    "unsure, say resolved=false. Reply with ONLY a JSON object "
    '{"resolved": bool, "reason": "cancelled"|"not-needed"|"", "confidence": "high"|"low"} '
    "— no prose, no code fences."
)


@dataclass(frozen=True)
class PrIntentVerdict:
    resolved: bool
    reason: str
    confidence: str


_UNRESOLVED = PrIntentVerdict(False, "", "low")


def _default_sdk_query_fn():
    from claude_agent_sdk import query  # lazy — not available in tests
    return query


def _build_prompt(transcript_tail: list[dict], goal: str) -> str:
    payload = json.dumps({"goal": goal, "transcript_tail": transcript_tail[-40:]},
                         indent=2, default=str)
    return (payload + "\n\nHas this session resolved NOT to open a PR? "
            'Reply with ONLY the JSON object {"resolved","reason","confidence"}.')


def _extract_obj(raw: str) -> dict | None:
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
        if isinstance(obj, dict) and "resolved" in obj:
            return obj
        idx = start + 1


def _parse(raw: str) -> PrIntentVerdict:
    obj = _extract_obj(raw)
    if obj is None:
        return _UNRESOLVED
    if not bool(obj.get("resolved")):
        return _UNRESOLVED
    reason = obj.get("reason") if obj.get("reason") in ("cancelled", "not-needed") else "not-needed"
    confidence = "high" if obj.get("confidence") == "high" else "low"
    return PrIntentVerdict(True, reason, confidence)


async def classify_pr_intent(transcript_tail: list[dict], goal: str, *,
                             _query_fn=None, _config_dir=None, _cwd=None) -> PrIntentVerdict:
    query_fn = _query_fn or _default_sdk_query_fn()
    config_dir = _config_dir if _config_dir is not None else os.environ.get("CLAUDE_CONFIG_DIR", "")
    cwd = _cwd or os.getcwd()
    try:
        options = None
        if _query_fn is None:
            from claude_agent_sdk import ClaudeAgentOptions
            options = ClaudeAgentOptions(
                model="haiku", system_prompt=_SYSTEM, cwd=cwd,
                env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
                allowed_tools=[], setting_sources=[])
        parts: list[str] = []
        async for msg in query_fn(prompt=_build_prompt(transcript_tail, goal), options=options):
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for block in content:
                    t = getattr(block, "text", None)
                    if isinstance(t, str):
                        parts.append(t)
        return _parse("".join(parts).strip())
    except Exception as exc:  # pragma: no cover - model down / bad options
        log.warning("pr_intent.classify failed: %s", exc)
        return _UNRESOLVED
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest server/tests/test_pr_intent.py -v`
Expected: PASS (all four)

- [ ] **Step 5: Commit**

```bash
git add server/automations/pr_intent.py server/tests/test_pr_intent.py
git commit -m "feat(autopilot): gated Haiku classifier for PR-cancellation intent"
```

---

### Task 7: Gate the classifier in context assembly; policy respects resolution

**Files:**
- Modify: `server/automations/autopilot_policy.py` (`DecisionContext`, add `pr_resolution` field near line 60)
- Modify: `server/sessions/orchestrator.py` (`_autopilot_get_context`, lines 2134-2174; add a helper `_autopilot_resolve_pr_intent`)
- Modify: `server/automations/autopilot_guidance.md` (one line)
- Test: `server/tests/test_autopilot_context.py` (create) + extend `server/tests/test_autopilot_policy.py`

**Interfaces:**
- Consumes: `met_bar` w/ `pr_resolution` (Task 5); `classify_pr_intent` (Task 6); `SessionRecord.pr_resolution` (Task 1).
- Produces: `DecisionContext.pr_resolution: str | None = None`. `_autopilot_get_context` populates it: durable record value if set, else (only on the no-PR `met_bar` branch) a high-confidence classifier verdict, which it also persists to the record + DB.

- [ ] **Step 1: Write the failing policy test** (`server/tests/test_autopilot_policy.py`)

```python
def test_complete_not_downgraded_when_pr_resolution_set(base_ctx, base_config):
    ctx = base_ctx(status="complete", effective_goal="fix ci",
                   pr_facts=_facts_no_pr(), pr_resolution="not-needed")
    out = post_validate(Decision(action="complete"), ctx, base_config)
    assert out.action == "complete"  # resolved → NOT nudged to open a PR


def test_complete_downgraded_without_resolution(base_ctx, base_config):
    ctx = base_ctx(status="complete", effective_goal="fix ci", pr_facts=_facts_no_pr())
    out = post_validate(Decision(action="complete"), ctx, base_config)
    assert out.action == "nudge"  # unchanged legacy behavior
```

Adapt to the module's existing `DecisionContext` construction helper. `_facts_no_pr()` = `PrFacts(has_repo=True, prs=())`.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_autopilot_policy.py -k pr_resolution -v`
Expected: FAIL — `DecisionContext` has no `pr_resolution` (or the complete stays nudged)

- [ ] **Step 3: Add the `DecisionContext` field** (`autopilot_policy.py`, after `fleet_context`, ~line 60)

```python
    # Durable 'session resolved it will not open a PR' token (marker or high-confidence
    # classifier). When set and no real PR exists, met_bar treats the goal as met, so
    # post_validate stops downgrading `complete` to an open-a-PR nudge. None = unresolved.
    pr_resolution: str | None = None
```

The `post_validate` call site already reads `getattr(ctx, "pr_resolution", None)` (Task 5, step 4) — now change it to `ctx.pr_resolution` since the field exists.

- [ ] **Step 4: Populate it in `_autopilot_get_context`** (`orchestrator.py`)

Before building `DecisionContext` (line 2165), resolve the intent:

```python
        pr_facts = self._autopilot_pr_facts(rec)
        transcript_tail = await self._autopilot_transcript_tail(session_id)
        pr_resolution = await self._autopilot_resolve_pr_intent(
            rec, goal_text, goal_source, pr_facts, transcript_tail)
```

Then pass `pr_facts=pr_facts`, `transcript_tail=transcript_tail`, and `pr_resolution=pr_resolution` into the `DecisionContext(...)` (replacing the inline `pr_facts=` / `transcript_tail=` args).

Add the helper next to `_autopilot_pr_facts`:

```python
    async def _autopilot_resolve_pr_intent(self, rec, goal_text, goal_source,
                                           pr_facts, transcript_tail) -> str | None:
        """Durable PR-resolution for a governed session. Returns the record's stored
        value if set; else, ONLY on the 'code goal but no PR' met_bar branch, runs the
        gated Haiku classifier and persists a high-confidence 'resolved' verdict.
        Any classifier failure returns None (autopilot falls back to its normal nudge)."""
        existing = getattr(rec, "pr_resolution", None)
        if existing:
            return existing
        from server.automations.completion_bar import met_bar
        verdict = met_bar(goal_text, goal_source, pr_facts)
        # Only the low-confidence 'code goal but no PR opened' branch produces the buggy
        # nudge; that is the ONLY place intent can change the outcome.
        if verdict.met or verdict.confidence != "low":
            return None
        from server.automations.pr_intent import classify_pr_intent
        intent = await classify_pr_intent(transcript_tail, goal_text)
        if not (intent.resolved and intent.confidence == "high"):
            return None
        rec.pr_resolution = intent.reason
        rec.pr_resolution_source = "classifier"
        try:
            async with db_session() as db:
                from sqlalchemy import update as sa_update
                await db.execute(sa_update(DbSession).where(DbSession.id == rec.id).values(
                    pr_resolution=intent.reason, pr_resolution_source="classifier"))
        except Exception:  # pragma: no cover — a persist failure must not stall the tick
            log.debug("autopilot.pr_intent_persist_failed id=%s", rec.id, exc_info=True)
        return intent.reason
```

- [ ] **Step 5: Write the context-gating test** (`server/tests/test_autopilot_context.py`)

Drive `_autopilot_resolve_pr_intent` (via a lightweight orchestrator/rec harness, or unit-test the helper on a stub with the needed attrs) asserting:
- returns the stored value without calling the classifier when `rec.pr_resolution` is set,
- returns `None` (no classifier) when `pr_facts` has an armed/merged/open PR,
- calls the classifier and returns the reason ONLY on the no-PR branch, persisting source `"classifier"`.

Use a fake `classify_pr_intent` via monkeypatch to avoid the model. Read `test_autopilot_context.py` neighbors (e.g. an existing `test_autopilot*.py`) for the orchestrator test harness pattern first.

Run: `python -m pytest server/tests/test_autopilot_context.py server/tests/test_autopilot_policy.py -v`
Expected: PASS

- [ ] **Step 6: Add the guidance line** (`server/automations/autopilot_guidance.md`)

Add one bullet where PR guidance lives:

```
- If the session has clearly decided NOT to open a PR (finished without one, or
  cancelled the PR work), respect that decision — do NOT nudge it to open a PR;
  confirm completion instead.
```

- [ ] **Step 7: Commit**

```bash
git add server/automations/autopilot_policy.py server/sessions/orchestrator.py server/automations/autopilot_guidance.md server/tests/test_autopilot_context.py server/tests/test_autopilot_policy.py
git commit -m "feat(autopilot): gate PR-intent classifier on the no-PR branch; policy respects resolution"
```

---

### Task 8: Archive-audit provenance for resolved sessions (race-free)

> **REVISED during implementation.** The original Task 8 broadened
> `_maybe_autoarchive_completed` to non-backlog sessions. Investigation found that
> is both **redundant and racy**: the autopilot loop (`autopilot.py:136-162`) already
> archives goal-met governed sessions in **auto mode** via the `_autopilot_archive`
> seam, honoring the manual-unarchive opt-out (`ctx.no_auto_archive`). Task 7 already
> makes a `pr_resolution` session read as goal-met (`met_bar` → met=True → decider
> `complete`), so a resolved, autopilot-driven session **is already archived by the
> loop** — that IS "any autopilot-driven session, archived at the right time."
> `orchestrator.py:2125` deliberately keeps `_maybe_autoarchive_completed` scoped to
> backlog-runner so the two archive paths never race over the same session; broadening
> it would reintroduce exactly that race. So Task 8 does NOT touch
> `_maybe_autoarchive_completed`. Its remaining, real value is **audit provenance**:
> when the loop archives a resolved session, the archive context should say *why*
> (resolved no-PR), so the 6-hour archive audit and any human review can see it.

**Files:**
- Modify: `server/sessions/orchestrator.py` (`_autopilot_archive`, the `context` dict at lines ~2465-2469 — the loop's archive seam)
- Test: `server/tests/test_autopilot_orchestrator_seams.py` (extend)

**Interfaces:**
- Consumes: `SessionRecord.pr_resolution` (Task 1).
- Produces: `_autopilot_archive`'s snapshot `context["reason"]` reflects the PR resolution
  (`"resolved no PR (<token>)"`) when the archived session carries one; unchanged
  (`"autopilot: goal met"`) otherwise. `context["pr_resolution"]` carries the raw token.

- [ ] **Step 1: Write the failing test** (`server/tests/test_autopilot_orchestrator_seams.py`, using the `_bare_orch()` harness)

```python
@pytest.mark.asyncio
async def test_autopilot_archive_context_records_resolution(tmp_path):
    orch = _bare_orch()
    orch._records = {"s1": SessionRecord(id="s1", name="n", account="a", claude_session_id="",
                                         cols=80, rows=24, cwd=str(tmp_path), repo="o/r")}
    orch._records["s1"].pr_resolution = "cancelled"
    captured = {}
    async def _spy(session_id, *, archived_by="user", context=None):
        captured["by"] = archived_by
        captured["context"] = context
    orch.archive_session = _spy
    ok = await orch._autopilot_archive("s1")
    assert ok is True
    assert captured["by"] == "autopilot"
    assert "cancelled" in captured["context"]["reason"]
    assert captured["context"]["pr_resolution"] == "cancelled"


@pytest.mark.asyncio
async def test_autopilot_archive_context_default_reason_when_unresolved(tmp_path):
    orch = _bare_orch()
    orch._records = {"s1": SessionRecord(id="s1", name="n", account="a", claude_session_id="",
                                         cols=80, rows=24, cwd=str(tmp_path), repo="o/r")}
    captured = {}
    async def _spy(session_id, *, archived_by="user", context=None):
        captured["context"] = context
    orch.archive_session = _spy
    await orch._autopilot_archive("s1")
    assert captured["context"]["reason"] == "autopilot: goal met"
    assert captured["context"].get("pr_resolution") is None
```

Read the top of `test_autopilot_orchestrator_seams.py` and match the real `_bare_orch()`
signature + `SessionRecord(...)` required fields (adjust the constructor if needed). The
`_autopilot_archive` seam calls `self._autopilot_pr_facts(rec)`; a bare orch must have
whatever that needs — if it raises, give the rec an empty `authored_branches`/no PR so
`_autopilot_pr_facts` returns `PrFacts(has_repo=True, prs=())`, or stub
`orch._autopilot_pr_facts = lambda rec: PrFacts(has_repo=bool(rec.repo), prs=())`.

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_autopilot_orchestrator_seams.py -k archive_context -v`
Expected: FAIL — reason is `"autopilot: goal met"` regardless of resolution; no `pr_resolution` key

- [ ] **Step 3: Record the resolution in the archive context** (`_autopilot_archive`, the `context` dict at ~lines 2465-2469)

```python
            resolution = getattr(rec, "pr_resolution", None)
            context = {
                "repo": rec.repo, "goal": goal_text, "goal_source": goal_source,
                "pr_state": pr.to_prompt() if pr is not None else None,
                "pr_resolution": resolution,
                "reason": (f"resolved no PR ({resolution})" if resolution
                           else "autopilot: goal met"),
            }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_autopilot_orchestrator_seams.py -k archive_context -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_autopilot_orchestrator_seams.py
git commit -m "feat(autopilot): record PR-resolution provenance in the autopilot archive context"
```

---

### Task 9: Full-suite green + lint + types

**Files:** none (verification task)

- [ ] **Step 1: Run the targeted suites**

Run: `python -m pytest server/tests/test_completion_bar.py server/tests/test_pr_intent.py server/tests/test_autopilot_policy.py server/tests/test_autopilot_context.py server/tests/test_translator.py server/tests/test_completion_marker.py server/tests/test_session_record_roundtrip.py -v`
Expected: all PASS

- [ ] **Step 2: Run the broader server suite** (per the repo's documented local invocation, with the known deselections from CLAUDE.md / memory — e.g. `--ignore daemon_properties`, deselect `eta_parity_live`)

Run: `python -m pytest server/tests/ -q` (add the documented ignores/deselects)
Expected: no new failures beyond the known-flaky set

- [ ] **Step 3: Lint + types (CI-exact)**

Run: `ruff check server/ && mypy server/ --ignore-missing-imports`
Expected: clean (fix any findings — mypy is a required CI gate)

- [ ] **Step 4: Commit any lint/type fixes**

```bash
git add -A && git commit -m "chore(autopilot): lint + type fixes for pr-resolution feature"
```

---

## Self-Review

**Spec coverage:**
- §1 durable state → Task 1 ✓
- §2a marker → Tasks 2, 3, 4 ✓
- §2b classifier → Task 6 ✓
- §3 gating in context assembly → Task 7 ✓
- §4 met_bar precedence + post_validate + guidance → Tasks 5, 7 ✓
- §5 auto-archive broaden + trigger + audit compatibility → Tasks 5 (audit caller), 8 ✓
- §6 component boundaries → covered across tasks ✓
- Testing §1–6 → mapped into Tasks 1–8 test steps; Task 9 = full green ✓

**Type consistency:** `pr_resolution: str | None` used identically across record/DB/`TurnEnd`/`DecisionContext`/`met_bar`. `PrIntentVerdict(resolved, reason, confidence)` defined in Task 6, consumed in Task 7. `resolution_from_text` defined in Task 2, consumed in Task 3. `last_pr_resolution` defined in Task 4, consumed in `_persist_status` (Task 4). No name drift.

**Placeholder scan:** Test scaffolding steps say "adapt to the module's existing fixture" — deliberate, because the exact fixture name must be read from the target test module; the assertions and production code are concrete. No TBD/TODO in production code steps.
