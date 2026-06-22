# Conversation-based Auto-Naming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Claude's transcript title the live, always-fresh source of truth for a session's auto-name, surfaced on both the dashboard card and the in-session header.

**Architecture:** The orchestrator already adopts Claude's `aiTitle` from the transcript into the DB/record on every `status=="done"`. We extend `_maybe_apply_auto_name` to also push a `Title(source="claude")` event onto the live driver stream (so the header converges with the dashboard), and add a light periodic background loop that re-reads the title for active, still-auto sessions — catching titles Claude writes after the final turn or slowly mid-turn. The turn-boundary trigger is kept; the periodic loop is additive.

**Tech Stack:** Python 3 / asyncio / SQLAlchemy(SQLite) backend · pytest · React/TS frontend · vitest.

## Global Constraints

- Constitution v1.1.0: verify against the real binary/transcripts, not assumptions (III); single source of truth / no dead code (VI). The real transcript key is `{"type":"ai-title","aiTitle":"…"}`; `titles.py` already reads `aiTitle`/`ai-title`/`summary` and `customTitle`.
- Auto-naming must **never** raise into the session loop — title reads degrade to `None` (already true in `titles.py`).
- Existing guards are invariants: skip when `not name_is_auto` (respects manual rename); skip when `title == rec.name` (idempotent).
- New model columns must be nullable (dev SQLite never ALTERs). **This feature adds no DB columns** — no migration concern.
- Tests: run from the main checkout's prebuilt env, e.g. `<main-checkout>/.venv/bin/python -m pytest server/tests/…`. Web tests run in `web/` via `vitest run`.

---

### Task 1: Push adopted title to the live header

**Files:**
- Modify: `server/sessions/orchestrator.py:360-377` (`_maybe_apply_auto_name`)
- Test: `server/tests/test_orchestrator_status_name.py`

**Interfaces:**
- Consumes: `self._records[sid]: SessionRecord` (fields `name`, `name_is_auto`), `self._drivers: dict[str, SessionDriver]`, `self.transcript_path(sid) -> str | None`, `read_ai_title_from_file(path) -> str | None`, `ev.Title(title: str, source: str)`.
- Produces: `_maybe_apply_auto_name(session_id: str) -> None` — unchanged signature; now, when it adopts a new title and a live driver exists, also enqueues `ev.Title(title=<new>, source="claude")` onto `self._drivers[session_id]._out`.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_orchestrator_status_name.py` (uses a stub driver since `_build_orch` rehydrates detached sessions with no live driver):

```python
import asyncio
import types
from server.sessions import events as ev


def _attach_stub_driver(orch, sid):
    """Give a (detached) session a live-driver stand-in exposing just an _out queue."""
    drv = types.SimpleNamespace(_out=asyncio.Queue())
    orch._drivers[sid] = drv
    return drv


def test_auto_name_emits_title_event_to_live_header(tmp_path):
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text('{"type":"ai-title","aiTitle":"Fix the login bug"}\n')

    async def run():
        orch = await _build_orch(tmp_path)
        orch.transcript_path = lambda sid: str(transcript)
        drv = _attach_stub_driver(orch, "s1")
        await orch._persist_status("s1", "done")
        events = []
        while not drv._out.empty():
            events.append(drv._out.get_nowait())
        return orch.record("s1").name, events

    name, events = asyncio.run(run())
    assert name == "Fix the login bug"
    titles = [e for e in events if isinstance(e, ev.Title)]
    assert len(titles) == 1
    assert titles[0].title == "Fix the login bug"
    assert titles[0].source == "claude"


def test_auto_name_idempotent_no_event_when_unchanged(tmp_path):
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text('{"type":"ai-title","aiTitle":"Fix the login bug"}\n')

    async def run():
        orch = await _build_orch(tmp_path)
        orch.transcript_path = lambda sid: str(transcript)
        drv = _attach_stub_driver(orch, "s1")
        await orch._persist_status("s1", "done")   # adopts → 1 event
        while not drv._out.empty():
            drv._out.get_nowait()
        await orch._persist_status("s1", "done")   # same title → no event
        return drv._out.empty()

    assert asyncio.run(run()) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_orchestrator_status_name.py::test_auto_name_emits_title_event_to_live_header server/tests/test_orchestrator_status_name.py::test_auto_name_idempotent_no_event_when_unchanged -v`
Expected: FAIL — no `Title` event is enqueued (the method doesn't touch `_out` yet).

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/orchestrator.py`, replace the body of `_maybe_apply_auto_name` (currently lines 360-377) with:

```python
    async def _maybe_apply_auto_name(self, session_id: str) -> None:
        """If the session name is still auto, adopt Claude's transcript title.

        Updates the DB + in-memory record and, when a live driver exists, pushes a
        ``Title(source="claude")`` event so the in-session header converges with the
        dashboard. Idempotent: a title equal to the current name is a no-op.
        """
        rec = self._records.get(session_id)
        if rec is None or not rec.name_is_auto:
            return
        path = self.transcript_path(session_id)
        if not path:
            return
        title = await asyncio.to_thread(read_ai_title_from_file, path)
        if not title or title == rec.name:
            return
        rec.name = title
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id).values(name=title)
            )
        driver = self._drivers.get(session_id)
        if driver is not None:
            await driver._out.put(ev.Title(title=title, source="claude"))
        log.info("session.auto_name id=%s title=%r", session_id, title)
```

