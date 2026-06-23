# In-Session Condensing Progress Bar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the whimsical "working" indicator during compaction with a calibrated progress bar that animates against a globally learned expected duration and only completes on the real `compact_boundary`.

**Architecture:** The driver measures each compaction's real duration (PreCompact hook → compact_boundary) and feeds it to a global EWMA persisted on the `AppSettings` singleton; on compaction start it emits a now-wired `Condensing` event carrying the current `expectedMs`. The frontend stores that estimate and renders a determinate `CondensingProgress` bar (linear to 95%, asymptotes on overrun, unmounts on completion) in place of the busy-tier `WorkingIndicator`.

**Tech Stack:** Python (asyncio, SQLAlchemy async, pytest), React+TS (vitest, fake timers), Tailwind.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-23-condensing-progress-bar-design.md`.
- EWMA params (verbatim): `EWMA_ALPHA = 0.3`, `DEFAULT_ESTIMATE_MS = 12000`, `MIN_MS = 3000`, `MAX_MS = 60000`. Seed = first (clamped) sample.
- **Record a duration ONLY on a real `compact_boundary`** (the driver's `ev.Condensing(active=False)` branch) — never on the turn-end backstop (a missing boundary records nothing).
- The bar can only reach 100% when the real boundary arrives; before that it is capped at 95% (linear) then asymptotes `95 + 4*(1 - 1/(1+overrunSeconds))` (< 99). No false precision.
- New DB columns MUST be nullable (`init_db` backfills; no migration).
- The `condensing` status string and StatusBar text are unchanged (shipped in PR #201) — this plan only adds the bar + its data.
- Backend gates: ruff + mypy clean; pytest. Run via the prebuilt env: `PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest …`. Known pre-existing env-only failures: `test_remote_mode_wiring` (network), `test_x11_integration` (xvfb).
- Web gates (in `web/`, node_modules symlinked from the deploy checkout): `node_modules/.bin/tsc -b`, `node_modules/.bin/vitest run`, `node_modules/.bin/eslint src` (0 warnings).

---

## Task 1: Compaction-stats module + persisted EWMA columns

**Files:**
- Modify: `server/db/models.py` (add two nullable columns to `AppSettings`, ~line 132-141)
- Create: `server/sessions/compaction_stats.py`
- Test: `server/tests/test_compaction_stats.py`

**Interfaces:**
- Produces: `fold(prev_ewma: float | None, sample_ms: float) -> float` (pure); `async record_duration(sample_ms: float) -> None`; `async current_estimate() -> float`. Constants `DEFAULT_ESTIMATE_MS`, `MIN_MS`, `MAX_MS`, `EWMA_ALPHA`. DB access uses `server.db.db_session()` (an `@asynccontextmanager` that auto-commits on clean exit — the same path `orchestrator._persist_status` uses; no explicit `commit()` needed).

- [ ] **Step 1: Write the failing test for `fold`.** Create `server/tests/test_compaction_stats.py`:

```python
from server.sessions.compaction_stats import fold, DEFAULT_ESTIMATE_MS, MIN_MS, MAX_MS


def test_fold_seeds_with_first_clamped_sample():
    assert fold(None, 9000) == 9000
    assert fold(None, 100) == MIN_MS        # below floor clamps up
    assert fold(None, 999999) == MAX_MS     # above ceiling clamps down


def test_fold_ewma_weights_recent_at_alpha_0_3():
    # 0.3*sample + 0.7*prev
    assert fold(10000, 20000) == 0.3 * 20000 + 0.7 * 10000


def test_fold_clamps_sample_before_folding():
    # a 5-minute outlier sample is clamped to MAX_MS before folding
    assert fold(10000, 300000) == 0.3 * MAX_MS + 0.7 * 10000
```

- [ ] **Step 2: Run, verify it fails.**

Run: `PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_compaction_stats.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.sessions.compaction_stats'`.

