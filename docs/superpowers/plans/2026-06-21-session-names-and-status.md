# Auto Session Names + Rich Status Indicators — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show an auto-generated name (Claude's own title) and a richer derived status (in_progress / waiting_input / ready_review / done) on every dashboard session row.

**Architecture:** A pure `derive_status()` folds signals the `SessionDriver` already holds (active turn, pending permission/question, pending plan, turns completed) into one dashboard status. The driver fires an `on_status` callback at each transition; the orchestrator persists it onto the in-memory `SessionRecord.status` (+ DB), which the dashboard's existing 5s poll surfaces. Names resolve as user-set → Claude `ai-title` (read from the transcript JSONL) → first-prompt heuristic, gated by a `name_is_auto` flag so manual renames are never overwritten. The in-session `StatusBar` keeps its existing `working`/`idle` live vocabulary untouched.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy 2 async / pytest-asyncio (backend); React / TypeScript / Vitest / lucide-react (frontend).

## Global Constraints

- Never store token/secret material in the app DB or audit detail (Constitution).
- Reuse Claude's native title; do NOT add a summarization LLM call.
- Title reader must never raise into the session loop — return `None` on any failure.
- Schema is managed via `Base.metadata.create_all` (see `server/db/__init__.py`); adding a model column is sufficient. Do not fabricate an Alembic migration (the `versions/` chain is empty).
- Backend tests: `python -m pytest <path> -v`. Frontend tests: `npm --prefix web test -- <path>` (Vitest).
- Status precedence (highest first): `ready_review` > `waiting_input` > `in_progress` > `done` > `idle`. `limited`/`ended` are orchestrator-managed overlays, outside `derive_status`.

---

### Task 1: Pure `derive_status` function

**Files:**
- Create: `server/sessions/status.py`
- Test: `server/tests/test_derive_status.py`

**Interfaces:**
- Produces: `StatusSignals` dataclass with fields
  `turn_active: bool, pending_permission: bool, pending_question: bool, pending_plan: bool, turns_completed: int`
  and `derive_status(sig: StatusSignals) -> str` returning one of
  `"ready_review" | "waiting_input" | "in_progress" | "done" | "idle"`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_derive_status.py
import pytest
from server.sessions.status import StatusSignals, derive_status

def S(**kw):
    base = dict(turn_active=False, pending_permission=False,
                pending_question=False, pending_plan=False, turns_completed=0)
    base.update(kw)
    return StatusSignals(**base)

