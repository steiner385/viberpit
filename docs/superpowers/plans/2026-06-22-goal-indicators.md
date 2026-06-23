# Goal Indicators Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture each session's active `/goal` condition server-side and surface it in the web UI as a "Goal" badge + tooltip on every session card.

**Architecture:** `/goal` is a built-in Claude Code feature with no structured SDK event (verified, `claude-agent-sdk==0.2.105`). We capture the goal text at the orchestrator's command entry point (`run_command`) — the same place `/model` and `/account` are already intercepted — persist it to a new nullable `active_goal` column, serialize it through the existing REST wire format, and render a badge driven by the existing 5s `useSessions()` poll. We still forward `/goal …` to the CLI so the binary's real goal feature engages.

**Tech Stack:** Python FastAPI + SQLAlchemy(SQLite) backend; React + TS + Tailwind frontend; pytest + vitest.

## Global Constraints

- Run server tests against the main checkout's prebuilt env: `<main-checkout>/.venv/bin/python -m pytest …`. Web tests run in `web/` (`vitest run`, `tsc --noEmit`, `eslint`); symlink `web/node_modules` from the main checkout if missing.
- New DB columns MUST be **nullable** — `init_db` backfills missing nullable columns on dev SQLite without a migration. A NOT NULL column would need Alembic.
- Single source of truth / no parallel implementations (constitution VI): reuse the existing `/model` capture-and-persist pattern in `server/sessions/orchestrator.py`; reuse existing badge styling in `SessionCard.tsx`. Do **not** add a new React Query hook (project memory: adding a `@/api/hooks` hook to Dashboard breaks ~9 web tests' factory mocks).
- Verify against the real binary, not assumptions (constitution III) — Task 1 gate.
- The wire field name is `activeGoal` (camelCase); the Python/DB field is `active_goal` (snake_case).

---

### Task 1: Phase 0 — `/goal` conformance spike (gate)

Mirrors the existing slash-command spike pattern (`server/tests/integration/test_sdk_commands_spike.py`, verdicts in `docs/superpowers/spikes/2026-06-22-slash-commands-verdicts.md`). The probe needs a live Claude Max account, so like the existing spike it **skips cleanly** without credentials and downstream tasks proceed on a documented "assumed TRUE" working assumption.

**Files:**
- Create: `docs/superpowers/spikes/2026-06-22-goal-conformance-verdicts.md`

**Interfaces:**
- Produces: a documented verdict (or PENDING marker) for whether `/goal` engages via the SDK input path. Downstream tasks assume TRUE.

- [ ] **Step 1: Write the verdict doc**

Create `docs/superpowers/spikes/2026-06-22-goal-conformance-verdicts.md`:

```markdown
# /goal conformance spike verdicts (2026-06-22)

Built-in `/goal <condition>` (singular) is verified present in the Claude Code binary
(strings in versions/2.1.186: "No goal set. Usage: /goal <condition>", "/goal clear to
stop early"). It has NO structured channel in claude-agent-sdk==0.2.105 (no SystemMessage
subtype / hook / message class carries goal state — confirmed by inspecting the installed
package). Capture is therefore done at our own dispatcher.

## Gate G.1 — `/goal <condition>` sent via passthrough engages the built-in feature: PENDING

Assumed TRUE (working assumption for Tasks 2–5). To confirm, an operator with a live Max
account runs the existing slash spike harness against a trusted cwd and sends "/goal echo
done", checking the stream/CLI acknowledges a goal was set (vs. the text being treated as a
plain model turn).

Decision gate: if FALSE, the badge would be cosmetic — STOP and revisit before shipping the
frontend (Task 5). Backend capture (Tasks 2–4) is still valid as a record of UI-requested
goals regardless.

## Gate G.2 — workspace trust permits /goal: ASSUMED TRUE

Sessions are pre-trusted via configdir.trust_project() in the orchestrator before the SDK
client launches, and we set no hook-restriction flags, so /goal should not be trust-blocked.

## Gate G.3 — catalog classification: CONFIRMED

`goal` is not in EXCLUDED and not in CLIENT/CONTROL, so classify("goal") == "passthrough"
(server/sessions/commands/catalog.py). Correct: we want it forwarded to the CLI after capture.
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/spikes/2026-06-22-goal-conformance-verdicts.md
git commit -m "docs(goals): phase-0 conformance spike verdicts (assumed TRUE, gated on creds)"
```

---

### Task 2: Persistence model — DB column + `SessionRecord` field

**Files:**
- Modify: `server/db/models.py:62` (after the `model` column on `Session`)
- Modify: `server/sessions/types.py` (`SessionRecord` field + `to_dict`/`from_dict`)
- Modify: `server/sessions/orchestrator.py:600-617` and `:1473-1487` (two rehydrate sites)
- Test: `server/tests/test_session_record_goal.py` (create)

**Interfaces:**
- Produces: `SessionRecord.active_goal: str | None` (default `None`); DB column `sessions.active_goal` (nullable String); `SessionRecord.to_dict()` emits `"activeGoal"`, `from_dict()` reads `"activeGoal"`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_session_record_goal.py`:

```python
from server.sessions.types import SessionRecord


def _rec(**kw):
    base = dict(id="s1", name="n", account="a", claude_session_id="c1",
                cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_active_goal_defaults_none():
    assert _rec().active_goal is None


def test_active_goal_round_trips_through_dict():
    rec = _rec(active_goal="ship the PR")
    d = rec.to_dict()
    assert d["activeGoal"] == "ship the PR"
    assert SessionRecord.from_dict(d).active_goal == "ship the PR"


def test_from_dict_missing_active_goal_is_none():
    d = _rec().to_dict()
    del d["activeGoal"]
    assert SessionRecord.from_dict(d).active_goal is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_session_record_goal.py -v`
Expected: FAIL — `SessionRecord` has no `active_goal` / `to_dict` lacks `activeGoal`.

- [ ] **Step 3: Add the DB column**

In `server/db/models.py`, immediately after the `model` column (line 62), add:

```python
    # Active /goal condition for this session (the built-in Claude Code goal feature).
    # NULL = no active goal. Nullable so init_db backfills on dev SQLite without a migration.
    active_goal: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
```

- [ ] **Step 4: Add the `SessionRecord` field and dict mapping**

In `server/sessions/types.py`, add the field after `account_pinned` (line 33):

```python
    active_goal: str | None = None    # active /goal condition; None = no goal
```

In `to_dict()` (after the `"accountPinned"` entry), add:

```python
            "activeGoal": self.active_goal,
```

In `from_dict()` (after the `account_pinned=...` line), add:

```python
            active_goal=d.get("activeGoal"),
```

- [ ] **Step 5: Thread through both rehydrate sites**

In `server/sessions/orchestrator.py`, in the rehydrate `SessionRecord(...)` at ~line 600-617, after `account_pinned=...` add:

```python
                active_goal=getattr(row, "active_goal", None),
```

In the unarchive `SessionRecord(...)` at ~line 1473-1487, after `account_pinned=...` add:

```python
            active_goal=getattr(row, "active_goal", None),
```

- [ ] **Step 6: Run test to verify it passes**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_session_record_goal.py -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Commit**

```bash
git add server/db/models.py server/sessions/types.py server/sessions/orchestrator.py server/tests/test_session_record_goal.py
git commit -m "feat(goals): persist active_goal on Session model + SessionRecord"
```

---

### Task 3: Orchestrator goal capture + persistence

Reuses the `/model` precedent: a branch in `run_command` + a `set_goal_for_session` / `_persist_goal` pair mirroring `set_model_for_session` / `_persist_model` (`server/sessions/orchestrator.py:1261-1277`). Unlike `/model`, the goal branch does **not** return early — it captures, then falls through so the passthrough dispatcher still forwards `/goal …` to the CLI.

**Files:**
- Modify: `server/sessions/orchestrator.py` (`run_command` ~line 999; new methods near `set_model_for_session` ~line 1261)
- Test: `server/tests/test_goal_command.py` (create)

**Interfaces:**
- Consumes: `SessionRecord.active_goal` (Task 2); `cmd_dispatcher.dispatch` passthrough forwarding (existing).
- Produces: `SessionOrchestrator.set_goal_for_session(session_id: str, goal: str | None) -> None`; `_persist_goal(session_id, goal) -> None`. `run_command(sid, "goal", args)` sets `record.active_goal` (`None` on empty or `clear`) and forwards `/goal <args>` to the CLI, returning `None`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_goal_command.py`:

```python
import pytest
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk


async def _orch_with_goal_session():
    client = fake_sdk.FakeSDKClient(messages=[], server_info={"commands": [{"name": "goal"}]})
    orch = SessionOrchestrator(sdk_factory=lambda *a, **k: client)
    d = SessionDriver("s1", "a", "/tmp", "/tmp/c", mode="interactive",
                      sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                      identity="t")
    await d.start()
    orch._drivers["s1"] = d
    orch._records["s1"] = SessionRecord(id="s1", name="n", account="a",
                                        claude_session_id="c1", cols=80, rows=24, cwd="/tmp")

    async def _noop_persist(sid, goal):
        pass
    orch._persist_goal = _noop_persist  # type: ignore[attr-defined]
    return orch, client


@pytest.mark.anyio
async def test_goal_command_captures_and_forwards():
    orch, client = await _orch_with_goal_session()
    res = await orch.run_command("s1", "goal", "ship the PR")
    assert res is None                                   # passthrough
    assert orch._records["s1"].active_goal == "ship the PR"
    assert client.sent == ["/goal ship the PR"]          # still forwarded to the CLI


@pytest.mark.anyio
async def test_goal_clear_resets_active_goal():
    orch, client = await _orch_with_goal_session()
    await orch.run_command("s1", "goal", "ship the PR")
    res = await orch.run_command("s1", "goal", "clear")
    assert res is None
    assert orch._records["s1"].active_goal is None
    assert client.sent == ["/goal ship the PR", "/goal clear"]


@pytest.mark.anyio
async def test_goal_empty_args_clears():
    orch, client = await _orch_with_goal_session()
    await orch.run_command("s1", "goal", "ship the PR")
    await orch.run_command("s1", "goal", "")
    assert orch._records["s1"].active_goal is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_goal_command.py -v`
Expected: FAIL — `run_command` has no `goal` branch; `active_goal` stays `None`.

- [ ] **Step 3: Add the capture methods**

In `server/sessions/orchestrator.py`, immediately after `_persist_model` (ends ~line 1277), add:

```python
    async def set_goal_for_session(self, session_id: str, goal: str | None) -> None:
        """Record the session's active /goal condition AND persist it, so the badge
        survives rehydrate. The /goal text is still forwarded to the CLI by run_command;
        this only mirrors the state for the UI (no SDK method exists for goals)."""
        record = self._records.get(session_id)
        if record is not None:
            record.active_goal = goal
        await self._persist_goal(session_id, goal)

    async def _persist_goal(self, session_id: str, goal: str | None) -> None:
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id).values(active_goal=goal)
            )
```

- [ ] **Step 4: Add the `run_command` branch**

In `run_command`, after the `account` branch (after line 1015, before `info = await d.server_info()`), add:

```python
        if str(name).lstrip("/") == "goal":
            cond = (args or "").strip()
            cleared = (not cond) or cond.lower() == "clear"
            await self.set_goal_for_session(session_id, None if cleared else cond)
            # fall through: forward /goal to the CLI so the built-in feature engages
```

(Do **not** `return` — the existing passthrough dispatch below forwards `/goal <args>`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_goal_command.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_goal_command.py
git commit -m "feat(goals): capture /goal at run_command, persist, still forward to CLI"
```

---

### Task 4: API serialization (`activeGoal` in the wire dict)

**Files:**
- Modify: `server/api/sessions.py:85-99` (`_record_to_dict`)
- Test: `server/tests/test_sessions_api_goal.py` (create)

**Interfaces:**
- Consumes: `SessionRecord.active_goal` (Task 2).
- Produces: `GET /api/sessions` items and `_record_to_dict(record)` include `"activeGoal": str | None`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_sessions_api_goal.py`:

```python
from server.api.sessions import _record_to_dict
from server.sessions.types import SessionRecord


def _rec(**kw):
    base = dict(id="s1", name="n", account="a", claude_session_id="c1",
                cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_record_to_dict_includes_active_goal():
    d = _record_to_dict(_rec(active_goal="ship the PR"), repo=None)
    assert d["activeGoal"] == "ship the PR"


def test_record_to_dict_active_goal_none_by_default():
    d = _record_to_dict(_rec(), repo=None)
    assert d["activeGoal"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_sessions_api_goal.py -v`
Expected: FAIL — `KeyError: 'activeGoal'`.

- [ ] **Step 3: Add the field to `_record_to_dict`**

In `server/api/sessions.py`, in the dict returned by `_record_to_dict`, after the `"model"` entry (line 98) add:

```python
        "activeGoal": getattr(record, "active_goal", None),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_sessions_api_goal.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add server/api/sessions.py server/tests/test_sessions_api_goal.py
git commit -m "feat(goals): serialize activeGoal in the sessions wire format"
```

---

### Task 5: Frontend — `Session.activeGoal` type + SessionCard badge

**Files:**
- Modify: `web/src/api/types.ts:61` (`Session` interface)
- Modify: `web/src/components/SessionCard.tsx` (import + badge in the meta row)
- Test: `web/src/components/__tests__/SessionCard.goal.test.tsx` (create)

**Interfaces:**
- Consumes: `activeGoal` wire field (Task 4).
- Produces: `Session.activeGoal?: string | null`; a "Goal" pill (text `Goal`, `title` = goal text) rendered only when `activeGoal` is a non-empty string.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/__tests__/SessionCard.goal.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'

const base: Session = {
  id: '1',
  name: 'Fix login bug',
  accountName: 'work',
  status: 'in_progress',
  createdAt: '2026-06-22T11:00:00Z',
  updatedAt: '2026-06-22T11:58:00Z',
}

const noop = () => {}

function renderCard(session: Session) {
  return render(
    <MemoryRouter>
      <SessionCard session={session} account={undefined}
        onOpen={noop} onRename={noop} onClose={noop} onDelete={noop} />
    </MemoryRouter>,
  )
}

describe('SessionCard goal badge', () => {
  it('shows a Goal pill with the goal text as tooltip when activeGoal is set', () => {
    renderCard({ ...base, activeGoal: 'ship the PR' })
    const pill = screen.getByText('Goal')
    expect(pill).toBeInTheDocument()
    expect(pill).toHaveAttribute('title', 'Goal: ship the PR')
  })

  it('hides the Goal pill when activeGoal is null', () => {
    renderCard({ ...base, activeGoal: null })
    expect(screen.queryByText('Goal')).not.toBeInTheDocument()
  })

  it('hides the Goal pill when activeGoal is absent', () => {
    renderCard(base)
    expect(screen.queryByText('Goal')).not.toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/SessionCard.goal.test.tsx`
Expected: FAIL — no "Goal" pill rendered.

- [ ] **Step 3: Add the `Session` type field**

In `web/src/api/types.ts`, inside the `Session` interface after the `model` field (line 61), add:

```ts
  /** Active /goal condition for this session; null/absent = no goal. */
  activeGoal?: string | null
```

- [ ] **Step 4: Add the badge to SessionCard**

In `web/src/components/SessionCard.tsx`, add `Target` to the lucide import (line 2):

```tsx
import { Pencil, Archive, Trash2, FolderGit2, ChevronRight, ChevronDown, Target } from 'lucide-react'
```

In the meta row, immediately after `<StatusPill status={session.status} />` (line 129), add:

```tsx
          {session.activeGoal && (
            <span
              className="inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-indigo-100 dark:bg-indigo-900/40 text-indigo-700 dark:text-indigo-300 max-w-[12rem]"
              title={`Goal: ${session.activeGoal}`}
            >
              <Target size={12} aria-hidden="true" className="flex-shrink-0" />
              <span className="truncate">Goal</span>
            </span>
          )}
```

- [ ] **Step 5: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/components/__tests__/SessionCard.goal.test.tsx`
Expected: PASS (3 tests).

- [ ] **Step 6: Typecheck + lint + full card tests**

Run (in `web/`):
```bash
npx tsc --noEmit
npx eslint src/components/SessionCard.tsx src/api/types.ts src/components/__tests__/SessionCard.goal.test.tsx
npx vitest run src/components/__tests__/SessionCard.meta.test.tsx src/components/__tests__/SessionCard.goal.test.tsx
```
Expected: no type errors, no lint errors, all card tests PASS.

- [ ] **Step 7: Commit**

```bash
git add web/src/api/types.ts web/src/components/SessionCard.tsx web/src/components/__tests__/SessionCard.goal.test.tsx
git commit -m "feat(goals): show Goal badge + tooltip on session cards"
```

---

## Final verification (before PR)

- [ ] Server suite: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_session_record_goal.py server/tests/test_goal_command.py server/tests/test_sessions_api_goal.py server/tests/test_command_orchestrator.py server/tests/test_set_model_persists.py -v` — all green (goal tests + the patterns they extend).
- [ ] Web: in `web/` run `npx vitest run`, `npx tsc --noEmit`, `npx eslint .` — all green.
- [ ] Open PR per CLAUDE.md completion workflow (non-draft, `gh pr merge --squash --auto`).

## Notes on scope (from the spec)

Out of scope for this plan: setting/clearing goals from a dedicated UI control, goal history, auto-met/auto-cleared detection (needs text parsing — Channel B), capturing goals typed in a raw terminal, and live WS push (the 5s REST poll suffices). If Task 1's Gate G.1 resolves FALSE on a real-credential run, pause before Task 5 and reassess — backend capture (Tasks 2–4) remains valid as a record of UI-requested goals.
```