- [ ] **Step 3: Add the DB columns.** In `server/db/models.py`, inside `class AppSettings` (after `default_tool_detail`, ~line 141), add:

```python
    compaction_ewma_ms: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    compaction_samples: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
```

Ensure `Float` is imported from sqlalchemy at the top of the file (add to the existing `from sqlalchemy import (...)` import if missing).

- [ ] **Step 4: Create the stats module.** Create `server/sessions/compaction_stats.py`:

```python
"""Global rolling estimate of how long context auto-compaction takes.

Each compaction yields a real wall-clock duration (PreCompact hook → compact_boundary).
We fold it into a persisted EWMA on the AppSettings singleton and serve the current
estimate so the UI can animate a progress bar against an expected duration. There is no
real compaction-progress signal — this is a learned *estimate*, clamped to a sane range.
"""
from __future__ import annotations

from sqlalchemy import select

from server.db import db_session
from server.db.models import AppSettings

EWMA_ALPHA = 0.3
DEFAULT_ESTIMATE_MS = 12000.0
MIN_MS = 3000.0
MAX_MS = 60000.0


def fold(prev_ewma: float | None, sample_ms: float) -> float:
    """Pure EWMA fold. Clamp the sample to [MIN_MS, MAX_MS]; seed with it when cold."""
    sample = max(MIN_MS, min(MAX_MS, sample_ms))
    if prev_ewma is None:
        return sample
    return EWMA_ALPHA * sample + (1 - EWMA_ALPHA) * prev_ewma


async def _get_or_create(db) -> AppSettings:
    row = (await db.execute(select(AppSettings).where(AppSettings.id == "singleton"))).scalar_one_or_none()
    if row is None:
        row = AppSettings(id="singleton")
        db.add(row)
    return row


async def record_duration(sample_ms: float) -> None:
    """Fold one measured compaction duration into the persisted EWMA.

    ``db_session()`` commits on clean exit, so no explicit commit is needed."""
    async with db_session() as db:
        row = await _get_or_create(db)
        row.compaction_ewma_ms = fold(row.compaction_ewma_ms, sample_ms)
        row.compaction_samples = (row.compaction_samples or 0) + 1


async def current_estimate() -> float:
    """Return the learned estimate (ms), or DEFAULT_ESTIMATE_MS when cold."""
    async with db_session() as db:
        row = (await db.execute(select(AppSettings).where(AppSettings.id == "singleton"))).scalar_one_or_none()
        if row is None or row.compaction_ewma_ms is None:
            return DEFAULT_ESTIMATE_MS
        return row.compaction_ewma_ms
```

- [ ] **Step 5: Add the persistence round-trip test.** Append to `server/tests/test_compaction_stats.py`:

```python
import pytest
from server.db import init_db
from server.sessions import compaction_stats as cs


@pytest.mark.asyncio
async def test_record_and_estimate_roundtrip(tmp_path):
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    assert await cs.current_estimate() == cs.DEFAULT_ESTIMATE_MS  # cold
    await cs.record_duration(9000)
    assert await cs.current_estimate() == 9000                    # seeded
    await cs.record_duration(19000)
    assert await cs.current_estimate() == 0.3 * 19000 + 0.7 * 9000
```

> `db_session()` reads the module-global session factory that `init_db(...)` sets, so calling `init_db` first is all the setup needed. If other DB tests in `server/tests` set up the DB differently (e.g. a fixture), mirror that fixture instead.

- [ ] **Step 6: Run, verify pass.**

Run: `PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_compaction_stats.py -q`
Expected: PASS.

- [ ] **Step 7: ruff + mypy + commit.**

```bash
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m ruff check server/sessions/compaction_stats.py server/db/models.py
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m mypy server/sessions/compaction_stats.py
git add server/sessions/compaction_stats.py server/db/models.py server/tests/test_compaction_stats.py
git commit -m "feat(stats): persisted global EWMA of compaction durations"
```

---