@pytest.mark.parametrize("sig,expected", [
    (S(),                                                        "idle"),
    (S(turns_completed=1),                                       "done"),
    (S(turn_active=True),                                        "in_progress"),
    (S(turn_active=True, turns_completed=2),                     "in_progress"),
    (S(turn_active=True, pending_permission=True),              "waiting_input"),
    (S(turn_active=True, pending_question=True),                "waiting_input"),
    (S(pending_permission=True),                                 "waiting_input"),
    (S(turn_active=True, pending_permission=True, pending_plan=True), "ready_review"),
    (S(pending_plan=True),                                       "ready_review"),
    # precedence: plan outranks a plain pending permission
    (S(pending_permission=True, pending_plan=True),             "ready_review"),
])
def test_derive_status(sig, expected):
    assert derive_status(sig) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_derive_status.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.sessions.status'`

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/status.py
"""Pure derivation of a session's dashboard status from live driver signals.

Kept dependency-free and side-effect-free so it is exhaustively table-testable.
Precedence (highest first): a pending plan-approval, then any prompt-for-the-user,
then an active turn, then a completed turn, else never-ran. limited/ended are
orchestrator overlays and intentionally NOT modelled here.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StatusSignals:
    turn_active: bool
    pending_permission: bool
    pending_question: bool
    pending_plan: bool
    turns_completed: int


def derive_status(sig: StatusSignals) -> str:
    if sig.pending_plan:
        return "ready_review"
    if sig.pending_permission or sig.pending_question:
        return "waiting_input"
    if sig.turn_active:
        return "in_progress"
    if sig.turns_completed >= 1:
        return "done"
    return "idle"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_derive_status.py -v`
Expected: PASS (11 cases)

- [ ] **Step 5: Commit**

```bash
git add server/sessions/status.py server/tests/test_derive_status.py
git commit -m "feat(sessions): pure derive_status precedence function"
```

---

### Task 2: Transcript `ai-title` reader

**Files:**
- Create: `server/sessions/titles.py`
- Test: `server/tests/test_titles.py`

**Interfaces:**
- Produces: `read_ai_title(projects_root: str | Path, claude_session_id: str) -> str | None`.
  Globs `**/<claude_session_id>.jsonl` under `projects_root`, scans lines for the
  most recent object carrying `customTitle` (preferred) or `ai-title`/`aiTitle`/`summary`,
  returns the stripped title or `None`. Never raises.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_titles.py
import json
from pathlib import Path
from server.sessions.titles import read_ai_title

def _write(root: Path, sid: str, lines: list[dict]):
    d = root / "projects" / "encoded-cwd"
    d.mkdir(parents=True)
    (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines))

def test_reads_ai_title(tmp_path):
    _write(tmp_path, "sid1", [
        {"type": "user", "message": {"content": "hi"}},
        {"ai-title": "Fix the login bug"},
    ])
    assert read_ai_title(tmp_path, "sid1") == "Fix the login bug"

def test_custom_title_wins_over_ai_title(tmp_path):
    _write(tmp_path, "sid2", [
        {"ai-title": "auto title"},
        {"customTitle": "My Renamed Session"},
    ])
    assert read_ai_title(tmp_path, "sid2") == "My Renamed Session"

def test_missing_file_returns_none(tmp_path):
    assert read_ai_title(tmp_path, "nope") is None

def test_malformed_lines_do_not_raise(tmp_path):
    d = tmp_path / "projects" / "x"; d.mkdir(parents=True)
    (d / "sid3.jsonl").write_text("{not json\n{\"ai-title\": \"ok\"}\n")
    assert read_ai_title(tmp_path, "sid3") == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_titles.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.sessions.titles'`

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/titles.py
"""Read Claude's own session title from the transcript JSONL.

Claude records customTitle (user-set) and ai-title (model-generated) in the
per-session transcript. We reuse that rather than summarising ourselves. Any
I/O or parse error degrades to None — this must never break the session loop.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_CUSTOM_KEYS = ("customTitle",)
_AUTO_KEYS = ("ai-title", "aiTitle", "summary")


def read_ai_title(projects_root: str | Path, claude_session_id: str) -> str | None:
    if not claude_session_id:
        return None
    try:
        root = Path(projects_root)
        matches = list(root.glob(f"**/{claude_session_id}.jsonl"))
        if not matches:
            return None
        custom: str | None = None
        auto: str | None = None
        for path in matches:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                for k in _CUSTOM_KEYS:
                    v = obj.get(k)
                    if isinstance(v, str) and v.strip():
                        custom = v.strip()
                for k in _AUTO_KEYS:
                    v = obj.get(k)
                    if isinstance(v, str) and v.strip():
                        auto = v.strip()
        return custom or auto
    except Exception:  # never raise into the session loop
        log.warning("read_ai_title failed for %s", claude_session_id, exc_info=True)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_titles.py -v`
Expected: PASS (4 cases)

- [ ] **Step 5: Commit**

```bash
git add server/sessions/titles.py server/tests/test_titles.py
git commit -m "feat(sessions): read Claude ai-title/customTitle from transcript"
```

---

### Task 3: `name_is_auto` on the model + record

**Files:**
- Modify: `server/db/models.py` (Session class, after `permission_mode`)
- Modify: `server/sessions/types.py` (`SessionRecord`)
- Test: `server/tests/test_session_name_is_auto.py`

**Interfaces:**
- Produces: `DbSession.name_is_auto: bool` (default True) and
  `SessionRecord.name_is_auto: bool = True`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_session_name_is_auto.py
from server.sessions.types import SessionRecord
from server.db.models import Session as DbSession

def test_record_defaults_name_is_auto_true():
    r = SessionRecord(id="i", name="New Session", account="a",
                      claude_session_id="c", cols=80, rows=24, cwd="/tmp")
    assert r.name_is_auto is True

def test_db_model_has_name_is_auto_column():
    assert "name_is_auto" in DbSession.__table__.columns
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_session_name_is_auto.py -v`
Expected: FAIL (`AttributeError`/`KeyError` — column/field missing)