Ensure `events` is imported as `ev` at the top of the file (it is used elsewhere; if not present, add `from server.sessions import events as ev`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_orchestrator_status_name.py -v`
Expected: PASS (all tests in the file, including the pre-existing `test_auto_name_applied_on_done` and `test_auto_name_not_applied_when_manual`).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_status_name.py
git commit -m "feat(sessions): push adopted Claude title to the live header

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Periodic titling loop

**Files:**
- Modify: `server/sessions/orchestrator.py` — `__init__` (~61-113), `start` (126-131), `stop` (133-142); add `_titling_monitor` + `_titling_sweep`.
- Test: `server/tests/test_titling_loop.py` (create)

**Interfaces:**
- Consumes: `_maybe_apply_auto_name(session_id)` (Task 1), `self._records: dict[str, SessionRecord]` (field `status`).
- Produces:
  - `__init__(..., titling_interval: float = 15.0)` — new keyword arg, stored as `self._titling_interval`.
  - `self._titling_task: asyncio.Task | None` — started in `start()`, cancelled in `stop()`.
  - `async _titling_sweep(self) -> None` — one pass: for each record with `status != "ended"` and `name_is_auto`, call `_maybe_apply_auto_name`. Tested directly (no timer).
  - `async _titling_monitor(self) -> None` — `while True: await _titling_sweep(); await asyncio.sleep(self._titling_interval)`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_titling_loop.py`:

```python
"""Tests for the periodic titling sweep — adopts late-written titles with no new turn."""
import asyncio

from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _seed(rows):
    async with db_session() as db:
        for r in rows:
            db.add(DbSession(**r))


async def _build_orch(tmp_path, **row):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"
    await init_db(db_url=db_url)
    base = dict(id="s1", name="New Session", account_name="acct1",
                claude_session_id="claude-s1", status="in_progress", cwd="/repo/s1")
    base.update(row)
    await _seed([base])
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    return orch


def test_titling_sweep_adopts_title_without_a_turn(tmp_path):
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text('{"type":"ai-title","aiTitle":"Investigate slow query"}\n')

    async def run():
        orch = await _build_orch(tmp_path)
        orch.transcript_path = lambda sid: str(transcript)
        await orch._titling_sweep()           # no _persist_status / no "done"
        return orch.record("s1").name

    assert asyncio.run(run()) == "Investigate slow query"


def test_titling_sweep_skips_ended_sessions(tmp_path):
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text('{"type":"ai-title","aiTitle":"Should not apply"}\n')

    async def run():
        orch = await _build_orch(tmp_path, status="ended")
        orch.transcript_path = lambda sid: str(transcript)
        await orch._titling_sweep()
        return orch.record("s1").name

    assert asyncio.run(run()) == "New Session"


def test_titling_interval_defaults_to_15s(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        return orch._titling_interval
    assert asyncio.run(run()) == 15.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_titling_loop.py -v`
Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute '_titling_sweep'` (and `_titling_interval`).

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/orchestrator.py`:

a) Add the constructor arg. Change the signature (lines 61-68) to include `titling_interval`:

```python
    def __init__(
        self,
        sdk_factory=None,
        work_dir: str | None = None,
        poll_interval: float = 60.0,
        usage_fn: Callable[[str], object] | None = None,
        repo_root: str | None = None,
        titling_interval: float = 15.0,
    ) -> None:
```

b) Where background-task attributes are declared (line 113, `self._rotation_task = ...`), add:

```python
        self._titling_interval: float = titling_interval
        self._titling_task: asyncio.Task | None = None
```

c) In `start()` (after the rotation task is created, line 131), add:

```python
        self._titling_task = asyncio.create_task(
            self._titling_monitor(), name="titling-monitor"
        )
```

d) In `stop()` (after the rotation-task teardown block, before `await self._poller.stop()`), add:

```python
        if self._titling_task is not None:
            self._titling_task.cancel()
            try:
                await self._titling_task
            except asyncio.CancelledError:
                pass
            self._titling_task = None
```

e) Add the sweep + monitor methods near `_maybe_apply_auto_name`:

```python
    async def _titling_sweep(self) -> None:
        """One titling pass: refresh the auto-name of every active, still-auto session.

        The turn-boundary trigger ("done") handles the common case; this catches titles
        Claude writes after the final turn or slowly mid-turn. Never raises.
        """
        for sid, rec in list(self._records.items()):
            if rec.status == "ended" or not rec.name_is_auto:
                continue
            try:
                await self._maybe_apply_auto_name(sid)
            except Exception:  # never let one session break the sweep
                log.warning("titling sweep failed for %s", sid, exc_info=True)

    async def _titling_monitor(self) -> None:
        """Background loop: sweep titles every ``self._titling_interval`` seconds."""
        while True:
            await self._titling_sweep()
            await asyncio.sleep(self._titling_interval)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_titling_loop.py -v`