## Task 2: `Condensing` wire form + driver timing/emit/record

**Files:**
- Modify: `server/sessions/events.py` (the `Condensing` dataclass, ~line 124)
- Modify: `server/sessions/driver.py` (constructor; `_on_pre_compact`; the `ev.Condensing` drain branch ~389; the `finally` backstop ~425)
- Test: `server/tests/test_driver_condensing.py` (new) — or extend `server/tests/test_driver_status.py`

**Interfaces:**
- Consumes: `current_estimate`/`record_duration` shapes via two injected async callables.
- Produces: `SessionDriver(..., on_record_condense_ms=None, get_condense_estimate=None)`:
  `on_record_condense_ms: Callable[[float], Awaitable[None]] | None`,
  `get_condense_estimate: Callable[[], Awaitable[float]] | None`.
  `ev.Condensing(active, trigger=None, pre_tokens=None, expected_ms=None)` with
  `to_wire() -> {"type":"condensing","active":bool,"expectedMs":float|None}`.

- [ ] **Step 1: Write the failing driver test.** Create `server/tests/test_driver_condensing.py`. Reuse the `_make_driver`/`_OneTurnClient` style already in `server/tests/test_driver_status.py` (read it first); construct a driver with injected fakes:

```python
import asyncio
import pytest
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions import events as ev
from server.tests.fakes.fake_sdk import FakeSDKClient


def _driver(**kw):
    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return FakeSDKClient([], on_can_use_tool=can_use_tool)
    clock = {"t": 100.0}
    d = SessionDriver("sid", "alpha", "/tmp", "/tmp/cfg", mode="default",
                      sdk_factory=factory, registry=PermissionRegistry(),
                      identity="wstest@example.com", clock=lambda: clock["t"], **kw)
    return d, clock


@pytest.mark.asyncio
async def test_pre_compact_emits_estimate():
    async def est():
        return 8000.0
    d, _ = _driver(get_condense_estimate=est)
    await d._on_pre_compact()
    out = []
    while not d._out.empty():
        out.append(d._out.get_nowait())
    cond = [e for e in out if isinstance(e, ev.Condensing) and e.active]
    assert cond and cond[0].expected_ms == 8000.0


@pytest.mark.asyncio
async def test_boundary_records_clamped_elapsed():
    recorded = []
    async def rec(ms): recorded.append(ms)
    d, clock = _driver(on_record_condense_ms=rec)
    await d._on_pre_compact()           # stamps started_at at t=100
    clock["t"] = 105.0                  # 5s later
    # simulate the drain consuming the translator's boundary event:
    await d._consume_condensing(ev.Condensing(active=False))
    assert recorded == [5000.0]


@pytest.mark.asyncio
async def test_backstop_without_boundary_records_nothing():
    recorded = []
    async def rec(ms): recorded.append(ms)
    d, clock = _driver(on_record_condense_ms=rec)
    await d._on_pre_compact()
    clock["t"] = 110.0
    d._condensing = False
    d._condense_started_at = None       # backstop clears without recording
    assert recorded == []
```

> The test calls a small helper `_consume_condensing` you will add in Step 4 to make the drain branch unit-testable without pumping a full SDK turn. Keep the drain branch a one-line delegate to it.

- [ ] **Step 2: Run, verify fail.**

Run: `PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_driver_condensing.py -q`
Expected: FAIL — `__init__() got an unexpected keyword argument 'get_condense_estimate'` (and `_consume_condensing` missing).

- [ ] **Step 3: Extend the `Condensing` event.** In `server/sessions/events.py`, replace the `Condensing` dataclass (~line 124) with:

```python
@dataclass
class Condensing:
    """Context auto-compaction state. Forwarded to the UI: ``active`` toggles the
    progress bar; ``expected_ms`` (set on the active=True start event) is the learned
    expected duration the bar animates against. ``pre_tokens`` is informational."""
    active: bool
    trigger: str | None = None
    pre_tokens: int | None = None
    expected_ms: float | None = None
    def to_wire(self):
        return {"type": "condensing", "active": self.active, "expectedMs": self.expected_ms}
```