- [ ] **Step 3: Write minimal implementation**

In `server/db/models.py`, inside `class Session`, after the `permission_mode` line add:

```python
    name_is_auto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
```

In `server/sessions/types.py`, inside `SessionRecord` after `status`:

```python
    name_is_auto: bool = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_session_name_is_auto.py -v`
Expected: PASS (2 cases)

- [ ] **Step 5: Commit**

```bash
git add server/db/models.py server/sessions/types.py server/tests/test_session_name_is_auto.py
git commit -m "feat(db): add name_is_auto flag to session model + record"
```

---

### Task 4: Driver emits derived dashboard status via `on_status`

**Files:**
- Modify: `server/sessions/driver.py`
- Test: `server/tests/test_driver_status.py`

**Interfaces:**
- Consumes: `derive_status`, `StatusSignals` (Task 1); `PermissionRegistry.pending`, `QuestionRegistry.pending`.
- Produces: `SessionDriver.__init__` gains `on_status: Callable[[str, str], Awaitable[None]] | None = None`.
  Driver tracks `self._turn_active: bool` and `self._turns_completed: int`; new
  `_dash_signals() -> StatusSignals` and `async _emit_dashboard_status()` that derives
  and awaits `on_status(self.session_id, status)`. Called at: `_start_turn` (after setting
  `_turn_active=True`), in `_can_use_tool`/`_handle_ask_user_question` immediately before and
  after the `await`, in `_drain` finally (after `_turn_active=False`, `_turns_completed += 1`),
  and in `interrupt`.

**Notes for implementer:** locate the existing fields block in `__init__` (near `self._titled = False`),
the `_start_turn`, `_drain`, `_can_use_tool`, `_handle_ask_user_question`, and `interrupt` methods.
`pending_plan` = any pending permission whose `tool == "ExitPlanMode"`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_driver_status.py
import asyncio
import pytest
from server.sessions.driver import SessionDriver
from server.tests.fakes import fake_sdk as f

pytestmark = pytest.mark.asyncio

async def _make_driver(captured):
    async def on_status(sid, status):
        captured.append(status)
    d = SessionDriver(
        session_id="s1", cwd="/tmp", config_dir="/tmp", mode="full-auto",
        resume=None, factory=f.fake_factory(), on_status=on_status,
    )
    return d

async def test_status_progresses_in_progress_then_done():
    captured: list[str] = []
    d = await _make_driver(captured)
    await d.start()
    await d.send_prompt("hello")
    # drain the pump to completion
    if d._pump:
        await d._pump
    assert "in_progress" in captured
    assert captured[-1] == "done"
```

(Adapt `SessionDriver(...)` kwargs to the real constructor signature; mirror the
construction used in the existing `server/tests/test_driver.py`. The two assertions —
`in_progress` appears and final is `done` — are the contract.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_driver_status.py -v`
Expected: FAIL (`TypeError: __init__ got unexpected keyword 'on_status'`)

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/driver.py`:

1. Import at top: `from .status import StatusSignals, derive_status`.
2. Add `on_status=None` to `__init__` params and store `self._on_status = on_status`.
   In the fields block add:

```python
        self._turn_active: bool = False
        self._turns_completed: int = 0
```

3. Add helpers (place near `_can_use_tool`):

```python
    def _dash_signals(self) -> StatusSignals:
        perms = self._reg.pending(self.session_id)
        return StatusSignals(
            turn_active=self._turn_active,
            pending_permission=bool(perms),
            pending_question=bool(self._qreg.pending(self.session_id)),
            pending_plan=any(p.get("tool") == "ExitPlanMode" for p in perms),
            turns_completed=self._turns_completed,
        )

    async def _emit_dashboard_status(self) -> None:
        if self._on_status is None:
            return
        try:
            await self._on_status(self.session_id, derive_status(self._dash_signals()))
        except Exception:
            log.warning("driver.on_status failed session=%s", self.session_id, exc_info=True)
```

4. In `_start_turn`, after the first line set the flag and emit:

```python
        self._turn_active = True
        await self._emit_dashboard_status()
```

5. In `_can_use_tool`, wrap the existing `allowed = await fut`:

```python
        await self._emit_dashboard_status()   # waiting_input / ready_review
        allowed = await fut
        await self._emit_dashboard_status()   # back to in_progress
