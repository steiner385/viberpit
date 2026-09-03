# Session Lifecycle Continuity & Self-Healing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close audit gaps G1–G10 so every session's presented state is truthful within a bounded staleness, every failure carries a durable typed reason, every transition is explained in place, and the system self-heals from daemon restarts/crashes, dead fleet hosts, stuck spawns, and DB write failures.

**Architecture:** A fenced per-host heartbeat table (pattern copied from `server/sessions/primary_lease.py`) from which per-session liveness is *derived*, never stored. The primary daemon fences and adopts sessions off dead hosts with best-effort transcript resume. Two new durable tables (`session_events`, `session_holds`) carry lifecycle provenance and pending-hold content across restarts. The frontend composes liveness into its single status map as a demotion step.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x async (SQLite + Postgres, portable SQL only) · React + TS + Tailwind · pytest + vitest.

**Spec:** `specs/002-session-continuity/spec.md` (read it first — every task cites FR numbers from it).

## Global Constraints

- Branch off `origin/master` after `git fetch` — NEVER the local `master` ref (stale in worktrees). One PR per phase; PRs open non-draft with `gh pr merge <#> --squash --auto` armed.
- The lint-and-test gate is FOUR steps, all required before push: `ruff check src/ server/` · `mypy server/ --ignore-missing-imports` · `pytest tests/` · `pytest server/tests/`.
- Frontend gate (run in `web/`): `vitest run` (FULL suite — changed-file-only runs miss manual-mock breakage, see PR #938), `npm run build` (the `tsc -b` project build is the enforcing typecheck — a bare `tsc --noEmit` checks zero files), `eslint`.
- Local server tests: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1` and unset ALL `CCSWITCH_SESSIOND_*` vars. Worktrees use the main checkout's venv: `/home/tony/GitHub/ccswitch/.venv/bin/python -m pytest`.
- Portable SQL only: single conditional UPDATE/INSERT with rowcount checks (no dialect locking); `//` for integer division; naive `datetime.utcnow()` timestamps to match `primary_lease.py` and the reconcile machinery.
- Every new table: nullable-or-defaulted columns only (reconcile auto-applies on both dialects); add entries to `_REQUIRED_EXTRAS` in `server/tests/test_postgres_compat.py`; add a chained alembic revision under `server/db/migrations/versions/` (`down_revision` = current newest); run the Postgres suite locally with `CCSWITCH_TEST_PG_URL` set (CI skips it — a missed entry is green locally, red never).
- Before adding any constant/helper, grep `server/` + `src/` + `web/src/` for an existing source of truth (the `AuditEvent` table was checked for `session_events` reuse and rejected: it is a security log keyed by NOT NULL `identity` — different concept).
- Env knob defaults, verbatim from spec: `CCSWITCH_HOST_HEARTBEAT_INTERVAL_S=10`, `CCSWITCH_HOST_DEAD_AFTER_S=300`, `CCSWITCH_SPAWN_DEADLINE_S=120`, dirty-flush interval 10 s.
- Rollout safety rule (NFR-3): a MISSING heartbeat row is never evidence of death — `fresh` with `last_confirmed_alive_at=None`, and adoption never triggers on a missing row, only on a stale existing one.

---

## Phase 1 — Liveness layer (FR-101…FR-104, G1/G2/G8 presentation)

### Task 1: `HostHeartbeat` model + schema conformance

**Files:**
- Modify: `server/db/models.py` (add class after `DaemonLease`)
- Modify: `server/tests/test_postgres_compat.py` (`_REQUIRED_EXTRAS`)
- Create: `server/db/migrations/versions/<next>_host_heartbeats.py`
- Test: `server/tests/test_host_heartbeat_model.py`

**Interfaces:**
- Produces: `server.db.models.HostHeartbeat` with columns `host: str (PK)`, `beat_at: datetime (NOT NULL, default utcnow)`, `fence: int (NOT NULL, default 1)`, `epoch: str | None`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_host_heartbeat_model.py
import pytest
from server.db import db_session, init_db
from server.db.models import HostHeartbeat


@pytest.mark.asyncio
async def test_host_heartbeat_roundtrip(tmp_db):  # use the suite's existing tmp-DB fixture
    async with db_session() as db:
        db.add(HostHeartbeat(host="rocky", epoch="1a2b-42"))
    async with db_session() as db:
        row = await db.get(HostHeartbeat, "rocky")
        assert row is not None and row.fence == 1 and row.beat_at is not None
```

(Match the existing DB-fixture name in `server/tests/conftest.py` — grep `init_db` there; several model tests show the pattern.)

- [ ] **Step 2: Run to verify it fails** — `pytest server/tests/test_host_heartbeat_model.py -v` → FAIL (ImportError: HostHeartbeat).
- [ ] **Step 3: Implement the model**

```python
class HostHeartbeat(Base):
    """Fenced per-host daemon heartbeat (spec 002 FR-101).

    Written only by the named host (fenced renew in host_heartbeat.beat) or by the
    primary bumping the fence to declare the host dead (host_heartbeat.declare_dead).
    Liveness is DERIVED from beat_at — never stored (FR-102)."""

    __tablename__ = "host_heartbeats"

    host: Mapped[str] = mapped_column(String, primary_key=True)
    beat_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    fence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    epoch: Mapped[str | None] = mapped_column(String, nullable=True)
```

- [ ] **Step 4: Schema conformance** — add `host_heartbeats` entries to `_REQUIRED_EXTRAS` in `server/tests/test_postgres_compat.py` following that file's existing entry format for NOT NULL columns (`beat_at`, `fence`); write the alembic revision (create_table mirroring the model; `down_revision` = current newest revision id — `ls server/db/migrations/versions/` and take the head). Defaults exist for all NOT NULL columns, so no `RECONCILE_EXEMPT` entry.
- [ ] **Step 5: Run test → PASS; run all four gate steps; commit** — `git commit -m "feat(db): host_heartbeats table for fenced liveness (spec 002 FR-101)"`

### Task 2: Fenced heartbeat operations

**Files:**
- Create: `server/sessions/host_heartbeat.py`
- Test: `server/tests/test_host_heartbeat.py`

**Interfaces:**
- Consumes: `HostHeartbeat` model (Task 1).
- Produces:
  - `@dataclass(frozen=True) HeartbeatState(host: str, beat_at: datetime, fence: int, epoch: str | None, age_s: float)`
  - `async def register(host: str, *, epoch: str | None = None) -> int` — insert-or-read; returns current fence (1 on first insert; the EXISTING fence if the row survives from before — a fenced-while-down daemon simply adopts the new fence).
  - `async def beat(host: str, fence: int) -> bool` — conditional `UPDATE … WHERE host=? AND fence=?` setting `beat_at=utcnow()`; `False` = fenced over (FR-202 trigger).
  - `async def declare_dead(host: str, *, dead_after_s: float) -> int | None` — conditional `UPDATE … WHERE host=? AND beat_at <= cutoff` setting `fence=fence+1`; returns the new fence or `None` (row missing, or beat too fresh — exactly-one-winner under races, same rowcount discipline as `primary_lease.acquire`).
  - `async def snapshot() -> dict[str, HeartbeatState]`

- [ ] **Step 1: Write the failing tests** — four tests: `register` twice keeps fence; `beat` with stale fence returns False; two `declare_dead` calls on an aged row — first returns fence 2, second returns None (already bumped ⇒ beat_at unchanged? NO: bump does not touch `beat_at`, so guard with fence: after a successful bump the caller owns adoption; a second caller's `declare_dead` must therefore ALSO check `fence == observed_fence`):

```python
# server/tests/test_host_heartbeat.py
import pytest
from datetime import datetime, timedelta
from server.sessions import host_heartbeat as hb
from server.db import db_session
from server.db.models import HostHeartbeat
from sqlalchemy import update


async def _age(host: str, seconds: float) -> None:
    async with db_session() as db:
        await db.execute(update(HostHeartbeat).where(HostHeartbeat.host == host)
                         .values(beat_at=datetime.utcnow() - timedelta(seconds=seconds)))


@pytest.mark.asyncio
async def test_register_is_idempotent_on_fence(tmp_db):
    assert await hb.register("w1") == 1
    assert await hb.register("w1") == 1


@pytest.mark.asyncio
async def test_beat_fails_after_fence_bump(tmp_db):
    fence = await hb.register("w1")
    await _age("w1", 999)
    assert await hb.declare_dead("w1", dead_after_s=300, observed_fence=fence) == fence + 1
    assert await hb.beat("w1", fence) is False          # old holder is fenced out
    assert await hb.beat("w1", fence + 1) is True       # re-registered holder beats fine


@pytest.mark.asyncio
async def test_declare_dead_single_winner(tmp_db):
    fence = await hb.register("w1")
    await _age("w1", 999)
    assert await hb.declare_dead("w1", dead_after_s=300, observed_fence=fence) == fence + 1
    assert await hb.declare_dead("w1", dead_after_s=300, observed_fence=fence) is None


@pytest.mark.asyncio
async def test_declare_dead_refuses_fresh_row(tmp_db):
    fence = await hb.register("w1")
    assert await hb.declare_dead("w1", dead_after_s=300, observed_fence=fence) is None
```

Note the signature refinement the race test forces: `declare_dead(host, *, dead_after_s, observed_fence)` — the WHERE clause is `host=? AND fence=observed_fence AND beat_at<=cutoff`, making the bump a compare-and-swap on the fence itself. Update the Interfaces block accordingly; Task 12 consumes this exact signature.

- [ ] **Step 2: Run → FAIL** (module missing).
- [ ] **Step 3: Implement** — mirror `primary_lease.py` structure exactly (module docstring explaining the fence, single conditional statements, `result.rowcount == 1` checks with the same `# type: ignore[attr-defined]` comment, `log.info` on transitions). `register`: try PK-guarded INSERT (fence=1, beat_at=utcnow); on IntegrityError rollback and SELECT the existing fence.
- [ ] **Step 4: Run → PASS; four gate steps.**
- [ ] **Step 5: Commit** — `feat(sessions): fenced host heartbeat ops (spec 002 FR-101/FR-201)`

### Task 3: Pure liveness derivation

**Files:**
- Create: `server/sessions/liveness.py`
- Test: `server/tests/test_liveness.py`

**Interfaces:**
- Produces:
  - `LivenessState = Literal["fresh", "stale", "unreachable", "dead"]`
  - `@dataclass(frozen=True) Liveness(state: LivenessState, last_confirmed_alive_at: datetime | None)`
  - `def derive_liveness(beat_at: datetime | None, now: datetime, *, interval_s: float = 10.0, dead_after_s: float = 300.0, declared_dead: bool = False) -> Liveness`

- [ ] **Step 1: Write the failing table-driven test**

```python
# server/tests/test_liveness.py
from datetime import datetime, timedelta
import pytest
from server.sessions.liveness import derive_liveness

NOW = datetime(2026, 9, 2, 12, 0, 0)


@pytest.mark.parametrize("age_s,declared,expected", [
    (0, False, "fresh"),
    (19.9, False, "fresh"),          # ≤ 2× interval
    (20.1, False, "stale"),
    (299.9, False, "stale"),
    (300.1, False, "unreachable"),
    (300.1, True, "dead"),
    (-30, False, "fresh"),           # writer clock ahead of reader: never punish skew
])
def test_thresholds(age_s, declared, expected):
    beat = NOW - timedelta(seconds=age_s)
    lv = derive_liveness(beat, NOW, declared_dead=declared)
    assert lv.state == expected
    assert lv.last_confirmed_alive_at == beat


def test_missing_row_is_fresh_unknown():
    # NFR-3: absence of evidence is not evidence of death (rollout safety).
    lv = derive_liveness(None, NOW)
    assert lv.state == "fresh" and lv.last_confirmed_alive_at is None
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — a pure function, ~15 lines, no I/O, no imports beyond dataclasses/datetime/typing.
- [ ] **Step 4: Run → PASS; gate; commit** — `feat(sessions): pure liveness derivation (spec 002 FR-102)`

### Task 4: Daemon heartbeat loop

**Files:**
- Modify: `server/ipc/serve.py` (lifespan — near where the drain handler and rehydrate are wired; grep `install_drain_on_sigterm`)
- Modify: `server/sessions/orchestrator.py` (loop task alongside `_stall_loop`; grep `_stall_loop` for the spawn/cancel pattern)
- Test: `server/tests/test_heartbeat_loop.py`

**Interfaces:**
- Consumes: `host_heartbeat.register/beat` (Task 2).
- Produces: orchestrator attribute `self._hb_fence: int | None`; method `async def _heartbeat_loop(self) -> None`; hook `async def _on_fenced(self) -> None` (Phase 1 body: `log.critical` only — full stand-down lands in Task 16, Phase 4, BEFORE anything bumps fences in production).

- [ ] **Step 1: Failing test** — with a fake clock/loop-once helper (follow the existing `_check_stalls` test style): calling the loop body once stamps a row for this daemon's host; monkeypatching `beat` to return False invokes `_on_fenced` exactly once.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — on daemon start: `self._hb_fence = await host_heartbeat.register(self._host, epoch=<daemon epoch string — grep daemon_health for how epoch is built>)`. Loop: every `CCSWITCH_HOST_HEARTBEAT_INTERVAL_S` (env-read once, default 10), `ok = await host_heartbeat.beat(self._host, self._hb_fence)`; on `False` → `await self._on_fenced()` and stop beating until re-registered. Determine `self._host` the same way sessions are host-stamped (grep how `session.host` is set in `fleet_orchestrator.py`); in-process/single-host mode uses the same hostname value. Exceptions in the loop are caught-and-logged (DB down must not kill the daemon — NFR-5).
- [ ] **Step 4: Run → PASS; gate; commit** — `feat(daemon): per-host fenced heartbeat loop (spec 002 FR-101)`

### Task 5: Liveness on the session API

**Files:**
- Modify: `server/api/sessions.py` — the function that builds the per-session JSON payload (grep `interruptedByRestart`; liveness rides the same serializer, one source of truth)
- Test: `server/tests/test_sessions_liveness_api.py`

**Interfaces:**
- Consumes: `host_heartbeat.snapshot()` (Task 2), `derive_liveness` (Task 3).
- Produces: payload fields `"liveness": "fresh"|"stale"|"unreachable"|"dead"` and `"lastConfirmedAliveAt": <iso8601 | null>` on session list + detail responses.

- [ ] **Step 1: Failing test** — API test (existing auth-pinned client fixture pattern): create a session row stamped `host="w1"`; seed a `HostHeartbeat` row aged 999 s; GET the list; assert `liveness == "unreachable"`. Second test: session with `host=None` (in-process) → `"fresh"` with null `lastConfirmedAliveAt`. Third: no heartbeat row at all → `"fresh"` (rollout rule).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — fetch `snapshot()` ONCE per request (not per session), cache on the request scope; `host=None` → fresh; look up `session.host` in the snapshot and call `derive_liveness`. Thresholds from the two env knobs.
- [ ] **Step 4: Run → PASS; gate; commit** — `feat(api): liveness + lastConfirmedAliveAt on session payloads (spec 002 FR-103)`

### Task 6: Frontend truth — liveness demotion + Unknown fallback

**Files:**
- Modify: `web/src/api/types.ts` (Session gains `liveness?: 'fresh'|'stale'|'unreachable'|'dead'`, `lastConfirmedAliveAt?: string | null`)
- Modify: `web/src/session/statusTier.ts`
- Modify: callers that pass opts to `statusPresentation()` (grep `statusPresentation(` and `interruptedByRestart` across `web/src/` — SessionCard, triageTier, anywhere building opts) to thread `liveness`
- Modify: `web/src/components/SessionCard.tsx` — "last seen X ago" subline when demoted (relative time from `lastConfirmedAliveAt`, same relative-time util the card already uses — grep for the existing formatter before adding one)
- Test: `web/src/session/statusTier.test.ts` (create if absent, colocated like `reducer.stale.test.ts`)

**Interfaces:**
- Consumes: API fields from Task 5.
- Produces: `StatusTier` union gains `'unreachable'`; `StatusPresentationOpts` gains `liveness?: string`; exported `UNREACHABLE: StatusPresentation`; FALLBACK becomes the neutral Unknown pill.

- [ ] **Step 1: Failing tests**

```ts
// web/src/session/statusTier.test.ts
import { describe, expect, it } from 'vitest'
import { statusPresentation } from './statusTier'

describe('liveness demotion (spec 002 FR-104)', () => {
  it.each(['in_progress', 'waiting_input', 'idle', 'done', 'queued'])(
    'demotes non-terminal %s when liveness is not fresh', (status) => {
      expect(statusPresentation(status, { liveness: 'unreachable' }).tier).toBe('unreachable')
    })
  it.each(['complete', 'ended'])('never demotes terminal %s', (status) => {
    expect(statusPresentation(status, { liveness: 'unreachable' }).tier).not.toBe('unreachable')
  })
  it('fresh (and absent) liveness changes nothing', () => {
    expect(statusPresentation('in_progress', { liveness: 'fresh' }).tier).toBe('busy')
    expect(statusPresentation('in_progress').tier).toBe('busy')
  })
})

describe('unknown status (spec 002 FR-402)', () => {
  it('renders neutral Unknown, never Ready', () => {
    const p = statusPresentation('some_future_status')
    expect(p.label).toBe('Unknown')
    expect(p.tier).toBe('waiting')
  })
})
```

- [ ] **Step 2: Run → FAIL** (`vitest run` in `web/`).
- [ ] **Step 3: Implement** — in `statusTier.ts`:
  - Add `'unreachable'` to `StatusTier`; extend the three exhaustive records (`TIER_ACCENT`: slate rail, amber dot, `pulse: false`; `TIER_RANK`: insert at rank 1 shifting error→2 etc. so unreachable sorts just under needs-you; `TIER_LABEL`: `'Unreachable'`). The exhaustive `Record` types make `npm run build` fail until all three are extended — that is the safety net working.
  - `const TERMINAL = new Set(['complete', 'ended', 'archived'])`; at the top of `statusPresentation`, after the interrupted check: `if (opts?.liveness && opts.liveness !== 'fresh' && !TERMINAL.has(state)) return UNREACHABLE` where `UNREACHABLE = { tier: 'unreachable', label: 'Unreachable', Icon: WifiOff, textClass: 'text-slate-500 dark:text-slate-400' }` (import `WifiOff` from lucide-react).
  - Replace `FALLBACK` with `{ tier: 'waiting', label: 'Unknown', Icon: HelpCircle, textClass: CALM }`; keep the `seenUnknownStatuses` warn but rewrite its comment — it is now permanent instrumentation, not a temporary diagnostic. Update the stale comment block above it.
  - Thread `liveness` through every `statusPresentation(...)`/`sessionTriage(...)` call site found by grep; the demotion must also apply in `triageTier.ts` ORDER: liveness demotion beats PR-checks lift (an unreachable session cannot act on failing checks either).
- [ ] **Step 4: FULL `vitest run` + `npm run build` + eslint → PASS.**
- [ ] **Step 5: Commit** — `feat(web): liveness demotion + neutral Unknown fallback (spec 002 FR-104/FR-402)`

**Phase 1 lands as one PR** (`Fixes` nothing yet — reference the epic issue). Verify on the real dashboard: stop the dev daemon; within ~30 s every non-terminal card must read "Unreachable".

---

## Phase 2 — Durable provenance & holds (FR-301…FR-307, FR-403…FR-407)

### Task 7: `session_events` + `session_holds` tables and recorder

**Files:**
- Modify: `server/db/models.py` (two classes)
- Create: `server/sessions/events.py`
- Modify: `server/tests/test_postgres_compat.py`; create alembic revision
- Test: `server/tests/test_session_events.py`

**Interfaces:**
- Produces:
  - `SessionEvent(id: int PK autoincrement, session_id: str indexed, at: datetime, kind: str, payload: dict)` — house pattern: plain indexed string, NO FK constraint (see Attachment.session_id; avoids the #895 FK-violation class).
  - `SessionHold(id: str PK uuid, session_id: str indexed, kind: str, content: dict, created_at: datetime, resolved_at: datetime | None)`
  - `server.sessions.events`: `async def record(session_id: str, kind: str, payload: dict | None = None) -> None` (swallows-and-logs DB errors — provenance must never break the turn path, NFR-5); `async def for_session(session_id: str, limit: int = 100) -> list[SessionEvent]` (newest first). Event kinds are module constants: `SPAWN_FAILED, INTERRUPTED, HOLD_LOST, MOVED_HOST, AUTO_RESUMED, ROTATED_ACCOUNT, AUTO_ARCHIVED, DELETE_RETRY` — the single vocabulary, imported everywhere (FR-301).

- [ ] **Step 1: Failing test** — record two events, read back newest-first; `record` with a broken DB (monkeypatch `db_session` to raise) does not raise.
- [ ] **Step 2–4: Implement, conformance lists + revision, gate, commit** — `feat(db): session_events + session_holds provenance tables (spec 002 FR-301/FR-305)`

### Task 8: Typed spawn failures for queued promotions (G6)

**Files:**
- Modify: `server/sessions/orchestrator.py:1664-1778` (`_maybe_start_queued` exception handler — currently marks `ended` silently)
- Modify: `server/api/sessions.py` payload builder (expose `"lastSpawnFailure": {"reason": str, "at": iso} | null`)
- Test: `server/tests/test_spawn_failure_events.py`

**Interfaces:**
- Consumes: `events.record` / `events.SPAWN_FAILED` (Task 7).
- Produces: every `queued → ended` transition is preceded by a `SPAWN_FAILED` event whose payload is `{"reason": <typed>, "transient": bool, "attempt": int}`; queue params JSON gains `"attempts": int`; transient reasons re-enqueue exactly once.

- [ ] **Step 1: Failing tests** — using the existing queued-session test fixtures (grep `test_` files referencing `_maybe_start_queued` / `session_queue`): (a) promotion failing with the clone-error class records `SPAWN_FAILED(transient=True, attempt=1)` and the session is re-queued, not ended; (b) second failure ends it with `attempt=2`; (c) a branch-switch failure ends immediately with `transient=False`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — classify at the existing catch site: transient = clone/network exception classes already distinguished there (`gitrepos.CloneError`, connection errors); permanent = `BranchSwitchError`, validation errors. Read/increment `attempts` in the queue-entry params (entry survives until spawn commits — the #939 invariant; do not change that ordering). `lastSpawnFailure` in the API: newest `SPAWN_FAILED` event, fetched only for sessions whose status is `ended` or `queued` (one grouped query, not N+1).
- [ ] **Step 4–5: Gate; commit** — `feat(sessions): typed spawn-failure provenance + transient retry (spec 002 FR-302)`

### Task 9: Spawn deadline + provider preflight (G5, G4)

**Files:**
- Modify: `server/sessions/orchestrator.py:1906-2109` (`_spawn_session`)
- Create: `server/sessions/preflight.py`
- Test: `server/tests/test_spawn_deadline.py`, `server/tests/test_preflight.py`

**Interfaces:**
- Consumes: `events.record` (Task 7).
- Produces:
  - `class SpawnTimeoutError(Exception)` in orchestrator module.
  - `preflight.check(account: str, cfg_dir: Path, provider: str) -> str | None` — sync, filesystem-only: for the Anthropic provider, missing `cfg_dir / ".credentials.json"` → `"missing_credentials"`; a parseable `expiresAt`/`expires_at` in the past → `"credentials_expired"`; otherwise `None`. Copilot returns `None` here (its existing `_copilot.ensure()` gate in `_spawn_session` IS its preflight — do not duplicate it, FR-304).
- [ ] **Step 1: Failing tests** — deadline: monkeypatch the driver factory with a `start()` that sleeps forever; `_spawn_session` must raise `SpawnTimeoutError` within the (test-shrunk, env-set to 0.1) deadline, tear down the ghost record, and a `SPAWN_FAILED(reason="spawn_timeout")` event must exist. Preflight: tmp cfg dir cases for both reasons + the ok case.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — `await asyncio.wait_for(driver.start(), timeout=SPAWN_DEADLINE_S)`; on `TimeoutError`: best-effort `await driver.aclose()` inside `contextlib.suppress`, pop the record (the existing ghost-record cleanup at orchestrator.py:2033-2039), record the event, raise `SpawnTimeoutError`. Preflight runs just before driver construction; a non-None reason records `SPAWN_FAILED` and raises a typed error that `_maybe_start_queued`'s Task 8 classifier treats as transient (rotation = the retry). Retry-on-rotated-account: in the Task 8 retry path, when the failure reason is `spawn_timeout`/preflight, exclude the failed account from `select_account` for that attempt (thread an `exclude` through the queue params).
- [ ] **Step 4–5: Gate; commit** — `feat(sessions): spawn deadline + provider preflight (spec 002 FR-303/FR-304)`

### Task 10: Durable holds — persist, resolve, and `hold_lost` on rehydrate (G7)

**Files:**
- Modify: `server/sessions/driver.py` (expose the pending hold: add `def pending_hold(self) -> tuple[str, dict] | None` reading the same internal pending-permission/question/plan state that `derive_status`'s signals come from — grep `pending_permission` in driver.py)
- Modify: `server/sessions/orchestrator.py` `_persist_status` (orchestrator.py:2211-2320) and rehydrate (orchestrator.py:5953-6078)
- Test: `server/tests/test_session_holds.py`

**Interfaces:**
- Consumes: `SessionHold` model, `events.record`/`HOLD_LOST` (Task 7).
- Produces:
  - On a status transition INTO `waiting_input`/`waiting_question`/`ready_review`: insert a `SessionHold(kind, content)` where content is the driver's pending payload (question text + options / permission request / plan summary).
  - On transition OUT: stamp `resolved_at` on the newest unresolved hold.
  - Rehydrate: for each session being normalized whose newest hold is unresolved AND whose last turn outcome is not settled, record `HOLD_LOST` with the full content and stamp `resolved_at` (the hold is consumed by the event); the session record carries `lost_hold: dict | None` in-memory for the API.
  - API payload field `"lostHold": {"kind": str, "content": dict} | null`.
- [ ] **Step 1: Failing tests** — drive a fake driver through waiting_question and back (existing fake-driver harness — the pattern in `test_resume_interrupted.py`): hold row created then resolved. Simulated restart with an unresolved hold → `HOLD_LOST` event carries the content and the payload surfaces `lostHold`. Edge case from spec: hold created but turn outcome settled before death → NO `hold_lost`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — all writes via `events.record`-style swallow-and-log; hold insert/resolve rides the existing `_persist_status` transaction boundary (same status source of truth, no new status observer).
- [ ] **Step 4–5: Gate; commit** — `feat(sessions): durable holds survive restarts as hold_lost provenance (spec 002 FR-305)`

### Task 11: Dirty-status flusher + frontend trust order (G3)

**Files:**
- Modify: `server/sessions/orchestrator.py` `_persist_status` + a new `_dirty_flush_loop` (pattern: `_stall_loop`)
- Modify: `web/src/session/reconcile.ts` (encode the trust rule where `shouldApplyRev` lives)
- Test: `server/tests/test_dirty_status_flush.py`, `web/src/session/reconcile.test.ts` (extend existing)

**Interfaces:**
- Produces: `self._dirty_status: set[str]`; `_persist_status`'s DB-failure path adds the session id; the 10 s loop re-persists current in-memory status for each dirty id and clears on success. Frontend: exported `trustOrder` comment + logic — an open, fresh WS patch for a session wins over a poll; a poll wins when the socket for that session is closed or stale (the existing `revAt` mechanism already carries the comparison — this task makes the socket-open case explicit rather than timestamp-lucky).
- [ ] **Steps: failing test (fail one persist via monkeypatched `db_session`, run loop body once, assert healed row + cleared set) → implement → gate → commit** — `feat(sessions): bound DB-shadow divergence with dirty-status flusher (spec 002 FR-307/FR-403)`

### Task 12: Delete that tells the truth (G9)

**Files:**
- Modify: `server/sessions/orchestrator.py:9842-9906` (`permanent_delete` — already returns False when kept) + expose `def delete_state(self, session_id: str) -> str | None` reading `_archive_purge_strikes` / `_archive_purge_poisoned` (`"pending"` / `"stuck"` / None)
- Modify: `server/api/sessions.py:1205` (`permanent_delete_session`) — False + row still present → HTTP 202 `{"state": "pending"}`; poisoned → `{"state": "stuck"}`
- Modify: `web/src/api/hooks.ts:353-370` (delete mutation) — on 202, do NOT remove from cache; set a `deleteState` field on the cached session instead
- Modify: `web/src/components/SessionCard.tsx` — `deleteState === 'pending'` → chip "Deleting… (retrying)"; `'stuck'` → error-tier chip "Delete stuck — needs manual cleanup"
- Test: `server/tests/test_delete_pending.py`, extend the Dashboard/SessionCard vitest suites (FULL run — the #938 manual-mock lesson applies here directly: `SessionCard` is shared)
- [ ] **Steps: failing tests (API 202 path via monkeypatched rmtree failure; card chip render) → implement → both gates → commit** — `feat(sessions): surface pending/stuck deletes instead of resurrecting cards (spec 002 FR-404)`

### Task 13: Auto-archive announce + undo (G10)

**Files:**
- Modify: `server/sessions/orchestrator.py:9291-9372` (`archive_session`) — when `archived_by` ∈ `AUTO_ARCHIVE_PROVENANCE`, `events.record(AUTO_ARCHIVED, {"by": archived_by, "reason": <goal-met summary from archive_context>})`
- Modify: `web/src/components/SessionCard.tsx` (archived group) — provenance chip ("Auto-archived · goal met") + Undo button wired to the EXISTING unarchive mutation in `web/src/api/hooks.ts` (grep `unarchive`; it already pins the sticky opt-out — do not add a second path)
- Test: `server/tests/test_auto_archive_event.py`, SessionCard vitest
- [ ] **Steps: failing tests → implement → gates → commit** — `feat(sessions): announce auto-archive with undo affordance (spec 002 FR-405)`

### Task 14: Events API + session timeline + lost-hold banner

**Files:**
- Create: `GET /api/sessions/{id}/events` route in `server/api/sessions.py` (serializes `events.for_session`, newest first)
- Create: `web/src/components/session/SessionTimeline.tsx` — collapsed `<details>` strip, one row per event: relative time + human label per kind (map the eight kinds to copy: `moved_host` → "Moved from {from} to {to}", `hold_lost` → "A pending {kind} was lost in a restart", `spawn_failed` → "Failed to start: {reason}", `auto_resumed` → "Auto-resumed after restart", `interrupted` → "Interrupted ({payload.kind})", `rotated_account` → "Rotated account: {reason}", `auto_archived` → "Auto-archived: {reason}", `delete_retry` → "Delete retry #{strike}")
- Create: `web/src/components/session/LostHoldBanner.tsx` — renders `session.lostHold` above the composer: "When the daemon restarted, the agent was asking:" + content (question text/options, permission summary, or plan title) + the existing resume affordance
- Modify: session detail view to mount both (grep where `PendingModelApprovalPanel` mounts — same region)
- Test: vitest for both components + a route test
- [ ] **Steps: failing tests → implement → gates → commit** — `feat(web): session timeline + lost-hold banner (spec 002 FR-406/FR-407)`

**Phase 2 lands as one PR.**

---

## Phase 3 — Resume policy (FR-306)

### Task 15: Auto-resume by default + hold-aware resume

**Files:**
- Modify: wherever `CCSWITCH_AUTO_RESUME_ON_RESTART` is read (grep it — set default `true`)
- Modify: `server/sessions/orchestrator.py` `auto_resume_interrupted` — extend eligibility: `interrupted_from_status` ∈ waiting statuses IS eligible IFF the session has `lost_hold` content; the nudge for that path is `RESUME_NUDGE` + `"\n\nYou were asking the user:\n{content}\nRe-raise the question if it is still relevant — do not decide it yourself."` Record `AUTO_RESUMED` events (kind + whether hold-aware).
- Modify: `server/db/models.py` — `Session.auto_resume_opt_out: Mapped[bool | None]` (nullable, default None); respected by both auto-resume paths; settable through the existing session-update/PATCH route (grep how `name` renames are accepted — same route, one more accepted field, exposed in types.ts)
- Test: extend `server/tests/test_resume_interrupted.py`
- [ ] **Steps: failing tests (waiting-status session with hold auto-resumes with content in nudge; without hold does not; opt-out session never; work statuses still resume) → implement → gate → commit** — `feat(sessions): auto-resume by default, hold-aware for waiting statuses (spec 002 FR-306)`

**Phase 3 lands as one PR** (small on purpose: it changes default behavior of every restart — keep it revertable in isolation).

---

## Phase 4 — Fenced adoption & cross-host resume (FR-201…FR-205)

### Task 16: Stand-down on fence loss (FR-202) — MUST land before any fence-bumper

**Files:**
- Modify: `server/sessions/orchestrator.py` — replace Task 4's log-only `_on_fenced` body
- Test: `server/tests/test_fence_stand_down.py`

**Interfaces:**
- Produces: `_on_fenced` now: (1) sets `self._fenced = True` — checked at the top of `_persist_status` and `send_prompt` (both become no-ops / rejects while fenced; a fenced daemon persists NOTHING); (2) closes every live driver via the existing bounded close path (grep `bounded_wait` in `stop()` — same discipline, #846); (3) clears `_records`; (4) re-registers (`host_heartbeat.register`) adopting the new fence; (5) re-runs rehydrate, which loads only rows still stamped to this host; (6) clears `_fenced`.
- [ ] **Steps: failing test (fake drivers; after `_on_fenced`, drivers closed, no status writes issued — assert via monkeypatched `db_session` recorder — and rehydrate ran) → implement → gate → commit** — `feat(sessions): fenced daemons stand down without persisting (spec 002 FR-202)`

### Task 17: Cross-host resume verification drill (Principle III gate)

**Files:**
- Create: `specs/002-session-continuity/research.md` — recorded findings
- Create: `scripts/verify-cross-host-resume.sh` (manual drill, not CI)

**Interfaces:** none — this task PROVES the spec assumption "SDK resume works from a composed config dir containing the transcript" against the real binary before Tasks 18–19 build on it.

- [ ] **Step 1:** Script: on host A create a session, run one turn, note `claude_session_id`; on host B (or a second config dir simulating it): `compose(account)`, glob `<cfg_dir>/projects/**/<claude_session_id>.jsonl`, recreate a worktree at a DIFFERENT path, and start the SDK with `resume=<claude_session_id>` (the same resume mechanism `ensure_driver` uses — grep `resume` in `server/sessions/sdk_factory.py` / `driver.py`).
- [ ] **Step 2:** Run it for real. Record in research.md: does resume tolerate a changed cwd? Is the transcript present in the composed dir (i.e., does backup sync include `projects/`)? If either answer is NO, STOP — the fallback ordering in Task 19 flips from resume-preferred to fresh-restart-only, and the spec's FR-204 wording is amended before proceeding. This is the ratchet: hidden complexity upgrades the path, loudly.
- [ ] **Step 3: Commit** — `docs(specs): cross-host resume drill findings (spec 002, principle III)`

### Task 18: Primary adoption pass (FR-201/203/205)

**Files:**
- Create: `server/sessions/adoption.py`
- Modify: `server/sessions/orchestrator.py` — schedule `_adoption_loop` (every 60 s) ONLY on the daemon holding the primary lease (grep how other primary-only loops are gated — the automation loops cite #911's every-daemon-runs-every-loop bug; copy the guard they added)
- Test: `server/tests/test_adoption.py`

**Interfaces:**
- Consumes: `host_heartbeat.snapshot/declare_dead(observed_fence=…)` (Task 2), `events.record`/`INTERRUPTED`/`MOVED_HOST` (Task 7), ranked-host selection (grep `create_session` host ranking in `fleet_orchestrator.py:157-173`).
- Produces: `async def find_dead_hosts(now, *, dead_after_s) -> list[HeartbeatState]` (stale rows owning ≥1 session whose status ∉ {`ended`,`archived`}); `async def adopt_host(orch, hb: HeartbeatState) -> int` — fence first via `declare_dead` (None → lost the race → return 0), then per session: `INTERRUPTED{kind: host_lost, from_status}` event, normalize via the SAME rehydrate normalization (extract the existing per-row normalization at orchestrator.py:5999-6028 into a reusable `_normalize_interrupted_row(row, cause)` — refactor, do not copy), restamp `session.host` to the chosen healthy host, `MOVED_HOST{from, to}` event. Capacity respected: adopted sessions enter through `_enqueue_session` when at cap (provenance already recorded).
- [ ] **Steps: failing tests (dead host with 2 sessions adopted exactly once under two racing passes — second `declare_dead` returns None; fresh host never adopted; sessions restamped + events written; at-cap adoption lands queued) → implement → gate → commit** — `feat(sessions): fenced adoption of sessions off dead hosts (spec 002 FR-201/FR-203/FR-205)`

### Task 19: Best-effort resume on the new host (FR-204)

**Files:**
- Modify: `server/sessions/adoption.py` — post-adopt spawn step
- Test: `server/tests/test_adoption_resume.py`

**Interfaces:**
- Consumes: Task 17's verified mechanism; `compose(account)`; worktree creation (`_spawn_session`'s existing path); `RESUME_NUDGE` (orchestrator.py:118).
- Produces: for each adopted session, in order: `compose(account)` on this host → glob `projects/**/<claude_session_id>.jsonl` → if found, spawn with `resume=claude_session_id` in a recreated worktree and send: `"This session was moved from {from_host} after it became unreachable. Continue where you left off."` → else fresh spawn (same repo/branch; child branch if taken) and send the continuity prompt: `"This session was moved from {from_host} after that host became unreachable; its conversation could not be recovered. Repo: {repo}, branch: {branch} (unpushed work may still exist on {from_host}). The goal was: {title}. The last prompt was: {initial_prompt_or_last}. Re-establish context and continue."` Both paths append to the `MOVED_HOST` payload: `{"resumed": bool}`.
- [ ] **Steps: failing tests (transcript-present path resumes with nudge; absent path fresh-spawns with continuity prompt naming branch + host; both record resumed flag) → implement → gate → commit** — `feat(sessions): best-effort cross-host resume with continuity nudge (spec 002 FR-204)`

### Task 20: Fleet drill — end-to-end acceptance

**Files:**
- Create: `server/tests/test_continuity_drill.py` (integration; SQLite; fake drivers + real orchestrator, two orchestrator instances sharing one DB simulating two hosts)
- Test: itself
- [ ] **Steps:** Encode spec acceptance scenarios 1 + 2 as one test each: (1) host w1's heartbeat ages past threshold with an `in_progress` session → primary pass on w2 adopts, session restamped w2 + `interrupted`/`moved_host` events + status idle; (2) w1's orchestrator then calls `beat` → gets False → `_on_fenced` closes its fake driver and persists nothing (recorder assert). Run FULL server suite serially (memory: concurrent full-suite runs on shared hosts SIGKILL at the Bash timeout — background it with `tee /tmp/002-drill.log`). Commit — `test(sessions): fleet continuity drill (spec 002 acceptance 1-2)`

**Phase 4 lands as one PR.**

---

## Self-review record

- **Spec coverage:** FR-101→T1/2/4 · FR-102→T3 · FR-103→T5 · FR-104→T6 · FR-201→T18 · FR-202→T16 · FR-203→T18 · FR-204→T17/19 · FR-205→T18 · FR-301→T7 · FR-302→T8 · FR-303/304→T9 · FR-305→T10 · FR-306→T15 · FR-307→T11 · FR-401/402→T6 · FR-403→T11 · FR-404→T12 · FR-405→T13 · FR-406/407→T14. NFR-1 verified by T6 matrix + T5; NFR-2 by T2/T16/T18 races; NFR-3 by T3 missing-row rule; NFR-4 by design (O(hosts) writes); NFR-5 by T7 swallow-and-log + T4 loop guard; NFR-6 rides the global constraints.
- **Type consistency:** `declare_dead(host, *, dead_after_s, observed_fence)` — T2's refined signature is what T18 consumes. `Liveness.last_confirmed_alive_at` ↔ API `lastConfirmedAliveAt` ↔ TS `lastConfirmedAliveAt`. Event-kind constants defined once in T7, consumed T8/9/10/13/14/15/18/19.
- **Ordering hazards encoded:** T16 (stand-down) is a hard prerequisite of T18 (the only fence-bumper); T17 (real-binary drill) gates T19's resume ordering; T4's `_on_fenced` is explicitly a stub until T16.