- [ ] **Step 4: Driver changes.** In `server/sessions/driver.py`:

(a) Constructor params — add to the keyword-only list (after `on_status=None`):

```python
                 on_record_condense_ms=None, get_condense_estimate=None,
```

and store them in `__init__` plus the start-stamp:

```python
        self._on_record_condense_ms = on_record_condense_ms
        self._get_condense_estimate = get_condense_estimate
        self._condense_started_at: float | None = None
```

(b) Replace `_on_pre_compact` so it stamps the start, reads the estimate, and emits a wire `Condensing(active=True)`:

```python
    async def _on_pre_compact(self, input=None, tool_use_id=None, ctx=None) -> dict:
        """SDK PreCompact hook: compaction is starting. Stamp the start, advertise the
        learned expected duration to the UI, flip the status. Never blocks compaction."""
        self._condense_started_at = self._clock()
        self._condensing = True
        est: float | None = None
        if self._get_condense_estimate is not None:
            try:
                est = await self._get_condense_estimate()
            except Exception:
                log.warning("driver.get_condense_estimate failed session=%s", self.session_id, exc_info=True)
        await self._out.put(ev.Condensing(active=True, expected_ms=est))
        await self._emit_status()
        return {}
```

(c) Add the testable consume helper and call it from the drain branch. Replace the existing drain `ev.Condensing` branch (~line 389-393) with:

```python
                    if isinstance(e, ev.Condensing):
                        await self._consume_condensing(e)
                        continue
```

and add the method (near `_on_pre_compact`):

```python
    async def _consume_condensing(self, e) -> None:
        """Toggle the flag, record a real boundary's duration, forward to the UI."""
        self._condensing = e.active
        if not e.active and self._condense_started_at is not None:
            ms = (self._clock() - self._condense_started_at) * 1000
            self._condense_started_at = None
            if self._on_record_condense_ms is not None:
                try:
                    await self._on_record_condense_ms(ms)
                except Exception:
                    log.warning("driver.record_condense_ms failed session=%s", self.session_id, exc_info=True)
        await self._emit_status()
        await self._out.put(e)   # forward so the UI clears the bar
```

(d) In the `finally` backstop (~line 425), also clear the start stamp (no record — not a real boundary):

```python
            self._condensing = False
            self._condense_started_at = None
```

- [ ] **Step 5: Run, verify pass.**

Run: `PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_driver_condensing.py server/tests/test_driver_status.py server/tests/test_translator.py -q`
Expected: PASS.

- [ ] **Step 6: ruff + mypy + commit.**

```bash
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m ruff check server/sessions/driver.py server/sessions/events.py
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m mypy server/sessions/driver.py server/sessions/events.py
git add server/sessions/driver.py server/sessions/events.py server/tests/test_driver_condensing.py
git commit -m "feat(driver): time compaction, emit expectedMs, record duration on boundary"
```

---

## Task 3: Orchestrator wiring — serve the estimate, persist durations

**Files:**
- Modify: `server/sessions/orchestrator.py` (add 2 methods; pass them at the 4 `SessionDriver(...)` sites: ~357, ~941, ~1040, ~1552)
- Test: `server/tests/test_orchestrator_condensing_stats.py` (new)

**Interfaces:**
- Consumes: driver params `on_record_condense_ms`, `get_condense_estimate` (Task 2); `compaction_stats.record_duration` / `current_estimate` (Task 1).
- Produces: `SessionOrchestrator._record_condense_ms(ms: float)` and `_condense_estimate() -> float`, wired into every driver it builds.

- [ ] **Step 1: Write the failing test.** Create `server/tests/test_orchestrator_condensing_stats.py`:

```python
import pytest
from server.db import init_db
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions import compaction_stats as cs
from server.tests.fakes.fake_factory import fake_sdk_factory


@pytest.mark.asyncio
async def test_orchestrator_stats_methods_persist_and_serve(tmp_path):
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    assert await orch._condense_estimate() == cs.DEFAULT_ESTIMATE_MS
    await orch._record_condense_ms(7000)
    assert await orch._condense_estimate() == 7000
```

> Match the orchestrator construction style other tests use (grep `SessionOrchestrator(` in `server/tests`). The stats functions reach the DB via `db_session()` exactly like `_persist_status`, so no maker injection is needed.

- [ ] **Step 2: Run, verify fail.**

Run: `PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_orchestrator_condensing_stats.py -q`
Expected: FAIL — `'SessionOrchestrator' object has no attribute '_condense_estimate'`.

- [ ] **Step 3: Add the methods.** In `server/sessions/orchestrator.py`, near `_persist_status` (~line 496), add (using the SAME session-maker access `_persist_status` uses — read it and copy the pattern, e.g. `from server.db import get_session_maker` or an injected `self._session_maker`):

```python
    async def _condense_estimate(self) -> float:
        from server.sessions import compaction_stats
        return await compaction_stats.current_estimate()

    async def _record_condense_ms(self, ms: float) -> None:
        from server.sessions import compaction_stats
        await compaction_stats.record_duration(ms)
```

(`compaction_stats` reaches the DB via `db_session()` internally — same path as `_persist_status`.)

- [ ] **Step 4: Wire all four driver construction sites.** At each `SessionDriver(...)` call (~357, ~941, ~1040, ~1552 — grep `on_status=self._persist_status` to find them), add the two kwargs next to `on_status=self._persist_status`:

```python
            on_status=self._persist_status,
            on_record_condense_ms=self._record_condense_ms,
            get_condense_estimate=self._condense_estimate,
```

- [ ] **Step 5: Run + broad orchestrator suites.**

Run: `PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_orchestrator_condensing_stats.py server/tests/test_orchestrator_lazy_resume.py server/tests/test_orchestrator_questions.py server/tests/test_sync_back.py -q`
Expected: PASS.

- [ ] **Step 6: Full backend suite + ruff + mypy + commit.**

```bash
PY=/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python
env -u CCSWITCH_ENABLE_XVFB -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=src $PY -m pytest server/tests/ -q -p no:cacheprovider   # only test_remote_mode_wiring + test_x11_integration may fail (env-only)
$PY -m ruff check server/ && $PY -m mypy server/sessions
git add server/sessions/orchestrator.py server/tests/test_orchestrator_condensing_stats.py
git commit -m "feat(orchestrator): serve learned estimate + persist compaction durations"
```

---

## Task 4: Frontend — reducer/event for the estimate

**Files:**
- Modify: `web/src/session/events.ts` (add `EvCondensing`, union, `SESSION_EVENT_TYPES`)
- Modify: `web/src/session/reducer.ts` (add `condense` state field + `condensing` case)
- Test: `web/src/session/reducer.condensing.test.ts` (new)

**Interfaces:**
- Produces: reducer state field `condense: { active: boolean; expectedMs: number } | null` (initial `null`). Event `EvCondensing { type:'condensing'; active:boolean; expectedMs?:number|null }`.

- [ ] **Step 1: Write the failing test.** Create `web/src/session/reducer.condensing.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { reduce, initialTranscript } from './reducer'

describe('condensing reducer', () => {
  it('stores expectedMs on active, clears on inactive', () => {
    const s1 = reduce(initialTranscript, { type: 'condensing', active: true, expectedMs: 8000 } as any)
    expect(s1.condense).toEqual({ active: true, expectedMs: 8000 })
    const s2 = reduce(s1, { type: 'condensing', active: false } as any)
    expect(s2.condense).toBeNull()
  })
  it('defaults expectedMs to 12000 when omitted', () => {
    const s = reduce(initialTranscript, { type: 'condensing', active: true } as any)
    expect(s.condense).toEqual({ active: true, expectedMs: 12000 })
  })
})
```