```

6. In `_handle_ask_user_question`, surround its `await` on the question future the same way
   (emit before awaiting the selection, emit after it resolves).

7. In `_drain`'s `finally`, in the `else` branch (queue empty) replace/augment so it reads:

```python
            else:
                self._turn_active = False
                self._turns_completed += 1
                await self._emit_dashboard_status()
                await self._out.put(ev.Status(state="idle", count=0))
```

8. In `interrupt`, after clearing the queue add:

```python
        self._turn_active = False
        await self._emit_dashboard_status()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_driver_status.py server/tests/test_driver.py -v`
Expected: PASS (new test + existing driver tests still green)

- [ ] **Step 5: Commit**

```bash
git add server/sessions/driver.py server/tests/test_driver_status.py
git commit -m "feat(driver): emit derived dashboard status via on_status callback"
```

---

### Task 5: Orchestrator persists status + auto-name; rename pins the name

**Files:**
- Modify: `server/sessions/orchestrator.py`
- Modify: `server/api/sessions.py` (pass `name_is_auto`; serialize `nameIsAuto`)
- Test: `server/tests/test_orchestrator_status_name.py`

**Interfaces:**
- Consumes: `read_ai_title` (Task 2); driver `on_status` (Task 4); `SessionRecord.name_is_auto` (Task 3).
- Produces: `SessionOrchestrator._persist_status(session_id, status)` (wired as the driver's
  `on_status` in BOTH driver-construction sites). `rename_session` also sets `name_is_auto=False`.
  `create_session(...)` accepts `name_is_auto: bool = True` and stores it on row + record.
  `_record_to_dict` adds `"nameIsAuto": record.name_is_auto`. On status `"done"`, the orchestrator
  attempts `read_ai_title`; if `record.name_is_auto` and a title is found, updates name (record + DB).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_orchestrator_status_name.py
import pytest
from server.sessions.orchestrator import SessionOrchestrator

pytestmark = pytest.mark.asyncio

async def test_persist_status_updates_record(orch_built):
    orch, sid = orch_built
    await orch._persist_status(sid, "waiting_input")
    assert orch.record(sid).status == "waiting_input"

async def test_persist_status_never_overwrites_ended(orch_built):
    orch, sid = orch_built
    await orch._persist_status(sid, "ended")
    await orch._persist_status(sid, "in_progress")
    assert orch.record(sid).status == "ended"

async def test_rename_pins_name_is_auto_false(orch_built):
    orch, sid = orch_built
    await orch.rename_session(sid, "My Name")
    assert orch.record(sid).name == "My Name"
    assert orch.record(sid).name_is_auto is False
```