Expected: PASS (all three).

- [ ] **Step 5: Run the broader suite for regressions**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_orchestrator_status_name.py server/tests/test_titles.py server/tests/test_session_name_is_auto.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_titling_loop.py
git commit -m "feat(sessions): add light periodic titling sweep

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Frontend — lock in `source="claude"` header update

**Files:**
- Test: `web/src/session/reducer.title.test.ts` (create)
- (No code change expected: `EvTitle.source` is already `string` and the `title` reducer case already updates `state.title` regardless of source. This task is a regression guard. If `tsc` flags the new event, widen the type then.)

**Interfaces:**
- Consumes: `transcriptReducer(state, ev)`, `initialTranscript` from `web/src/session/reducer.ts`; `EvTitle { type: 'title'; title: string; source: string }` from `web/src/session/events.ts`.
- Produces: nothing consumed downstream — a test asserting a `title` event with `source: 'claude'` sets `state.title`.

- [ ] **Step 1: Write the failing test**

Create `web/src/session/reducer.title.test.ts`:

```typescript
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'

describe('transcriptReducer — title', () => {
  it('updates the header title from a Claude-sourced title event', () => {
    const s = transcriptReducer(initialTranscript, {
      type: 'title', title: 'Investigate slow query', source: 'claude',
    })
    expect(s.title).toBe('Investigate slow query')
  })

  it('a later Claude title supersedes an earlier prompt-sourced one', () => {
    const s1 = transcriptReducer(initialTranscript, {
      type: 'title', title: 'fix the', source: 'prompt',
    })
    const s2 = transcriptReducer(s1, {
      type: 'title', title: 'Fix flaky auth test', source: 'claude',
    })
    expect(s2.title).toBe('Fix flaky auth test')
  })
})
```

- [ ] **Step 2: Run the test**

Run (in `web/`): `npx vitest run src/session/reducer.title.test.ts`
Expected: PASS immediately (the reducer already handles this). If it FAILS to typecheck on the `source` field, proceed to Step 3; otherwise skip to Step 4.

- [ ] **Step 3: (Only if Step 2 type-errors) widen the type**

The `source` field is already `string` in `web/src/session/events.ts:14`, so no change should be needed. If a stricter union is introduced elsewhere, add `'claude'` to it. Otherwise leave the code untouched.

- [ ] **Step 4: Typecheck + lint**

Run (in `web/`): `npx tsc --noEmit && npx eslint src/session/reducer.title.test.ts`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/reducer.title.test.ts
git commit -m "test(web): lock in Claude-sourced header title updates

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Full verification + PR

**Files:** none (verification only).

- [ ] **Step 1: Backend suite**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/ -q`
Expected: PASS (no regressions). Note: API auth tests may need `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1` per CLAUDE.md.

- [ ] **Step 2: Web checks**

Run (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint .`
Expected: PASS / clean.

- [ ] **Step 3: Lint backend**

Run: `<main-checkout>/.venv/bin/python -m ruff check server/sessions/orchestrator.py server/tests/test_titling_loop.py`
Expected: clean.

- [ ] **Step 4: Push + open PR**

```bash
git push -u origin feat/conversation-based-auto-naming
gh pr create --fill --base master
```

PR body ends with the Claude Code trailer (`🤖 Generated with [Claude Code](https://claude.com/claude-code)`). Merge once mergeable per CLAUDE.md completion workflow.

---

## Self-Review

**Spec coverage:**
- "Single source of truth = Claude's title, on both surfaces" → Task 1 (header event) + existing DB path. ✓
- "Keep both triggers (turn-boundary + periodic)" → `_persist_status`'s `"done"` call kept untouched; Task 2 adds the periodic sweep calling the same method. ✓
- "Periodic re-read for active, still-auto sessions; skip ended; ~15s configurable" → Task 2 (`_titling_sweep` filter + `titling_interval=15.0`). ✓
- "Re-read even after a title exists (track topic shifts)" → sweep does not stop after first adoption; idempotency guard prevents redundant writes/events. ✓
- "First-prompt slug demoted to placeholder" → no `driver.py:213` change; `source="claude"` event supersedes it in the header (covered by Task 3 test). ✓
- "Title read never raises" → `titles.py` unchanged; sweep wraps each session in try/except. ✓
- "Manual rename / idempotence guards" → Task 1 tests `test_auto_name_idempotent…` and existing `test_auto_name_not_applied_when_manual`. ✓
- "Detached session: update DB, skip _out push" → Task 1 implementation guards on `self._drivers.get(session_id)`. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code. Task 3 Step 3 is conditional but specifies the exact action and the likely no-op outcome. ✓

**Type consistency:** `_maybe_apply_auto_name(session_id)`, `_titling_sweep()`, `_titling_monitor()`, `_titling_interval`, `_titling_task`, `ev.Title(title=, source="claude")` used consistently across tasks and tests. Test helper `_build_orch` mirrors the existing one in `test_orchestrator_status_name.py`. ✓