> Confirm the reducer's exported entry point name (`reduce`) by reading `web/src/session/reducer.ts`; if it differs, use the real export. There are two `switch (ev.type)` blocks — add the `condensing` case to the LIVE one (the one used for incoming events, not replay); a `condensing` event never appears in replay.

- [ ] **Step 2: Run, verify fail.**

Run (in `web/`): `node_modules/.bin/vitest run src/session/reducer.condensing.test.ts`
Expected: FAIL — `condense` undefined / event type not handled.

- [ ] **Step 3: Add the event type.** In `web/src/session/events.ts`: add the interface, the union member, and the wire-type set entry:

```ts
export interface EvCondensing    { type: 'condensing';    active: boolean; expectedMs?: number | null }
```
Add `| EvCondensing` to the `SessionEvent` union, and `'condensing',` to the `SESSION_EVENT_TYPES` set.

- [ ] **Step 4: Add reducer state + case.** In `web/src/session/reducer.ts`:
  - In `TranscriptState`, add `condense: { active: boolean; expectedMs: number } | null`.
  - In `initialTranscript`, add `condense: null,`.
  - In the LIVE `switch (ev.type)`, add:

```ts
    case 'condensing': {
      return ev.active
        ? { ...state, condense: { active: true, expectedMs: ev.expectedMs ?? 12000 } }
        : { ...state, condense: null }
    }
```

- [ ] **Step 5: Run, verify pass + tsc.**

Run (in `web/`): `node_modules/.bin/vitest run src/session/reducer.condensing.test.ts && node_modules/.bin/tsc -b`
Expected: PASS, tsc clean.

- [ ] **Step 6: Commit.**

```bash
git add web/src/session/events.ts web/src/session/reducer.ts web/src/session/reducer.condensing.test.ts
git commit -m "feat(web): condensing event carries learned expectedMs into reducer state"
```

---

## Task 5: Frontend — `CondensingProgress` component

**Files:**
- Create: `web/src/components/session/CondensingProgress.tsx`
- Test: `web/src/components/session/__tests__/CondensingProgress.test.tsx`

**Interfaces:**
- Consumes: nothing from earlier tasks except the `expectedMs` number.
- Produces: `CondensingProgress({ expectedMs }: { expectedMs: number })`. Renders `role="progressbar"`.

- [ ] **Step 1: Write the failing test.** Create `web/src/components/session/__tests__/CondensingProgress.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { CondensingProgress } from '../CondensingProgress'

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe('CondensingProgress', () => {
  it('shows the label and a progressbar that stays under 100% before the end', () => {
    render(<CondensingProgress expectedMs={10000} />)
    expect(screen.getByText(/Condensing context…/)).toBeInTheDocument()
    const bar = screen.getByRole('progressbar')
    act(() => { vi.advanceTimersByTime(5000) })   // halfway → ~47% (linear to 95%)
    const mid = Number(bar.getAttribute('aria-valuenow'))
    expect(mid).toBeGreaterThan(0)
    expect(mid).toBeLessThan(95)
    act(() => { vi.advanceTimersByTime(60000) })  // long overrun → asymptote, still < 100
    expect(Number(bar.getAttribute('aria-valuenow'))).toBeLessThan(100)
  })
})
```

- [ ] **Step 2: Run, verify fail.**

Run (in `web/`): `node_modules/.bin/vitest run src/components/session/__tests__/CondensingProgress.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement.** Create `web/src/components/session/CondensingProgress.tsx`:

```tsx
import { useState, useEffect, useRef } from 'react'
import { cn } from '@/lib/utils'
import { formatElapsed } from './elapsed'
import { prefersReducedMotion } from './brailleSpinner'