(Provide an `orch_built` fixture that constructs a `SessionOrchestrator` with one seeded
record — model it on the fixtures in `server/tests/test_orchestrator_questions.py`/
`test_orchestrator_rehydrate.py`. The DB uses `init_db("sqlite+aiosqlite:///:memory:")`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_orchestrator_status_name.py -v`
Expected: FAIL (`AttributeError: 'SessionOrchestrator' object has no attribute '_persist_status'`)

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/orchestrator.py`:

1. Import: `from server.sessions.titles import read_ai_title`.
2. Add the persistence + auto-name method (near `_persist_claude_session_id`):

```python
    async def _persist_status(self, session_id: str, status: str) -> None:
        rec = self._records.get(session_id)
        if rec is not None and rec.status == "ended":
            return
        if rec is not None:
            rec.status = status
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id).values(status=status)
            )
        if status == "done":
            await self._maybe_apply_auto_name(session_id)

    async def _maybe_apply_auto_name(self, session_id: str) -> None:
        rec = self._records.get(session_id)
        if rec is None or not rec.name_is_auto or not rec.claude_session_id:
            return
        projects_root = self._projects_root()  # existing helper used for transcript globbing
        title = read_ai_title(projects_root, rec.claude_session_id)
        if not title or title == rec.name:
            return
        rec.name = title
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id).values(name=title)
            )
```

   If no `_projects_root()` helper exists, derive it from the same config/projects path the
   existing transcript-glob/replay code uses (see `server/sessions/replay.py`); factor that
   path into a small method and reuse it here.

3. In `rename_session`, set the flag on record and DB values:

```python
        if session_id in self._records:
            self._records[session_id].name = name
            self._records[session_id].name_is_auto = False
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id)
                .values(name=name, name_is_auto=False)
            )
```

4. In `create_session`, add a `name_is_auto: bool = True` parameter; set it on the `DbSession(...)`
   row (`name_is_auto=name_is_auto`) and the `SessionRecord(...)` (`name_is_auto=name_is_auto`).
   Pass `on_status=self._persist_status` to the `SessionDriver(...)` constructor here.

5. In `ensure_driver`'s `_build(...)`, also pass `on_status=self._persist_status` to the
   `SessionDriver(...)` constructor.

6. In `rehydrate`, read `row.name_is_auto` into the `SessionRecord(...)` construction.

In `server/api/sessions.py`:

7. In `create_session` route, compute and pass the flag:

```python
    name_is_auto = body.name is None
    name = body.name or "New Session"
    ...
    record = await orch.create_session(..., name_is_auto=name_is_auto)
```

8. In `_record_to_dict`, add `"nameIsAuto": getattr(record, "name_is_auto", True)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_orchestrator_status_name.py server/tests/test_orchestrator_rehydrate.py server/tests/test_api_rehydrate_list.py -v`
Expected: PASS (new tests + existing rehydrate/list tests green)

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/api/sessions.py server/tests/test_orchestrator_status_name.py
git commit -m "feat(orchestrator): persist derived status + apply Claude auto-name"
```

---

### Task 6: Frontend status vocabulary + pill config

**Files:**
- Modify: `web/src/api/types.ts`
- Modify: `web/src/components/UsageBadge.tsx`
- Test: `web/src/components/__tests__/StatusPill.rich.test.tsx`

**Interfaces:**
- Consumes: `nameIsAuto`, rich `status` from the API.
- Produces: `SessionStatus = 'idle' | 'in_progress' | 'waiting_input' | 'ready_review' | 'done' | 'limited' | 'ended'`;
  `Session.nameIsAuto?: boolean`; `SESSION_STATUS_MAP` entries for all new statuses (keeps a
  `working` alias so the live `StatusBar` never regresses).

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/StatusPill.rich.test.tsx
import { render, screen } from '@testing-library/react'
import { StatusPill } from '../UsageBadge'

test.each([
  ['in_progress', 'In progress'],
  ['waiting_input', 'Waiting for input'],
  ['ready_review', 'Ready for review'],
  ['done', 'Done'],
])('renders %s label', (status, label) => {
  render(<StatusPill status={status as any} />)
  expect(screen.getByText(label)).toBeInTheDocument()
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix web test -- StatusPill.rich`
Expected: FAIL (labels not found — statuses fall through to raw string)

- [ ] **Step 3: Write minimal implementation**

In `web/src/api/types.ts` replace the `SessionStatus` line and extend `Session`:

```ts
export type SessionStatus =
  | 'idle' | 'in_progress' | 'waiting_input' | 'ready_review' | 'done'
  | 'limited' | 'ended'

export interface Session {
  id: string
  name: string
  accountName: string
  status: SessionStatus
  createdAt: string
  updatedAt: string
  detached?: boolean
  nameIsAuto?: boolean
}
```

In `web/src/components/UsageBadge.tsx`: add `Eye` and `Loader2` to the lucide import,
then replace `SESSION_STATUS_MAP` with (typed `Record<string, StatusConfig>` to allow the
`working` alias):

```tsx
const SESSION_STATUS_MAP: Record<string, StatusConfig> = {
  idle:         { label: 'Idle',              Icon: Clock,        textClass: 'text-gray-600 dark:text-gray-400' },
  in_progress:  { label: 'In progress',       Icon: Loader2,      textClass: 'text-emerald-700 dark:text-emerald-400' },
  working:      { label: 'In progress',       Icon: Loader2,      textClass: 'text-emerald-700 dark:text-emerald-400' },
  waiting_input:{ label: 'Waiting for input', Icon: CircleAlert,  textClass: 'text-amber-700 dark:text-amber-400' },
  ready_review: { label: 'Ready for review',  Icon: Eye,          textClass: 'text-blue-700 dark:text-blue-400' },
  done:         { label: 'Done',              Icon: CheckCircle2, textClass: 'text-emerald-700 dark:text-emerald-400' },
  limited:      { label: 'Limited',           Icon: AlertTriangle,textClass: 'text-amber-700 dark:text-amber-400' },
  ended:        { label: 'Ended',             Icon: X,            textClass: 'text-gray-600 dark:text-gray-400' },
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm --prefix web test -- StatusPill.rich`
Expected: PASS (4 cases)

- [ ] **Step 5: Commit**

```bash
git add web/src/api/types.ts web/src/components/UsageBadge.tsx web/src/components/__tests__/StatusPill.rich.test.tsx
git commit -m "feat(web): rich session status vocabulary + pill config"
```

---

### Task 7: Dashboard card shows the auto-name affordance + full-suite green

**Files:**
- Modify: `web/src/components/SessionCard.tsx`
- Test: `web/src/components/__tests__/SessionCard.autoname.test.tsx`

**Interfaces:**
- Consumes: `Session.nameIsAuto` (Task 6).
- Produces: when `nameIsAuto`, the name carries a subtle `title="Auto-named by Claude"`
  tooltip + an `aria-label` suffix so users can tell auto from manual names.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/SessionCard.autoname.test.tsx
import { render, screen } from '@testing-library/react'
import { SessionCard } from '../SessionCard'

const base = {
  id: '1', name: 'Fix login bug', accountName: 'work',
  status: 'done' as const, createdAt: '', updatedAt: '',
}

test('auto-named session marks the name as auto', () => {
  render(<SessionCard session={{ ...base, nameIsAuto: true }} account={undefined}
    onOpen={() => {}} onRename={() => {}} onClose={() => {}} />)
  expect(screen.getByText('Fix login bug')).toHaveAttribute('title', 'Auto-named by Claude')
})

test('manually named session has no auto marker', () => {
  render(<SessionCard session={{ ...base, nameIsAuto: false }} account={undefined}
    onOpen={() => {}} onRename={() => {}} onClose={() => {}} />)
  expect(screen.getByText('Fix login bug')).not.toHaveAttribute('title')
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix web test -- SessionCard.autoname`
Expected: FAIL (no `title` attribute on the name span)

- [ ] **Step 3: Write minimal implementation**

In `web/src/components/SessionCard.tsx`, change the name span (currently
`<span className="text-sm font-semibold ...">{session.name}</span>`) to:

```tsx
          <span
            className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate"
            title={session.nameIsAuto ? 'Auto-named by Claude' : undefined}
          >
            {session.name}
          </span>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm --prefix web test -- SessionCard.autoname`
Expected: PASS (2 cases)

- [ ] **Step 5: Run the full suites to confirm no regressions**

Run: `python -m pytest server/tests -q` and `npm --prefix web test`
Expected: all green. If any existing server test asserted the literal status `"working"`/`"idle"`
on a *dashboard record* (vs the live `ev.Status` event), update it to the derived vocabulary; do
NOT change `StatusBar`/live-event tests (they keep `working`/`idle`).

- [ ] **Step 6: Commit**

```bash
git add web/src/components/SessionCard.tsx web/src/components/__tests__/SessionCard.autoname.test.tsx
git commit -m "feat(web): show auto-named affordance on session card"
```

---

## Self-Review

- **Spec coverage:** names via `ai-title` (T2, T5) ✓; `name_is_auto` gate + manual-wins (T3, T5) ✓; first-prompt interim name (unchanged driver `Title`) ✓; status taxonomy + precedence (T1) ✓; derivation from existing signals (T4) ✓; persistence to record/DB + dashboard surfacing via existing poll (T5) ✓; frontend type/map/card (T6, T7) ✓; testing strategy (every task TDD) ✓.
- **Placeholder scan:** none — every code step has full code; the two places that defer to existing patterns (driver constructor kwargs in T4 test, `orch_built` fixture in T5, projects-root helper in T5) name the exact existing files to mirror.
- **Type consistency:** `StatusSignals`/`derive_status` names match across T1→T4; `on_status(session_id, status)` signature matches T4↔T5; `nameIsAuto` (wire) vs `name_is_auto` (python) boundary is explicit in T5/T6; `SessionStatus` union in T6 matches the labels tested in T6.