/**
 * Determinate-looking progress bar for context compaction. There is no real
 * compaction-progress signal, so this animates against a learned *expected*
 * duration: linear to 95% at the expected finish, then asymptotes (never reaching
 * 100%). The component unmounts when condensing ends — that is the only "done".
 */
export function CondensingProgress({ expectedMs }: { expectedMs: number }) {
  const reduced = prefersReducedMotion()
  const startRef = useRef<number>(0)
  const [elapsedMs, setElapsedMs] = useState(0)

  useEffect(() => {
    startRef.current = Date.now()
    const id = setInterval(() => setElapsedMs(Date.now() - startRef.current), reduced ? 1000 : 100)
    return () => clearInterval(id)
  }, [reduced])

  const frac = expectedMs > 0 ? elapsedMs / expectedMs : 1
  let pct: number
  if (frac < 1) {
    pct = Math.min(95, frac * 95)
  } else {
    const overrunSeconds = (elapsedMs - expectedMs) / 1000
    pct = 95 + 4 * (1 - 1 / (1 + overrunSeconds))   // → <99, never 100
  }
  const rounded = Math.round(pct)

  return (
    <div className="flex-shrink-0 flex items-center gap-2 px-4 py-2 text-xs text-gray-400 dark:text-gray-500">
      <span className="font-medium">Condensing context…</span>
      <div
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={rounded}
        aria-valuetext="Condensing context…"
        className="h-1.5 w-32 overflow-hidden rounded bg-gray-200 dark:bg-gray-700"
      >
        <div
          className={cn('h-full rounded bg-emerald-500', !reduced && 'transition-[width] duration-100 ease-linear')}
          style={{ width: `${rounded}%` }}
        />
      </div>
      <span className="tabular-nums opacity-70">{formatElapsed(elapsedMs)}</span>
    </div>
  )
}
```

> Verify `formatElapsed` is exported from `./elapsed` and `prefersReducedMotion` from `./brailleSpinner` (both are used by `WorkingIndicator.tsx` — copy the import paths from there).

- [ ] **Step 4: Run, verify pass + tsc + eslint.**

Run (in `web/`): `node_modules/.bin/vitest run src/components/session/__tests__/CondensingProgress.test.tsx && node_modules/.bin/tsc -b && node_modules/.bin/eslint src/components/session/CondensingProgress.tsx`
Expected: PASS, tsc clean, eslint 0 warnings.

- [ ] **Step 5: Commit.**

```bash
git add web/src/components/session/CondensingProgress.tsx web/src/components/session/__tests__/CondensingProgress.test.tsx
git commit -m "feat(web): CondensingProgress estimate-driven bar"
```

---

## Task 6: Frontend — render the bar in SessionBody

**Files:**
- Modify: `web/src/components/session/SessionBody.tsx` (the busy-tier indicator, ~line 185-188)
- Test: `web/src/components/session/__tests__/SessionBody.condensing.test.tsx` (new) — or extend an existing SessionBody test if one renders the busy indicator

**Interfaces:**
- Consumes: `CondensingProgress` (Task 5); reducer `condense` state (Task 4); the `condensing` status string.

- [ ] **Step 1: Write the failing test.** Read the existing SessionBody tests first to copy the runtime/`rt` mock shape. Create `web/src/components/session/__tests__/SessionBody.condensing.test.tsx` asserting: when the runtime state has `status.state === 'condensing'` and `condense: { active:true, expectedMs: 9000 }`, SessionBody renders a `progressbar` and the "Condensing context…" label, and does NOT render the whimsical working spinner (`working-spinner` testid absent); and that a normal `in_progress` status still renders the `working-spinner`.

```tsx
// Skeleton — adapt the rt/runtime mock to match the existing SessionBody test helpers.
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SessionBody } from '../SessionBody'
// ...build a runtime `rt` with state.status.state='condensing', state.condense={active:true,expectedMs:9000},
//    state.turns=[at least one], etc., mirroring the existing SessionBody test fixtures...

it('renders the condensing bar instead of the working spinner', () => {
  render(<SessionBody rt={condensingRt} /* + other required props */ />)
  expect(screen.getByRole('progressbar')).toBeInTheDocument()
  expect(screen.getByText(/Condensing context…/)).toBeInTheDocument()
  expect(screen.queryByTestId('working-spinner')).toBeNull()
})
```

> If SessionBody is impractical to render standalone (many props), prefer extending the nearest existing SessionBody/`SessionView` test that already mounts it, adding the condensing case there. Do NOT weaken existing assertions.

- [ ] **Step 2: Run, verify fail.**

Run (in `web/`): `node_modules/.bin/vitest run src/components/session/__tests__/SessionBody.condensing.test.tsx`
Expected: FAIL — progressbar absent (still renders WorkingIndicator).

- [ ] **Step 3: Implement the swap.** In `web/src/components/session/SessionBody.tsx`, add the import:

```tsx
import { CondensingProgress } from './CondensingProgress'
```

and replace the busy-tier block (~line 185-188) so condensing renders the bar:

```tsx
            {statusTier(rt.state.status.state) === 'busy' && (
              rt.state.status.state === 'condensing' ? (
                <CondensingProgress expectedMs={rt.state.condense?.expectedMs ?? 12000} />
              ) : (
                <div
                  aria-hidden="true"
                  className={cn(
                    'flex-shrink-0 flex items-center px-4 py-2',
                    'text-xs text-gray-400 dark:text-gray-500',
                  )}
                >
                  <WorkingIndicator count={rt.state.status.count} subAgentCount={runningSubAgents} />
                </div>
              )
            )}
```

- [ ] **Step 4: Run, verify pass + full web gates.**

Run (in `web/`): `node_modules/.bin/vitest run src/components/session && node_modules/.bin/tsc -b && node_modules/.bin/eslint src`
Expected: PASS, tsc clean, eslint 0 warnings.

- [ ] **Step 5: Commit.**

```bash
git add web/src/components/session/SessionBody.tsx web/src/components/session/__tests__/SessionBody.condensing.test.tsx
git commit -m "feat(web): show CondensingProgress in the session during compaction"
```

---

## Final verification (before PR)

- [ ] Backend: `env -u CCSWITCH_ENABLE_XVFB -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=src <deploy>/.venv/bin/python -m pytest server/tests/ -q -p no:cacheprovider` (only `test_remote_mode_wiring` + `test_x11_integration` may fail — env-only). Plus `ruff check server/` and `mypy server/sessions`.
- [ ] Web (in `web/`): `node_modules/.bin/tsc -b && node_modules/.bin/vitest run && node_modules/.bin/eslint src`.
- [ ] Manual: during a real compaction, the prominent in-session indicator shows "Condensing context…" + a bar that fills toward ~95%, holds if it overruns, and disappears when compaction completes; the StatusBar text + `ctx %` readout are unchanged.

## Self-review notes (author)

- **Spec coverage:** measurement → Task 2; EWMA persistence on AppSettings → Task 1; estimate delivery (wire event) → Tasks 2+3; bar component → Task 5; SessionBody swap → Task 6; reducer field → Task 4. Record-only-on-boundary + missing-boundary-records-nothing → Task 2 (`_consume_condensing` vs the `finally` backstop). Honesty cap/asymptote → Task 5.
- **Type consistency:** `expected_ms`/`expectedMs`, `condense: {active, expectedMs}`, `on_record_condense_ms`/`get_condense_estimate`, `fold`/`record_duration`/`current_estimate` are used identically across producing/consuming tasks.
- **Resolved during planning:** DB access uses `db_session()` (verified — auto-commits, same path as `_persist_status`); no session-maker accessor needed.
- **Flagged unknowns (each has a "read the real code first" note):** the reducer entry-point export name + which `switch` is the live one (Task 4), the SessionBody test fixture shape (Task 6), and the 4 exact driver construction sites (Task 3, grep `on_status=self._persist_status`).
